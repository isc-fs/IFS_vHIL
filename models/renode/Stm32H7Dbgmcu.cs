//
// DBGMCU of the STM32H72x/H73x (the MCU debug unit), compiled by Renode at
// load time and placed by platforms/cpus/stm32h733.repl at DBGMCU_BASE
// 0x5C001000 (stm32h733xx.h:2491). Renode's stm32h7.repl has none, so every
// access went to an unmapped address.
//
// Who touches it: HAL_DMA_Init reads IDCODE (stm32h7xx_hal_dma.c:302, a
// rev-V UART erratum), and the
// AMS's idle-WFI change (IFS08-CE-AMS#626, main.c:219) sets DBG_SLEEPD1 with
// HAL_DBGMCU_EnableDBGSleepMode.
//
// Registers (DBGMCU_TypeDef in stm32h733xx.h:533-558; RM0468 "Debug
// support", DBGMCU registers):
// - IDCODE (0x00), read-only: REV_ID [31:16], DEV_ID [11:0]. DEV_ID is 0x483
//   for the H72x/H73x; REV_ID is the repl's idCode, 0x1001 by default
//   (silicon revision Z, REV_ID_Z in stm32h7xx_hal.h:62). Every HAL test of
//   it ("<= REV_ID_Y", ">= 0x2000") reads the same for Z as for the 0 an
//   unmapped read gave before.
// - CR (0x04), read/write: DBG_SLEEPD1 0, DBG_STOPD1 1, DBG_STANDBYD1 2,
//   DBG_STOPD3 7, DBG_STANDBYD3 8, DBG_TRACECKEN 20, DBG_CKD1EN 21,
//   DBG_CKD3EN 22, DBG_TRGOEN 28 (DBGMCU_CR_* in stm32h733xx.h:22208-22232).
//   Stored, with no effect on the emulation: they keep the debug and trace
//   clocks running through Sleep/Stop/Standby so a probe stays attached, and
//   the emulator's debugger (VhilGdb) needs no clock. The low-power modes
//   themselves are what the CPU model does with WFI, not what these say.
// - APB3FZ1, APB1LFZ1, APB1HFZ1, APB2FZ1, APB4FZ1 (0x34, 0x3C, 0x44, 0x4C,
//   0x54), read/write: the peripheral freeze bits. Stored, with no effect:
//   they act only while a debugger halts the core, and a halted Renode
//   machine stops its virtual time, timers and watchdogs included, anyway.
// Not modelled: the CoreSight ID registers (PIDR/CIDR, 0xFD0-0xFFC); a read
// logs as unhandled, for the peripheral guard.
//
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class STM32H7_DBGMCU : IDoubleWordPeripheral, IKnownSize
    {
        public STM32H7_DBGMCU(uint idCode = 0x10010483)
        {
            this.idCode = idCode;
            Reset();
        }

        public long Size => 0x1000;

        public void Reset()
        {
            cr = 0;
            freeze = new uint[FreezeOffsets.Length];
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == IDCODE)
            {
                return idCode;
            }
            if(offset == CR)
            {
                return cr;
            }
            var i = FreezeIndex(offset);
            if(i >= 0)
            {
                return freeze[i];
            }
            this.Log(LogLevel.Warning, "Unhandled read from offset 0x{0:X}.", offset);
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == IDCODE)
            {
                return;   // read-only
            }
            if(offset == CR)
            {
                cr = value & CrMask;
                return;
            }
            var i = FreezeIndex(offset);
            if(i >= 0)
            {
                freeze[i] = value;
                return;
            }
            this.Log(LogLevel.Warning, "Unhandled write to offset 0x{0:X}, value 0x{1:X}.", offset, value);
        }

        private static int FreezeIndex(long offset)
        {
            return System.Array.IndexOf(FreezeOffsets, offset);
        }

        private const long IDCODE = 0x00;
        private const long CR = 0x04;
        private static readonly long[] FreezeOffsets = { 0x34, 0x3C, 0x44, 0x4C, 0x54 };
        // Bits 0-2, 7-8, 20-22 and 28 (stm32h733xx.h:22208-22232); the rest
        // are reserved and read 0.
        private const uint CrMask = 0x10700187;

        private uint cr;
        private uint[] freeze;
        private readonly uint idCode;
    }
}
