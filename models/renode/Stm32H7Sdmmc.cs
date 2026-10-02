//
// SDMMC1 of the STM32H7 with its internal DMA (IDMA), compiled by Renode at
// load time and placed by platforms/cpus/stm32h733.repl in place of the
// STM32HSDMMC that Renode's stm32h7.repl puts at the same address.
//
// Renode's STM32HSDMMC moves data only through the FIFO register and tags the
// IDMA registers, so a HAL that transfers by IDMA (the AMS's FatFs path:
// HAL_SD_ReadBlocks_DMA / HAL_SD_WriteBlocks_DMA, stm32h7xx_hal_sd.c) never
// sees a block arrive. This keeps Renode's command and FIFO model and adds
// IDMA in single-buffer mode: when a data command starts with IDMACTRL.IDMAEN
// set, the block(s) move between the card and memory at IDMABASE0 in one go,
// and DATAEND/DBCKEND are raised as at the end of a FIFO transfer. A transfer
// takes no virtual time.
// Not modelled: double-buffer mode (IDMABMODE; logs a warning), IDMABTC and
// IDMATE, transfer timing.
//
// Register offsets from ST's stm32h733xx.h (SDMMC_TypeDef: IDMACTRL 0x50,
// IDMABSIZE 0x54, IDMABASE0 0x58, IDMABASE1 0x5C); SDMMC1_BASE =
// D1_AHB1PERIPH_BASE + 0x7000 = 0x52007000, DLYB_SDMMC1 at +0x8000, so the
// block is 0x1000 long. The H7-specific CLKCR/CMD/DCTRL fields follow Renode's
// STM32HSDMMC (MIT, Antmicro), which this replaces.
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SD
{
    public class STM32H7_SDMMC : STM32SDMMC, IKnownSize, IDoubleWordPeripheral
    {
        public STM32H7_SDMMC(IMachine machine) : base(machine)
        {
        }

        public long Size => 0x1000;

        // Re-implemented so a CMD write can be followed by the IDMA transfer
        // the base class's data path does not know about.
        public new void WriteDoubleWord(long offset, uint value)
        {
            base.WriteDoubleWord(offset, value);
            if(offset != CmdOffset || (value & CpsmEnable) == 0 || !idmaEnable.Value)
            {
                return;
            }
            if(idmaDoubleBuffer.Value)
            {
                this.WarningLog("IDMA double-buffer mode is not modelled; transferring buffer 0 only");
            }
            // The base class queues the card access for the nearest synced
            // state; queue the IDMA after it.
            Machine.LocalTimeSource.ExecuteInNearestSyncedState(_ => TransferByIdma());
        }

        protected override void InitializeRegisters()
        {
            base.InitializeRegisters();

            Clock
                .WithReservedBits(10, 2)
                .WithTaggedFlag("Power saving configuration bit (PWRSAV)", 12)
                .WithReservedBits(13, 1)
                // The model moves whole blocks whatever the bus width.
                .WithValueField(14, 2, name: "Wide bus mode enable bit (WIDBUS)")
                .WithTaggedFlag("SDMMC_CK dephasing selection bit for data and command (NEGEDGE)", 16)
                .WithTaggedFlag("Hardware flow control enable (HWFC_EN)", 17)
                .WithTaggedFlag("Data rate signaling selection (DDR)", 18)
                .WithTaggedFlag("Bus speed for selection of SDMMC operating modes (BUSSPEED)", 19)
                .WithTag("Receive clock selection (SELCLKRX)", 20, 2)
                .WithReservedBits(22, 10);
            Cmd
                .WithFlag(6, name: "CMDTRANS")
                .WithFlag(7, name: "CMDSTOP")
                .WithTaggedFlag("DTHOLD", 13)
                .WithTaggedFlag("BOOTMODE", 14)
                .WithTaggedFlag("BOOTEN", 15)
                .WithTaggedFlag("CMDSUSPEND", 16)
                .WithReservedBits(17, 15);
            DataCtrl
                .WithTag("Data transfer mode selection (DTMODE)", 2, 2)
                .WithTaggedFlag("BOOTACKEN", 12)
                .WithTaggedFlag("FIFO reset, flushes any remaining data (FIFORST)", 13)
                .WithReservedBits(14, 18);
            for(long offset = 0x4; offset < 0x40; offset += 0x4)
            {
                RegistersCollection.DefineRegister((long)Registers.Fifo + offset)
                    .WithTag("Receive and transmit FIFO data (FIFODATA)", 0, 32);
            }
            Registers.IDMACtrl.Define(this)
                .WithFlag(0, out idmaEnable, name: "IDMA enable (IDMAEN)")
                .WithFlag(1, out idmaDoubleBuffer, name: "Buffer mode selection (IDMABMODE)")
                .WithFlag(2, name: "Double buffer mode active buffer indication (IDMABACT)")
                .WithReservedBits(3, 29);
            Registers.IDMABSize.Define(this)
                .WithReservedBits(0, 5)
                .WithValueField(5, 8, name: "Number of bytes per buffer (IDMABNDT)")
                .WithReservedBits(13, 19);
            Registers.IDMABase0.Define(this)
                .WithValueField(0, 32, out idmaBase0, name: "Buffer 0 memory base address (IDMABASE0)");
            Registers.IDMABase1.Define(this)
                .WithValueField(0, 32, name: "Buffer 1 memory base address (IDMABASE1)");
        }

        protected override int ClkDivWidth => 10;

        protected override int CommandFieldsOffset => 8;

        // Moves what the data command left in the base class's buffers: a read
        // drains ReadDataBuffer into memory, a write feeds the card from memory.
        // ReadBuffer/WriteBuffer raise DATAEND and DBCKEND on the last word.
        private void TransferByIdma()
        {
            var sysbus = Machine.GetSystemBus(this);
            var address = idmaBase0.Value;
            var words = 0;
            while(ReadDataBuffer.Count > 0)
            {
                sysbus.WriteDoubleWord(address, ReadBuffer());
                address += 4;
                words++;
            }
            while(WriteDataLeft > 0)
            {
                WriteBuffer(sysbus.ReadDoubleWord(address));
                address += 4;
                words++;
            }
            this.DebugLog("IDMA moved {0} bytes at 0x{1:X}", words * 4, idmaBase0.Value);
        }

        private IFlagRegisterField idmaEnable;
        private IFlagRegisterField idmaDoubleBuffer;
        private IValueRegisterField idmaBase0;

        private const long CmdOffset = 0x0C;
        private const uint CpsmEnable = 1u << 12;   // CMD.CPSMEN, CommandFieldsOffset + 4

        private enum Registers
        {
            IDMACtrl = 0x50,
            IDMABSize = 0x54,
            IDMABase0 = 0x58,
            IDMABase1 = 0x5C,
            Fifo = 0x80
        }
    }
}
