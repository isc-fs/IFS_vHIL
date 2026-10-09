//
// The Renode monitor on a loopback-only TCP port (vhil/renode.py, launch).
// Compiled by Renode at start-up, with its own monitor port turned off:
//
//     renode --disable-gui --plain -P -1 \
//         -e "include @models/renode/VhilMonitor.cs; emulation StartVhilMonitor 40123"
//
// Renode 1.17's `-P <port>` listens on every interface, unauthenticated
// (SocketServerProvider.Start binds IPAddress.Any, and the CLI has no bind
// address option), and anyone who reaches the port runs monitor commands,
// which include reading and writing host files. This serves the same monitor,
// a shell per connection as the CLI builds for -P (CommandLineInterface.cs,
// PrepareShell), on 127.0.0.1 only. One client at a time: a second connects
// once the first closes.
//
// A machine abort ends the client's connection, after one line naming it:
//
//     VHIL-ABORT machine 'ams' aborted (PC 0x40000000); see the Renode log
//
// Renode 1.17 pauses an aborted machine for good ("Emulation cannot continue
// until this machine is removed", Machine.Abort), and a `RunFor` waiting on
// its CPU never returns (#239): the client (vhil/renode.py) gets the line
// instead of a prompt that never comes.
//
using System;
using System.Collections.Concurrent;
using System.Linq;
using System.Text;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using AntShell;
using AntShell.Terminal;
using Antmicro.Renode.Core;
using Antmicro.Renode.Utilities;
using RenodeMonitor = Antmicro.Renode.UserInterface.Monitor;
using Antmicro.Renode.UserInterface;

namespace Antmicro.Renode.Testing
{
    public static class VhilMonitorExtensions
    {
        public static void StartVhilMonitor(this Emulation emulation, int port)
        {
            var monitor = ObjectCreator.Instance.GetSurrogate<RenodeMonitor>();
            var listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start(1);
            emulation.MachineStateChanged += OnMachineStateChanged;
            EmulationManager.Instance.EmulationChanged += () =>
                EmulationManager.Instance.CurrentEmulation.MachineStateChanged += OnMachineStateChanged;
            new Thread(() => Serve(listener, monitor)) { IsBackground = true, Name = "vhil-monitor" }.Start();
        }

        private static void OnMachineStateChanged(IMachine machine, MachineStateChangedEventArgs args)
        {
            var io = client;
            if(args.CurrentState != MachineStateChangedEventArgs.State.Aborted || io == null)
            {
                return;
            }
            var emulation = EmulationManager.Instance.CurrentEmulation;
            var name = emulation.TryGetMachineName(machine, out var n) ? n : "?";
            var cpu = machine.SystemBus.GetCPUs().FirstOrDefault();
            var pc = cpu != null ? string.Format(" (PC 0x{0:X8})", cpu.PC.RawValue) : "";
            foreach(var b in Encoding.ASCII.GetBytes($"\nVHIL-ABORT machine '{name}' aborted{pc}; see the Renode log\n"))
            {
                io.Write(b);
            }
            // Not on the aborting CPU's thread: closing waits for the line to go out.
            new Thread(io.Dispose) { IsBackground = true, Name = "vhil-monitor-abort" }.Start();
        }

        private static void Serve(TcpListener listener, RenodeMonitor monitor)
        {
            Shell current = null;
            monitor.MachineChanged += name => current?.SetPrompt(name != null
                ? new Prompt(string.Format("({0}) ", name), ConsoleColor.DarkYellow) : null);
            monitor.Quitted += () => current?.Stop();
            while(true)
            {
                Socket socket;
                try
                {
                    socket = listener.AcceptSocket();
                }
                catch(SocketException)
                {
                    return;
                }
                using(var io = new LoopbackIO(socket))
                {
                    var shell = ShellProvider.GenerateShell(monitor);
                    shell.Terminal = new NavigableTerminalEmulator(new IOProvider { Backend = io }, null);
                    shell.Terminal.PlainMode = true;
                    monitor.Interaction = shell.Writer;
                    current = shell;
                    client = io;
                    shell.Start(true);   // returns when the client closes
                    client = null;
                    current = null;
                }
            }
        }

        private static volatile LoopbackIO client;
    }

    // A client socket as AntShell's passive I/O source: blocking reads, and
    // writes drained by a thread so no output waits for a Flush.
    public class LoopbackIO : IPassiveIOSource
    {
        public LoopbackIO(Socket socket)
        {
            this.socket = socket;
            socket.NoDelay = true;
            writer = new Thread(Drain) { IsBackground = true, Name = "vhil-monitor-tx" };
            writer.Start();
        }

        public bool TryPeek(out int value)
        {
            if(peeked == -1 && Available())
            {
                peeked = ReadOne();
            }
            value = peeked;
            return peeked != -1;
        }

        public int Read()
        {
            if(peeked != -1)
            {
                var b = peeked;
                peeked = -1;
                return b;
            }
            return ReadOne();
        }

        public void CancelRead()
        {
            Close();
        }

        public void Write(byte b)
        {
            if(!output.IsAddingCompleted)
            {
                try { output.Add(b); } catch(InvalidOperationException) { }
            }
        }

        public void Flush()
        {
        }

        public void Dispose()
        {
            Close();
        }

        private bool Available()
        {
            try { return socket.Available > 0; } catch(Exception) { return false; }
        }

        private int ReadOne()
        {
            try
            {
                return socket.Receive(one, 0, 1, SocketFlags.None) == 1 ? one[0] : -1;
            }
            catch(Exception)
            {
                return -1;
            }
        }

        private void Drain()
        {
            var buffer = new byte[4096];
            try
            {
                foreach(var first in output.GetConsumingEnumerable())
                {
                    var n = 0;
                    buffer[n++] = first;
                    while(n < buffer.Length && output.TryTake(out var next))
                    {
                        buffer[n++] = next;
                    }
                    socket.Send(buffer, 0, n, SocketFlags.None);
                }
            }
            catch(Exception)
            {
                // the client went away; Read returns -1 and the shell ends
            }
        }

        private void Close()
        {
            if(Interlocked.Exchange(ref closed, 1) == 1)
            {
                return;
            }
            output.CompleteAdding();
            writer.Join(1000);
            try { socket.Shutdown(SocketShutdown.Both); } catch(Exception) { }
            socket.Close();
        }

        private readonly Socket socket;
        private readonly Thread writer;
        private readonly BlockingCollection<byte> output = new BlockingCollection<byte>();
        private readonly byte[] one = new byte[1];
        private int peeked = -1;
        private int closed;
    }
}
