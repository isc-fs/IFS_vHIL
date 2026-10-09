//
// A board's machine with a time source of its own (#209). Compiled by Renode
// at load time, before any board is created (vhil/system.py).
//
//     emulation CreateVhilMachine "ecu"
//     mach set "ecu"
//
// Renode 1.17's `mach create` makes a machine whose local time source IS the
// emulation's master time source (Emulation.TryAddMachine: a machine with no
// time source of its own gets the master). With two boards, both CPUs are then
// sinks of one time source, and its virtual time is the minimum of what the
// two CPUs have executed: each machine's clock (its timers, interrupts and
// scheduled actions) advances only as far as the slower CPU, on whichever
// CPU thread moved that minimum. Where a firmware's timer interrupt lands in
// its instruction stream then depends on how far the other board's thread got
// on the host, so two runs of the same system diverge within milliseconds of
// boot (#209: 20 runs of ecu-ams, 20 different CAN timelines).
//
// A machine created with a SlaveTimeSource (Machine(createLocalTimeSource:
// true)) has a clock driven by its own CPU only. The master grants each slave
// the same quantum, and the boards still meet at every sync point, where the
// CAN bus model decides (VhilCanBus.cs). Between sync points each board runs
// alone, as it does in a single-board system, which is deterministic.
//
// The slave takes the master's quantum, advance-immediately and serial
// settings at creation (`emulation SetGlobal*` set them on every slave
// afterwards).
//
// A machine's reset (watchdog, SYSRESETREQ: Machine.RequestReset) now runs in
// that machine's own synced state, on its own thread, so two boards can reset
// at once. Renode's Monitor runs each board's reset macro by swapping its one
// current-machine field (Monitor.ResetMachine, ObtainMachineContext) with no
// lock: two at once could run one board's macro on the other. The reset
// handlers are serialised here.
//
using System;
using System.Reflection;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;

namespace Antmicro.Renode.Testing
{
    public static class VhilMachineExtensions
    {
        public static void CreateVhilMachine(this Emulation emulation, string name)
        {
            var machine = new Machine(createLocalTimeSource: true);
            var slave = machine.LocalTimeSource;
            var master = emulation.MasterTimeSource;
            slave.Quantum = master.Quantum;
            slave.AdvanceImmediately = master.AdvanceImmediately;
            slave.ExecuteInSerial = master.ExecuteInSerial;
            emulation.AddMachine(machine, name);
            SerialiseResets(machine);
        }

        // Wrap every MachineReset handler registered so far (the Monitor's
        // reset macro, on MachineAdded) in one lock shared by all machines.
        private static void SerialiseResets(Machine machine)
        {
            var field = typeof(Machine).GetField("MachineReset", BindingFlags.Instance | BindingFlags.NonPublic);
            if(field == null)
            {
                throw new RecoverableException("Machine.MachineReset's field not found (Renode version?)");
            }
            var handlers = field.GetValue(machine) as Action<IMachine>;
            if(handlers == null)
            {
                return;
            }
            foreach(var handler in handlers.GetInvocationList())
            {
                var h = (Action<IMachine>)handler;
                machine.MachineReset -= h;
                machine.MachineReset += m =>
                {
                    lock(ResetLock)
                    {
                        h(m);
                    }
                };
            }
        }

        private static readonly object ResetLock = new object();
    }
}
