//
// One owner for the bus hooks on a board's peripheral. Renode keeps a single
// read hook and a single write hook per peripheral (SystemBus.SetHook*
// replaces the previous one), so two models that each hooked the same
// peripheral would silently disable each other. The models that own registers
// of one register them here instead, and this dispatches each 32-bit access
// by offset; offsets nobody registered go to the peripheral unchanged.
//
//   sysbus.rcc   VhilResetFlags.cs (RCC_RSR), VhilRccResets.cs (xxxRSTR)
//   sysbus.nvic  VhilResetFlags.cs (AIRCR), VhilExecuteNever.cs (MPU_CTRL),
//                VhilCaches.cs (CCR, ICIALLU)
//
using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Testing
{
    public class VhilBusHooks
    {
        // The dispatcher of a board's peripheral (e.g. "sysbus.rcc"), created
        // (and the hooks installed) on first use.
        public static VhilBusHooks For(IMachine machine, string peripheral)
        {
            lock(perMachine)
            {
                var hooks = perMachine.GetOrCreateValue(machine);
                if(!hooks.TryGetValue(peripheral, out var dispatcher))
                {
                    dispatcher = new VhilBusHooks(machine, peripheral);
                    hooks.Add(peripheral, dispatcher);
                }
                return dispatcher;
            }
        }

        // value -> what the access reads.
        public void OnRead(long offset, Func<uint, uint> handler)
        {
            if(reads.Count == 0)
            {
                // Only a peripheral with a read handler pays for a read hook.
                machine.SystemBus.SetHookAfterPeripheralRead<uint>(Peripheral, (value, at) =>
                    reads.TryGetValue(at, out var h) ? h(value) : value);
            }
            reads.Add(offset, handler);
        }

        // written value -> what reaches the peripheral's model.
        public void OnWrite(long offset, Func<uint, uint> handler)
        {
            writes.Add(offset, handler);
        }

        public IBusPeripheral Peripheral { get; }

        private VhilBusHooks(IMachine machine, string peripheral)
        {
            if(!machine.TryGetByName<IBusPeripheral>(peripheral, out var target))
            {
                throw new ArgumentException($"needs {peripheral}");
            }
            this.machine = machine;
            Peripheral = target;
            machine.SystemBus.SetHookBeforePeripheralWrite<uint>(target, (value, offset) =>
                writes.TryGetValue(offset, out var h) ? h(value) : value);
        }

        private readonly IMachine machine;
        private readonly Dictionary<long, Func<uint, uint>> reads = new Dictionary<long, Func<uint, uint>>();
        private readonly Dictionary<long, Func<uint, uint>> writes = new Dictionary<long, Func<uint, uint>>();

        private static readonly ConditionalWeakTable<IMachine, Dictionary<string, VhilBusHooks>> perMachine =
            new ConditionalWeakTable<IMachine, Dictionary<string, VhilBusHooks>>();
    }
}
