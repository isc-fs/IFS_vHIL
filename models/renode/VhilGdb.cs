//
// Renode's GDB stub for one machine, on a loopback-only TCP port and with a
// read-only packet set (the debugger: vhil/gdb.py, docs/debugger.md).
// Compiled by Renode when a live session first debugs a board:
//
//     include @models/renode/VhilGdb.cs
//     mach set "ecu"
//     machine StartVhilGdbServer 40124 "/tmp/vhil-gdb-xyz/ecu.halts"
//
// Renode 1.17's `machine StartGdbServer <port>` listens on every interface,
// unauthenticated (SocketServerProvider.Start binds IPAddress.Any, with no
// option to bind one address), and whoever reaches it reads and writes all of
// the target's memory and runs monitor commands through `qRcmd` (GDB's
// `monitor`), which include reading and writing host files. This builds the
// same stub (GdbStub, on the same SocketServerProvider) with:
//
//   - the provider's listening socket bound to 127.0.0.1, before anything
//     listens (one client at a time, as Renode's own);
//   - only the packets a debugger that looks needs (Allowed): breakpoints
//     (Z/z), continue and step (c/s), reading registers and memory (g/p/m),
//     the halt reason, qSupported, qXfer (the target description) and qCRC.
//     No monitor commands (qRcmd), no memory or register writes (M/X/P), no
//     vRun or kill, no reverse execution, no Trace32 register access: an
//     unknown packet gets the empty reply, "not supported";
//   - a line per halt in `haltLog`, "<virtual us> <reason>", so the worker
//     knows when a board stopped while its monitor waits in RunFor (the whole
//     emulation stops with it: a halted CPU holds the time source).
//
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Threading;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Utilities;
using Antmicro.Renode.Utilities.GDB;

namespace Antmicro.Renode.Testing
{
    public static class VhilGdbExtensions
    {
        public static void StartVhilGdbServer(this IMachine machine, int port, string haltLog)
        {
            lock(stubs)
            {
                if(stubs.ContainsKey(machine))
                {
                    throw new RecoverableException("this machine's GDB server runs already");
                }
                var cpus = machine.SystemBus.GetCPUs().OfType<ICpuSupportingGdb>().ToList();
                if(cpus.Count == 0)
                {
                    throw new RecoverableException("no CPU that supports GDB");
                }
                var provider = new SocketServerProvider(false, false, "vhil-gdb");
                var server = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                server.Bind(new IPEndPoint(IPAddress.Loopback, port));
                server.Listen(1);
                Set(provider, "server", server);
                Set(provider, "stopRequested", false);

                // GdbStub(machine, cpus) and then its terminal, as its port
                // constructor does (connected: false, autostartEmulation:
                // false): the CPUs halt when a client connects, not before.
                var stub = (GdbStub)typeof(GdbStub).GetConstructor(
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic, null,
                    new[] { typeof(IMachine), typeof(IEnumerable<ICpuSupportingGdb>) }, null)
                    .Invoke(new object[] { machine, cpus });
                Restrict(stub.CommandsManager);
                Set(stub, "terminal", provider);
                typeof(GdbStub).GetMethod("SetupTerminal", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public)
                    .Invoke(stub, new object[] { false, false });

                File.WriteAllText(haltLog, "");
                foreach(var cpu in cpus)
                {
                    cpu.Halted += args =>
                    {
                        var us = (long)machine.ElapsedVirtualTime.TimeElapsed.TotalMicroseconds;
                        lock(stubs)
                        {
                            File.AppendAllText(haltLog, string.Format(CultureInfo.InvariantCulture,
                                "{0} {1}\n", us, args.Reason));
                        }
                    };
                }

                var body = typeof(SocketServerProvider).GetMethod("ListenerThreadBody",
                    BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                var listener = new Thread(() => body.Invoke(provider, null)) { IsBackground = true, Name = "vhil-gdb" };
                Set(provider, "listenerThread", listener);
                listener.Start();
                stubs[machine] = stub;
            }
        }

        public static void StopVhilGdbServer(this IMachine machine)
        {
            lock(stubs)
            {
                if(stubs.TryGetValue(machine, out var stub))
                {
                    stubs.Remove(machine);
                    stub.Dispose();
                }
            }
        }

        // The packets this stub answers (module comment); every other
        // command is dropped from the manager before a client can connect.
        private static void Restrict(CommandsManager manager)
        {
            var f = typeof(CommandsManager).GetField("availableCommands", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
            if(f == null)
            {
                throw new RecoverableException("CommandsManager has no availableCommands (Renode version?)");
            }
            var set = f.GetValue(manager);
            var items = ((IEnumerable)set).Cast<object>().ToList();
            var remove = set.GetType().GetMethod("Remove");
            foreach(var d in items)
            {
                var mnemonic = (string)d.GetType().GetProperty("Mnemonic").GetValue(d);
                if(!Allowed.Contains(mnemonic))
                {
                    remove.Invoke(set, new[] { d });
                }
            }
            // The mnemonics a packet is matched against (TryGetCommand), and
            // any command already looked up.
            var list = (List<string>)Field(manager, "mnemonicList");
            list.RemoveAll(m => !Allowed.Contains(m));
            ((IDictionary)Field(manager, "commandsCache")).Clear();
            var left = ((IEnumerable)set).Cast<object>()
                .Select(d => (string)d.GetType().GetProperty("Mnemonic").GetValue(d)).ToList();
            if(left.Any(m => !Allowed.Contains(m)) || !left.Contains("Z") || !left.Contains("m"))
            {
                throw new RecoverableException("could not restrict the GDB stub's packets");
            }
        }

        private static object Field(object target, string field)
        {
            var f = target.GetType().GetField(field, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
            if(f == null)
            {
                throw new RecoverableException(string.Format("{0} has no field {1} (Renode version?)", target.GetType().Name, field));
            }
            return f.GetValue(target);
        }

        private static void Set(object target, string field, object value)
        {
            var f = target.GetType().GetField(field, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
            if(f == null)
            {
                throw new RecoverableException(string.Format("{0} has no field {1} (Renode version?)", target.GetType().Name, field));
            }
            f.SetValue(target, value);
        }

        private static readonly HashSet<string> Allowed = new HashSet<string>
        {
            "Z", "z", "c", "s", "g", "p", "m", "?", "qSupported", "qXfer", "qCRC:",
        };

        private static readonly Dictionary<IMachine, GdbStub> stubs = new Dictionary<IMachine, GdbStub>();
    }
}
