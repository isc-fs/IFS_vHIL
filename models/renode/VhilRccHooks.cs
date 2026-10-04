//
// One owner for the bus hooks on a board's RCC. Renode keeps a single read
// hook and a single write hook per peripheral (SystemBus.SetHook* replaces
// the previous one), so two models that each hooked the RCC would silently
// disable each other. The models that own RCC registers (VhilResetFlags.cs:
// RCC_RSR; VhilRccResets.cs: the xxxRSTR peripheral resets) register their
// offsets here instead, and this dispatches each 32-bit access by offset.
// Offsets nobody registered go to Renode's STM32H7_RCC unchanged.
//
using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Testing
{
    public class VhilRccHooks
    {
        // The board's dispatcher, created (and the hooks installed) on first use.
        public static VhilRccHooks For(IMachine machine)
        {
            lock(perMachine)
            {
                if(!perMachine.TryGetValue(machine, out var hooks))
                {
                    hooks = new VhilRccHooks(machine);
                    perMachine.Add(machine, hooks);
                }
                return hooks;
            }
        }

        // value -> what the access reads.
        public void OnRead(long offset, Func<uint, uint> handler)
        {
            reads.Add(offset, handler);
        }

        // written value -> what reaches the RCC model.
        public void OnWrite(long offset, Func<uint, uint> handler)
        {
            writes.Add(offset, handler);
        }

        public IBusPeripheral Rcc { get; }

        private VhilRccHooks(IMachine machine)
        {
            if(!machine.TryGetByName<IBusPeripheral>("sysbus.rcc", out var rcc))
            {
                throw new ArgumentException("needs sysbus.rcc");
            }
            Rcc = rcc;
            machine.SystemBus.SetHookAfterPeripheralRead<uint>(rcc, (value, offset) =>
                reads.TryGetValue(offset, out var h) ? h(value) : value);
            machine.SystemBus.SetHookBeforePeripheralWrite<uint>(rcc, (value, offset) =>
                writes.TryGetValue(offset, out var h) ? h(value) : value);
        }

        private readonly Dictionary<long, Func<uint, uint>> reads = new Dictionary<long, Func<uint, uint>>();
        private readonly Dictionary<long, Func<uint, uint>> writes = new Dictionary<long, Func<uint, uint>>();

        private static readonly ConditionalWeakTable<IMachine, VhilRccHooks> perMachine =
            new ConditionalWeakTable<IMachine, VhilRccHooks>();
    }
}
