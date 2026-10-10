//
// The Cortex-M7 L1 cache controls, per board, as architected no-ops for
// timing. Compiled by Renode at load time and created by the generated script
// for every stm32h733 board:
//     emulation CreateVhilCaches "vhil_caches_ams" "ams"
//
// On the chip the I-cache and D-cache are enabled by CCR.IC (bit 17) and
// CCR.DC (bit 16), both read-write and 0 at reset, at 0xE000ED14 (ARMv7-M ARM
// DDI0403E B3.2.8, "Configuration and Control Register"; ST PM0253 4.3.7,
// "Configuration and control register"). CMSIS's SCB_EnableICache
// (cachel1_armv7.h:57-70, CMSIS 5) reads CCR.IC, returns if it is set, else
// invalidates the whole I-cache with a write to ICIALLU (0xE000EF50; ARMv7-M
// ARM B2.2.7, "Cache maintenance operations"; PM0253 4.8, "Cache maintenance
// operations") and sets CCR.IC.
//
// Renode 1.17 has no cache: every fetch and load sees memory, and every
// instruction costs the same virtual time (1 / PerformanceInMips us). That is
// what a coherent cache does to what code observes, so enabling a cache or
// invalidating it changes nothing there, only how fast code runs, which
// Renode can't model (the firmware's catalogue rate stands for it,
// docs/cpu-timing.md). What it did get wrong:
//
//   - CCR: tlib keeps only the CCR bits it implements (tlibSetCcr), so IC and
//     DC read back 0 after the firmware sets them. They are kept here and read
//     back as written; every reset clears them.
//   - ICIALLU: the NVIC model defines no register there, so the write is
//     dropped with an "Unhandled write" warning. It is counted here
//     (ICacheInvalidations); configs/peripherals.yaml explains the warning.
//
// Both go through the NVIC's bus hooks (VhilBusHooks.cs).
//
using System;
using Antmicro.Renode.Core;

namespace Antmicro.Renode.Testing
{
    public static class VhilCachesExtensions
    {
        public static void CreateVhilCaches(this Emulation emulation, string name, string machineName)
        {
            if(!emulation.TryGetMachineByName(machineName, out var machine))
            {
                throw new ArgumentException($"no machine '{machineName}'");
            }
            emulation.ExternalsManager.AddExternal(new VhilCaches(machine), name);
        }
    }

    public class VhilCaches : IExternal
    {
        public VhilCaches(IMachine machine)
        {
            var nvic = VhilBusHooks.For(machine, "sysbus.nvic");
            nvic.OnWrite(Ccr, value =>
            {
                enables = value & (CcrIc | CcrDc);
                return value;
            });
            nvic.OnRead(Ccr, value => (value & ~(CcrIc | CcrDc)) | enables);
            nvic.OnWrite(IcIallu, value =>
            {
                invalidations++;
                return value;
            });
            machine.MachineReset += _ => enables = 0;
        }

        public void Reset()
        {
        }

        // Monitor: whether the firmware has the I-cache / D-cache enabled.
        public bool ICacheEnabled => (enables & CcrIc) != 0;
        public bool DCacheEnabled => (enables & CcrDc) != 0;
        // Monitor: ICIALLU writes since the emulation started.
        public int ICacheInvalidations => invalidations;

        private const long Ccr = 0xD14;              // CCR 0xE000ED14, NVIC at 0xE000E000
        private const long IcIallu = 0xF50;          // ICIALLU 0xE000EF50
        private const uint CcrDc = 1u << 16;
        private const uint CcrIc = 1u << 17;

        private uint enables;
        private int invalidations;
    }
}
