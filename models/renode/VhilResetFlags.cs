//
// The STM32H7's reset-source flags (RCC_RSR), per board. Compiled by Renode at
// load time and created by the generated script for every stm32h733 board:
//     emulation CreateVhilResetFlags "vhil_reset_ecu" "ecu"
//
// Renode's STM32H7_RCC (sealed) resets RSR to 0x00FE0000 on every machine
// reset, so a software or watchdog reset reads as a power-on, and sets
// reserved bit 18. RM0468 Rev 3 (§8.4.4 Table 52 p. 314, Table 61 p. 430)
// says otherwise: RSR resets to 0x00FA0000 on power-on only,
// each reset ORs in its own flags, and RMVF clears them. This owns RSR
// through bus hooks on the RCC (RCC_RSR 0x0D0, its RCC_C1_RSR mirror 0x130)
// and attributes each machine reset:
//
//   power-on        announced: PowerOn (the broker's relay, vhil.sim's power
//                   cycle; vhil.broker.power_on_commands), RSR = 0x00FA0000
//   pin (NRST)      the test API PinReset                 PINRSTF CPURSTF
//   software        AIRCR.SYSRESETREQ written to the NVIC SFTRSTF PINRSTF CPURSTF
//   IWDG1           any other reset: the IWDG is the only other reset source
//                   the platform models (WWDG is not)    IWDG1RSTF PINRSTF CPURSTF
//
// Flags per RM0468 Table 52: POR 1, pin 2, SFTRESET 4, IWDG1 8. Bits per
// stm32h733xx.h (RCC_RSR_*): RMVF 16, CPURSTF 17, D1RSTF 19, D2RSTF 20,
// BORRSTF 21, PINRSTF 22, PORRSTF 23, SFTRSTF 24, IWDG1RSTF 26.
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Testing
{
    public static class VhilResetFlagsExtensions
    {
        public static void CreateVhilResetFlags(this Emulation emulation, string name, string machineName)
        {
            if(!emulation.TryGetMachineByName(machineName, out var machine))
            {
                throw new ArgumentException($"no machine '{machineName}'");
            }
            emulation.ExternalsManager.AddExternal(new VhilResetFlags(machine), name);
        }
    }

    public class VhilResetFlags : IExternal
    {
        public VhilResetFlags(IMachine machine)
        {
            this.machine = machine;
            var sysbus = machine.SystemBus;
            if(!machine.TryGetByName<IBusPeripheral>("sysbus.rcc", out var rcc)
               || !machine.TryGetByName<IBusPeripheral>("sysbus.nvic", out var nvic))
            {
                throw new ArgumentException("needs sysbus.rcc and sysbus.nvic");
            }
            sysbus.SetHookAfterPeripheralRead<uint>(rcc, (value, offset) =>
                offset == RsrOffset || offset == C1RsrOffset ? rsr : value);
            sysbus.SetHookBeforePeripheralWrite<uint>(rcc, (value, offset) =>
            {
                if((offset == RsrOffset || offset == C1RsrOffset) && (value & Rmvf) != 0)
                {
                    rsr = 0;
                }
                return value;
            });
            sysbus.SetHookBeforePeripheralWrite<uint>(nvic, (value, offset) =>
            {
                if(offset == Aircr && (value >> 16) == AircrKey && (value & SysResetReq) != 0)
                {
                    pending = Software;
                }
                return value;
            });
            machine.MachineReset += _ => OnReset();
        }

        public void Reset()
        {
        }

        // -- test API (monitor) ------------------------------------------------

        // The board's next reset is a power-on (call before machine Reset).
        public void PowerOn()
        {
            pending = PowerOnValue;
            powerOn = true;
        }

        // Pull NRST: a pin reset, now.
        public void PinReset()
        {
            pending = Pin;
            machine.RequestReset();
        }

        public uint Rsr => rsr;

        private void OnReset()
        {
            if(powerOn)
            {
                rsr = PowerOnValue;          // "Reset by power-on reset only"
            }
            else
            {
                rsr |= pending ?? Iwdg;
            }
            machine.Log(LogLevel.Debug, "RCC_RSR 0x{0:X8} after reset", rsr);
            pending = null;
            powerOn = false;
        }

        private readonly IMachine machine;
        private uint rsr = PowerOnValue;      // a board starts powered on
        private uint? pending;
        private bool powerOn;

        private const long RsrOffset = 0x0D0;
        private const long C1RsrOffset = 0x130;
        private const long Aircr = 0xD0C;     // SCB AIRCR 0xE000ED0C, NVIC at 0xE000E000
        private const uint AircrKey = 0x05FA;
        private const uint SysResetReq = 1u << 2;
        private const uint Rmvf = 1u << 16;

        private const uint CpuRst = 1u << 17, D1Rst = 1u << 19, D2Rst = 1u << 20, BorRst = 1u << 21;
        private const uint PinRst = 1u << 22, PorRst = 1u << 23, SftRst = 1u << 24, Iwdg1Rst = 1u << 26;
        private const uint PowerOnValue = PorRst | PinRst | BorRst | D2Rst | D1Rst | CpuRst;   // 0x00FA0000
        private const uint Pin = PinRst | CpuRst;
        private const uint Software = SftRst | PinRst | CpuRst;
        private const uint Iwdg = Iwdg1Rst | PinRst | CpuRst;
    }
}
