//
// The FDCAN clock calibration unit (CCU) of the STM32H7, compiled by Renode at
// load time and placed by platforms/cpus/stm32h733.repl over the range
// Renode's stm32h7.repl tags "CAN_CCU" (FDCAN_CCU_BASE = D2_APB1PERIPH_BASE +
// 0xA800, stm32h733xx.h:2340; registers FDCAN_ClockCalibrationUnit_TypeDef,
// stm32h733xx.h:404-413).
//
// HAL_FDCAN_IRQHandler reads the CCU's IR and IE on every FDCAN interrupt
// (stm32h7xx_hal_fdcan.c:5331-5332) and writes IR to clear what it found
// (__HAL_FDCAN_CLEAR_FLAG, stm32h7xx_hal_fdcan.h:1870). Neither firmware
// configures clock calibration (HAL_FDCAN_ConfigClockCalibration): classic
// CAN on the HSE kernel clock, so no calibration event ever raises a flag and
// IR reads 0, as the tag answered. Each tagged access logged a warning,
// formatted on the CPU's thread: three per FDCAN interrupt, a few hundred a
// second per board (#260).
//
//   IR  0x10  calibration interrupt flags, write 1 to clear: no calibration
//             runs, so none is ever set.
//   IE  0x14  calibration interrupt enables: kept, raise nothing.
//
// Not modelled: CREL, CCFG, CSTAT, CWD and calibration itself. An access to
// one is logged as unhandled, which the peripheral guard
// (vhil/peripheral_guard.py) reports.
//
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.CAN
{
    public class STM32H7_FDCAN_CCU : IDoubleWordPeripheral, IKnownSize
    {
        public STM32H7_FDCAN_CCU(IMachine machine)
        {
        }

        public long Size => 0x400;

        public void Reset()
        {
            ie = 0;
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case Ir:
                return 0;
            case Ie:
                return ie;
            }
            this.LogUnhandledRead(offset);
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case Ir:
                return;   // write 1 to clear: nothing is ever set
            case Ie:
                ie = value & IeMask;
                return;
            }
            this.LogUnhandledWrite(offset, value);
        }

        private const long Ir = 0x10;
        private const long Ie = 0x14;
        // IE's two enables, for IR's two flags (the HAL's CCU_IR_MASK,
        // 0xC0000000, shifted down by 30: stm32h7xx_hal_fdcan.h:1537, 1808).
        private const uint IeMask = 0x3;

        private uint ie;
    }
}
