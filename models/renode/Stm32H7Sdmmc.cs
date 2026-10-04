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
// A card that does not answer (none attached, or a VhilSDCard below with
// Respond false: in the slot per card detect, but dead) is modelled as the
// CPSM and DPSM see it (RM0468 Rev 3, 60.5.4 "SDMMC adapter": command path
// state machine pp. 2429-2432, data path state machine pp. 2438-2442; flags in
// 60.10.11 SDMMC_STAR, p. 2487). Renode's base sets CTIMEOUT for every
// command when no card is attached and never CMDSENT, so the HAL's wait for
// CMDSENT after CMD0 (SDMMC_GetCmdError) spun for its full SDMMC_CMDTIMEOUT.
// Here:
//  - WAITRESP = 00: the command is shifted out whether or not a card listens,
//    so CMDSENT is set (CPSM Send -> Idle).
//  - WAITRESP != 00: no response start bit comes within the response timeout
//    (64 SDMMC_CK), so CTIMEOUT is set (CPSM Wait -> Idle); RESPCMDR/RESPxR
//    are not modified.
//  - CMDTRANS with DTDIR = receive enables the DPSM at the end of the command,
//    before any response; with no start bit on D[3:0] its data timer runs out
//    (Wait_R) and DTIMEOUT is set. With DTDIR = transmit the DPSM is enabled
//    only at the end of a response, so a timed-out write command starts no
//    transfer.
//  - a card that stops answering between its command response and the data
//    (the IDMA transfer queued below) moves no data: DTIMEOUT instead of
//    DATAEND/DBCKEND.
// Each raises SDMMC1's interrupt when its MASKR bit is set. The timeouts take
// no virtual time, like the transfers: the response timeout is 64 SDMMC_CK,
// and DTIMEOUT would come DATATIME SDMMC_CK periods later (the HAL programs
// 0xFFFFFFFF; it clears the flag on the command's CTIMEOUT without waiting).
// Test API (monitor): UnansweredCommands here; Respond on the card.
//
// Register offsets from ST's stm32h733xx.h (SDMMC_TypeDef: IDMACTRL 0x50,
// IDMABSIZE 0x54, IDMABASE0 0x58, IDMABASE1 0x5C); SDMMC1_BASE =
// D1_AHB1PERIPH_BASE + 0x7000 = 0x52007000, DLYB_SDMMC1 at +0x8000, so the
// block is 0x1000 long. The H7-specific CLKCR/CMD/DCTRL fields follow Renode's
// STM32HSDMMC (MIT, Antmicro), which this replaces.
//
using System;
using System.Reflection;
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
            // The base keeps its status flags and interrupt update private;
            // the no-answer paths below set the same flags the base does.
            // Renode is pinned (1.17.0): a rename upstream fails here, at load.
            cmdSent = BaseField<IFlagRegisterField>("cmdSent");
            cTimeout = BaseField<IFlagRegisterField>("cTimeout");
            baseUpdateInterrupts = typeof(STM32SDMMC).GetMethod("UpdateInterrupts", BaseMembers)
                ?? throw new InvalidOperationException("STM32SDMMC.UpdateInterrupts not found");
            writeDataLeft = typeof(STM32SDMMC).GetProperty("WriteDataLeft", BaseMembers)
                ?? throw new InvalidOperationException("STM32SDMMC.WriteDataLeft not found");
        }

        public long Size => 0x1000;

        // Test API (monitor): commands sent that no card answered, since load.
        public ulong UnansweredCommands { get; private set; }

        public override void Reset()
        {
            base.Reset();
            dataTimeout = false;
        }

        // DTIMEOUT is a tag in the base's STAR: OR in the modelled flag.
        public new uint ReadDoubleWord(long offset)
        {
            var value = base.ReadDoubleWord(offset);
            if(offset == StarOffset && dataTimeout)
            {
                value |= DTimeoutFlag;
            }
            return value;
        }

        // Re-implemented so a CMD write to a card that does not answer gets
        // the CPSM's no-answer outcome, and one to a card that does can be
        // followed by the IDMA transfer the base class's data path does not
        // know about.
        public new void WriteDoubleWord(long offset, uint value)
        {
            if(offset == CmdOffset && (value & CpsmEnable) != 0 && !CardAnswers)
            {
                // Latch CMDINDEX/WAITRESP/CMDTRANS, but keep the base's card path out.
                base.WriteDoubleWord(offset, value & ~CpsmEnable);
                SendUnanswered(value);
                return;
            }
            base.WriteDoubleWord(offset, value);
            if(offset == IcrOffset || offset == MaskrOffset)
            {
                // The base just updated the IRQ without DTIMEOUT.
                if(offset == IcrOffset && (value & DTimeoutFlag) != 0)
                {
                    dataTimeout = false;
                }
                UpdateInterrupts();
            }
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

        // A card that died between its command response and the data (queued
        // for the nearest synced state) gives none and takes none.
        protected override void ReadCard(SDCard sdCard, uint size)
        {
            if(CardAnswers)
            {
                base.ReadCard(sdCard, size);
            }
        }

        protected override void WriteCard(SDCard sdCard, uint data)
        {
            if(CardAnswers)
            {
                base.WriteCard(sdCard, data);
            }
        }

        private bool CardAnswers =>
            RegisteredPeripheral is VhilSDCard card ? card.Respond : RegisteredPeripheral != null;

        // The CPSM with no card answering (header): CMDSENT for a command that
        // expects no response, CTIMEOUT for one that does; a read data
        // command's DPSM then times out waiting for the data start bit.
        private void SendUnanswered(uint command)
        {
            UnansweredCommands++;
            var waitResp = (command >> WaitRespShift) & 0x3;
            if(waitResp == 0)
            {
                cmdSent.Value = true;
            }
            else
            {
                cTimeout.Value = true;
            }
            if((command & CmdTransFlag) != 0 && (base.ReadDoubleWord(DctrlOffset) & DtDirFlag) != 0)
            {
                dataTimeout = true;
            }
            this.DebugLog("CMD{0} unanswered: {1}{2}", command & 0x3F,
                waitResp == 0 ? "CMDSENT" : "CTIMEOUT", dataTimeout ? ", DTIMEOUT" : "");
            UpdateInterrupts();
        }

        private void UpdateInterrupts()
        {
            baseUpdateInterrupts.Invoke(this, null);
            if(dataTimeout && (base.ReadDoubleWord(MaskrOffset) & DTimeoutFlag) != 0)
            {
                IRQ.Set(true);
            }
        }

        private T BaseField<T>(string name) where T : class
        {
            return typeof(STM32SDMMC).GetField(name, BaseMembers)?.GetValue(this) as T
                ?? throw new InvalidOperationException($"STM32SDMMC.{name} not found");
        }

        // Moves what the data command left in the base class's buffers: a read
        // drains ReadDataBuffer into memory, a write feeds the card from memory.
        // ReadBuffer/WriteBuffer raise DATAEND and DBCKEND on the last word.
        private void TransferByIdma()
        {
            if(!CardAnswers)
            {
                // DPSM in Wait_R/Busy with nothing on D[3:0]: the data timer runs out.
                ReadDataBuffer.Clear();
                writeDataLeft.SetValue(this, 0UL);
                dataTimeout = true;
                this.DebugLog("IDMA: the card does not answer, DTIMEOUT");
                UpdateInterrupts();
                return;
            }
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
        private bool dataTimeout;

        private readonly IFlagRegisterField cmdSent;
        private readonly IFlagRegisterField cTimeout;
        private readonly MethodInfo baseUpdateInterrupts;
        private readonly PropertyInfo writeDataLeft;

        private const BindingFlags BaseMembers = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;

        // stm32h733xx.h: SDMMC_TypeDef offsets and SDMMC_* bit definitions.
        private const long CmdOffset = 0x0C;
        private const long DctrlOffset = 0x2C;
        private const long StarOffset = 0x34;
        private const long IcrOffset = 0x38;
        private const long MaskrOffset = 0x3C;
        private const int WaitRespShift = 8;          // SDMMC_CMD_WAITRESP_Pos
        private const uint CmdTransFlag = 1u << 6;    // SDMMC_CMD_CMDTRANS
        private const uint CpsmEnable = 1u << 12;     // SDMMC_CMD_CPSMEN, CommandFieldsOffset + 4
        private const uint DtDirFlag = 1u << 1;       // SDMMC_DCTRL_DTDIR (1: card to controller)
        private const uint DTimeoutFlag = 1u << 3;    // SDMMC_STA_DTIMEOUT, ICR_DTIMEOUTC, MASK_DTIMEOUTIE

        private enum Registers
        {
            IDMACtrl = 0x50,
            IDMABSize = 0x54,
            IDMABase0 = 0x58,
            IDMABase1 = 0x5C,
            Fifo = 0x80
        }
    }

    // Renode's SDCard with a switch for a card that sits in the slot (card
    // detect low) but does not answer: dead, or unpowered. With Respond false
    // (or the constructor's dead: true) STM32H7_SDMMC treats it as absent on
    // the CMD and data lines (see its header). The card's own state is left as
    // it was; when it answers again the host's next CMD0 resets it. Respond
    // survives a machine reset: a dead card stays dead.
    public class VhilSDCard : SDCard
    {
        public VhilSDCard(long capacity, bool dead = false) : base(capacity)
        {
            Respond = !dead;
        }

        public VhilSDCard(string imageFile, long capacity, bool persistent = false, bool dead = false)
            : base(imageFile, capacity, persistent)
        {
            Respond = !dead;
        }

        public bool Respond { get; set; }
    }
}
