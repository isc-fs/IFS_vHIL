//
// RunFor that returns early on an event: the co-simulation port's
// event-driven exchange (docs/cosim.md, #147). Compiled with the other
// models (vhil/models.py).
//
//     emulation RunForUntilTrigger 100000        -> "<us run> <triggered 0|1>"
//     vhil_gpio_ams Trigger "sysbus.gpioPortB:5" true
//
// `emulation RunFor` runs the master time source for a fixed interval and
// only then gives the monitor back. A plant that must react to a board's
// output (the AMS's contactor coils) within a few ms would have to stop the
// emulation every few ms to look, and each stop costs (docs/performance.md:
// about 2 ms of host CPU a RunFor on ecu-ams, so a 10 ms step is +20 %).
// This runs exactly as Emulation.RunFor does (start every machine, the
// master's RunFor loop, pause), but checks one flag at each sync point and
// returns there when it is set: the flag is raised by a GPIO probe when a pin
// armed with Trigger changes level (VhilProbe.cs, OnGPIO), on the board's
// CPU thread during the quantum. The master reads it after the quantum, when
// every board has reached the sync point, so a run returns at the first sync
// point after the edge: the same one on every run (each board on a time
// source of its own, invariant 11). Nothing else changes: the same quanta,
// the same sync points, so a run with no armed edge computes exactly what
// `emulation RunFor` would.
//
// The loop is MasterTimeSource.RunFor's (Renode 1.17), reached by reflection
// because InnerExecute and the started/active scopes are protected; a Renode
// without them fails loudly here, never silently.
//
using System;
using System.Reflection;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Testing
{
    public static class VhilRunnerExtensions
    {
        // "<us run> <1 if an armed edge ended it, else 0>": an edge in the
        // last quantum ends the run where it would have ended anyway, and the
        // caller still needs to know.
        public static string RunForUntilTrigger(this Emulation emulation, ulong microseconds)
        {
            var ran = VhilRunner.Run(emulation, microseconds);
            return $"{ran} {(VhilRunner.Triggered ? 1 : 0)}";
        }
    }

    public static class VhilRunner
    {
        // Raised by an armed pin's edge; cleared when a run starts.
        public static volatile bool Triggered;

        public static ulong Run(Emulation emulation, ulong microseconds)
        {
            Bind();
            if(emulation.IsStarted)
            {
                throw new RecoverableException("This action is not available when emulation is already started");
            }
            var master = emulation.MasterTimeSource;
            var start = master.ElapsedVirtualTime;
            Triggered = false;
            innerStartAll.Invoke(emulation, null);
            try
            {
                ensureDispatcherExited.Invoke(master, null);
                using((IDisposable)obtainStarted.Invoke(master, null))
                using((IDisposable)obtainActive.Invoke(master, null))
                using(TimeDomainsManager.Instance.RegisterCurrentThread(() => new TimeStamp(master.ElapsedVirtualTime, master.Domain)))
                {
                    var period = TimeInterval.FromMicroseconds(microseconds);
                    var args = new object[2];
                    while(period.Ticks > 0)
                    {
                        waitIfBlocked.Invoke(master, null);
                        args[0] = null;
                        args[1] = (TimeInterval?)period;
                        var syncPoint = (bool)innerExecute.Invoke(master, args);
                        period -= (TimeInterval)args[0];
                        if(syncPoint && Triggered)
                        {
                            break;
                        }
                    }
                }
            }
            finally
            {
                emulation.PauseAll();
            }
            return (ulong)(master.ElapsedVirtualTime - start).TotalMicroseconds;
        }

        private static void Bind()
        {
            if(innerExecute != null)
            {
                return;
            }
            const BindingFlags Any = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
            innerStartAll = Find(typeof(Emulation), "InnerStartAll", Any);
            ensureDispatcherExited = Find(typeof(MasterTimeSource), "EnsureDispatcherExited", Any);
            obtainStarted = Find(typeof(TimeSourceBase), "ObtainStartedState", Any);
            obtainActive = Find(typeof(TimeSourceBase), "ObtainSourceActiveState", Any);
            waitIfBlocked = Find(typeof(TimeSourceBase), "WaitIfBlocked", Any);
            innerExecute = Find(typeof(TimeSourceBase), "InnerExecute", Any);
        }

        private static MethodInfo Find(Type type, string name, BindingFlags flags)
        {
            var method = type.GetMethod(name, flags);
            if(method == null)
            {
                throw new RecoverableException($"{type.Name}.{name} not found (Renode version?)");
            }
            return method;
        }

        private static MethodInfo innerStartAll, ensureDispatcherExited, obtainStarted, obtainActive,
            waitIfBlocked, innerExecute;
    }
}
