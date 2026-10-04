//
// I2C of the STM32H7 (ST's I2C v2 IP) as a bus master, compiled by Renode at
// load time and placed by platforms/cpus/stm32h733.repl in place of the
// STM32F7_I2C that Renode's stm32h7.repl puts at I2C2.
//
// Renode's STM32F7_I2C (same IP) cannot answer "nobody acknowledged": a
// START to an address no target answers only logs a warning, sets no flag,
// and the HAL then busy-waits its whole timeout for TXIS. On a real bus the
// address byte is NACKed: NACKF sets and the master sends a STOP by itself
// (RM0468, I2C master transmitter/receiver: "If a NACK is received ... a STOP
// condition is automatically sent"), so the HAL fails within one address
// byte. That is what a missing or unpowered BMI088 costs the AMS, and what
// this model does. A target may refuse its address too (II2CStartCondition,
// e.g. a model told not to respond). Renode's class is sealed, so this is a
// re-implementation of the master side, not a subclass.
//
// Modelled: 7-bit addressing; START / repeated START / STOP; NBYTES with
// RELOAD, AUTOEND and software end (TC/TCR); TXDR written before START (the
// HAL's prefetched memory address); TXIS, RXNE, NACKF, STOPF, TC, TCR, BUSY
// and their interrupts; RX by DMA (RXDMAEN: a request edge per received
// byte, DmaReceive, for the DMAMUX).
// Kept but without effect: TIMINGR (bus timing: a transfer takes no virtual
// time), the digital/analog noise filters (no electrical bus), the
// own-address registers and CR2.NACK (slave side: no other master on the bus
// ever addresses this one).
// Not modelled (tags, so the peripheral guard sees a firmware use them):
// slave mode, 10-bit addressing, TX by DMA, SMBus/PEC, wake-up, and the bus
// errors BERR/ARLO/OVR/TIMEOUT, which an emulated bus never produces.
//
// Register offsets from ST's stm32h733xx.h (I2C_TypeDef: CR1 0x00, CR2 0x04,
// OAR1 0x08, OAR2 0x0C, TIMINGR 0x10, TIMEOUTR 0x14, ISR 0x18, ICR 0x1C,
// PECR 0x20, RXDR 0x24, TXDR 0x28); bit positions from its I2C_CR1_* /
// I2C_CR2_* / I2C_ISR_* / I2C_ICR_* definitions.
//
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.I2C
{
    // A target told about each START or repeated START addressed to it. It
    // returns false to leave the address unacknowledged (a NACK). A plain
    // II2CPeripheral always acknowledges.
    public interface II2CStartCondition
    {
        bool OnStart(bool read);
    }

    // The DMA reads RXDR a byte at a time (PSIZE byte, the AMS's
    // stm32h7xx_hal_msp.c I2C2_RX init).
    [AllowedTranslations(AllowedTranslation.ByteToDoubleWord)]
    public class STM32H7_I2C : SimpleContainer<II2CPeripheral>, IDoubleWordPeripheral, IKnownSize
    {
        public STM32H7_I2C(IMachine machine) : base(machine)
        {
            EventInterrupt = new GPIO();
            ErrorInterrupt = new GPIO();
            DmaReceive = new GPIO();
            registers = new DoubleWordRegisterCollection(this, BuildRegisters());
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            return registers.Read(offset);
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            registers.Write(offset, value);
        }

        public override void Reset()
        {
            registers.Reset();
            Abort();
            EventInterrupt.Unset();
            ErrorInterrupt.Unset();
            DmaReceive.Unset();
        }

        public long Size => 0x400;

        public GPIO EventInterrupt { get; }

        // Bus errors are never raised (see the header); wired for completeness.
        public GPIO ErrorInterrupt { get; }

        public GPIO DmaReceive { get; }

        // Addresses that went unacknowledged since reset, for tests.
        public int NackCount { get; private set; }

        private Dictionary<long, DoubleWordRegister> BuildRegisters()
        {
            return new Dictionary<long, DoubleWordRegister>
            {
                {(long)Registers.Control1, new DoubleWordRegister(this)
                    .WithFlag(0, out peripheralEnable, name: "PE",
                        writeCallback: (_, value) => { if(!value) Abort(); })
                    .WithFlag(1, out txInterruptEnable, name: "TXIE")
                    .WithFlag(2, out rxInterruptEnable, name: "RXIE")
                    .WithTaggedFlag("ADDRIE", 3)
                    .WithFlag(4, out nackInterruptEnable, name: "NACKIE")
                    .WithFlag(5, out stopInterruptEnable, name: "STOPIE")
                    .WithFlag(6, out transferCompleteInterruptEnable, name: "TCIE")
                    .WithFlag(7, name: "ERRIE")
                    .WithValueField(8, 4, name: "DNF")
                    .WithFlag(12, name: "ANFOFF")
                    .WithReservedBits(13, 1)
                    .WithTaggedFlag("TXDMAEN", 14)
                    .WithFlag(15, out rxDmaEnable, name: "RXDMAEN")
                    .WithTaggedFlag("SBC", 16)
                    .WithTaggedFlag("NOSTRETCH", 17)
                    .WithTaggedFlag("WUPEN", 18)
                    .WithTaggedFlag("GCEN", 19)
                    .WithTaggedFlag("SMBHEN", 20)
                    .WithTaggedFlag("SMBDEN", 21)
                    .WithTaggedFlag("ALERTEN", 22)
                    .WithTaggedFlag("PECEN", 23)
                    .WithReservedBits(24, 8)
                    .WithWriteCallback((_, __) => Update())},

                {(long)Registers.Control2, new DoubleWordRegister(this)
                    .WithValueField(0, 10, out slaveAddress, name: "SADD")
                    .WithFlag(10, out readTransfer, name: "RD_WRN")
                    .WithTaggedFlag("ADD10", 11)
                    .WithTaggedFlag("HEAD10R", 12)
                    .WithFlag(13, out start, name: "START")
                    .WithFlag(14, out stop, name: "STOP")
                    .WithFlag(15, name: "NACK")
                    .WithValueField(16, 8, out numberOfBytes, name: "NBYTES")
                    .WithFlag(24, out reload, name: "RELOAD")
                    .WithFlag(25, out autoEnd, name: "AUTOEND")
                    .WithTaggedFlag("PECBYTE", 26)
                    .WithReservedBits(27, 5)
                    .WithWriteCallback((_, __) => Control2Written())},

                {(long)Registers.OwnAddress1, new DoubleWordRegister(this)
                    .WithValueField(0, 10, name: "OA1")
                    .WithFlag(10, name: "OA1MODE")
                    .WithReservedBits(11, 4)
                    .WithFlag(15, name: "OA1EN")
                    .WithReservedBits(16, 16)},

                {(long)Registers.OwnAddress2, new DoubleWordRegister(this)
                    .WithReservedBits(0, 1)
                    .WithValueField(1, 7, name: "OA2")
                    .WithValueField(8, 3, name: "OA2MSK")
                    .WithReservedBits(11, 4)
                    .WithFlag(15, name: "OA2EN")
                    .WithReservedBits(16, 16)},

                {(long)Registers.Timing, new DoubleWordRegister(this)
                    .WithValueField(0, 8, name: "SCLL")
                    .WithValueField(8, 8, name: "SCLH")
                    .WithValueField(16, 4, name: "SDADEL")
                    .WithValueField(20, 4, name: "SCLDEL")
                    .WithReservedBits(24, 4)
                    .WithValueField(28, 4, name: "PRESC")},

                {(long)Registers.Timeout, new DoubleWordRegister(this)
                    .WithTag("TIMEOUTA", 0, 12)
                    .WithTaggedFlag("TIDLE", 12)
                    .WithReservedBits(13, 2)
                    .WithTaggedFlag("TIMOUTEN", 15)
                    .WithTag("TIMEOUTB", 16, 12)
                    .WithReservedBits(28, 3)
                    .WithTaggedFlag("TEXTEN", 31)},

                {(long)Registers.InterruptAndStatus, new DoubleWordRegister(this, 0x1)
                    // TXE: TXDR empty. Writing 1 flushes it (I2C_Flush_TXDR).
                    .WithFlag(0, name: "TXE", valueProviderCallback: _ => !txHeld.HasValue,
                        writeCallback: (_, value) => { if(value) txHeld = null; })
                    // Settable by software only with NOSTRETCH (slave mode).
                    .WithFlag(1, FieldMode.Read, name: "TXIS", valueProviderCallback: _ => txInterruptStatus)
                    .WithFlag(2, FieldMode.Read, name: "RXNE", valueProviderCallback: _ => rxData.Count > 0)
                    .WithFlag(3, FieldMode.Read, name: "ADDR", valueProviderCallback: _ => false)
                    .WithFlag(4, FieldMode.Read, name: "NACKF", valueProviderCallback: _ => nackReceived)
                    .WithFlag(5, FieldMode.Read, name: "STOPF", valueProviderCallback: _ => stopDetected)
                    .WithFlag(6, FieldMode.Read, name: "TC", valueProviderCallback: _ => transferComplete)
                    .WithFlag(7, FieldMode.Read, name: "TCR", valueProviderCallback: _ => transferCompleteReload)
                    // Bus errors: an emulated bus has none.
                    .WithFlag(8, FieldMode.Read, name: "BERR", valueProviderCallback: _ => false)
                    .WithFlag(9, FieldMode.Read, name: "ARLO", valueProviderCallback: _ => false)
                    .WithFlag(10, FieldMode.Read, name: "OVR", valueProviderCallback: _ => false)
                    .WithFlag(11, FieldMode.Read, name: "PECERR", valueProviderCallback: _ => false)
                    .WithFlag(12, FieldMode.Read, name: "TIMEOUT", valueProviderCallback: _ => false)
                    .WithFlag(13, FieldMode.Read, name: "ALERT", valueProviderCallback: _ => false)
                    .WithReservedBits(14, 1)
                    .WithFlag(15, FieldMode.Read, name: "BUSY", valueProviderCallback: _ => busy)
                    .WithFlag(16, FieldMode.Read, name: "DIR", valueProviderCallback: _ => false)
                    .WithValueField(17, 7, FieldMode.Read, name: "ADDCODE", valueProviderCallback: _ => 0)
                    .WithReservedBits(24, 8)},

                {(long)Registers.InterruptClear, new DoubleWordRegister(this)
                    .WithReservedBits(0, 3)
                    .WithFlag(3, FieldMode.WriteOneToClear, name: "ADDRCF")
                    .WithFlag(4, FieldMode.WriteOneToClear, name: "NACKCF",
                        writeCallback: (_, value) => { if(value) nackReceived = false; })
                    .WithFlag(5, FieldMode.WriteOneToClear, name: "STOPCF",
                        writeCallback: (_, value) => { if(value) stopDetected = false; })
                    .WithReservedBits(6, 2)
                    // Clears of flags the model never raises.
                    .WithFlag(8, FieldMode.WriteOneToClear, name: "BERRCF")
                    .WithFlag(9, FieldMode.WriteOneToClear, name: "ARLOCF")
                    .WithFlag(10, FieldMode.WriteOneToClear, name: "OVRCF")
                    .WithFlag(11, FieldMode.WriteOneToClear, name: "PECCF")
                    .WithFlag(12, FieldMode.WriteOneToClear, name: "TIMOUTCF")
                    .WithFlag(13, FieldMode.WriteOneToClear, name: "ALERTCF")
                    .WithReservedBits(14, 18)
                    .WithWriteCallback((_, __) => Update())},

                {(long)Registers.PacketErrorChecking, new DoubleWordRegister(this)
                    .WithTag("PEC", 0, 8)
                    .WithReservedBits(8, 24)},

                {(long)Registers.ReceiveData, new DoubleWordRegister(this)
                    .WithValueField(0, 8, FieldMode.Read, name: "RXDATA", valueProviderCallback: _ => ReceiveDataRead())
                    .WithReservedBits(8, 24)},

                {(long)Registers.TransmitData, new DoubleWordRegister(this)
                    .WithValueField(0, 8, FieldMode.Write, name: "TXDATA",
                        writeCallback: (_, value) => TransmitDataWritten((byte)value))
                    .WithReservedBits(8, 24)},
            };
        }

        private void Control2Written()
        {
            if(start.Value && stop.Value)
            {
                this.WarningLog("START and STOP set together; ignoring both");
            }
            else if(start.Value)
            {
                Start();
            }
            else if(stop.Value)
            {
                if(busy)
                {
                    Stop();
                }
            }
            else if(busy && transferCompleteReload && numberOfBytes.Value > 0)
            {
                // NBYTES written after TCR: the next chunk of the same transfer.
                transferCompleteReload = false;
                BeginSegment();
            }
            // The hardware clears START once the address is out, STOP once
            // the STOP condition is.
            start.Value = false;
            stop.Value = false;
            Update();
        }

        private void Start()
        {
            if(!peripheralEnable.Value)
            {
                this.WarningLog("START with PE clear; ignored");
                return;
            }
            transferComplete = false;
            transferCompleteReload = false;
            txInterruptStatus = false;
            rxData.Clear();
            var address = (int)((slaveAddress.Value >> 1) & 0x7F);
            var read = readTransfer.Value;
            II2CPeripheral found;
            var acknowledged = TryGetByAddress(address, out found)
                && (!(found is II2CStartCondition aware) || aware.OnStart(read));
            if(!acknowledged)
            {
                // NACK on the address: NACKF, and the master sends a STOP by
                // itself whatever AUTOEND says.
                this.DebugLog("address 0x{0:X2} ({1}) not acknowledged", address, read ? "read" : "write");
                NackCount++;
                nackReceived = true;
                if(busy)
                {
                    // A repeated START the target refused: end its transaction.
                    target?.FinishTransmission();
                }
                target = null;
                busy = false;
                stopDetected = true;
                return;
            }
            if(busy && target != null && target != found)
            {
                // A repeated START to someone else deselects the last target.
                target.FinishTransmission();
            }
            target = found;
            busy = true;
            BeginSegment();
        }

        // Start moving NBYTES in the current direction.
        private void BeginSegment()
        {
            remaining = (int)numberOfBytes.Value;
            if(readTransfer.Value)
            {
                if(remaining == 0)
                {
                    SegmentDone();
                    return;
                }
                var data = target.Read(remaining);
                for(var i = 0; i < remaining; i++)
                {
                    // A target that runs out leaves SDA released: 0xFF.
                    rxData.Enqueue(data != null && i < data.Length ? data[i] : (byte)0xFF);
                }
                remaining = 0;
                rxSegmentOpen = true;
                return;
            }
            if(remaining == 0)
            {
                SegmentDone();
                return;
            }
            if(txHeld.HasValue)
            {
                // TXDR was written before START: that byte goes first.
                var b = txHeld.Value;
                txHeld = null;
                SendByte(b);
            }
            else
            {
                txInterruptStatus = true;
            }
        }

        private void TransmitDataWritten(byte value)
        {
            if(busy && !readTransfer.Value && txInterruptStatus && remaining > 0)
            {
                SendByte(value);
            }
            else
            {
                txHeld = value;
                txInterruptStatus = false;
            }
            Update();
        }

        private void SendByte(byte value)
        {
            target.Write(new[] { value });
            remaining--;
            txInterruptStatus = remaining > 0;
            if(remaining == 0)
            {
                SegmentDone();
            }
        }

        private uint ReceiveDataRead()
        {
            // One DMA request per byte: the request falls as the byte is
            // taken and rises again (Update) if another is waiting.
            DmaReceive.Unset();
            if(rxData.Count == 0)
            {
                this.WarningLog("RXDR read with nothing received");
                return 0;
            }
            var value = rxData.Dequeue();
            if(rxData.Count == 0 && rxSegmentOpen)
            {
                rxSegmentOpen = false;
                SegmentDone();
            }
            Update();
            return value;
        }

        // NBYTES moved: reload, automatic STOP, or wait for software (TC).
        private void SegmentDone()
        {
            txInterruptStatus = false;
            if(reload.Value)
            {
                transferCompleteReload = true;
            }
            else if(autoEnd.Value)
            {
                Stop();
            }
            else
            {
                transferComplete = true;
            }
        }

        private void Stop()
        {
            target?.FinishTransmission();
            target = null;
            busy = false;
            transferComplete = false;
            txInterruptStatus = false;
            stopDetected = true;
        }

        // PE cleared (a software reset) or a machine reset: release the bus
        // and drop every flag and byte in flight.
        private void Abort()
        {
            if(busy)
            {
                target?.FinishTransmission();
            }
            target = null;
            busy = false;
            remaining = 0;
            rxSegmentOpen = false;
            rxData.Clear();
            txHeld = null;
            txInterruptStatus = false;
            nackReceived = false;
            stopDetected = false;
            transferComplete = false;
            transferCompleteReload = false;
        }

        private void Update()
        {
            var irq = (txInterruptEnable.Value && txInterruptStatus)
                || (rxInterruptEnable.Value && rxData.Count > 0)
                || (nackInterruptEnable.Value && nackReceived)
                || (stopInterruptEnable.Value && stopDetected)
                || (transferCompleteInterruptEnable.Value && (transferComplete || transferCompleteReload));
            EventInterrupt.Set(irq);
            DmaReceive.Set(rxDmaEnable.Value && rxData.Count > 0);
        }

        private II2CPeripheral target;
        private bool busy;
        private int remaining;
        private bool rxSegmentOpen;
        private readonly Queue<byte> rxData = new Queue<byte>();
        private byte? txHeld;
        private bool txInterruptStatus;
        private bool nackReceived;
        private bool stopDetected;
        private bool transferComplete;
        private bool transferCompleteReload;

        private IFlagRegisterField peripheralEnable;
        private IFlagRegisterField txInterruptEnable;
        private IFlagRegisterField rxInterruptEnable;
        private IFlagRegisterField nackInterruptEnable;
        private IFlagRegisterField stopInterruptEnable;
        private IFlagRegisterField transferCompleteInterruptEnable;
        private IFlagRegisterField rxDmaEnable;
        private IValueRegisterField slaveAddress;
        private IFlagRegisterField readTransfer;
        private IFlagRegisterField start;
        private IFlagRegisterField stop;
        private IValueRegisterField numberOfBytes;
        private IFlagRegisterField reload;
        private IFlagRegisterField autoEnd;

        private readonly DoubleWordRegisterCollection registers;

        private enum Registers
        {
            Control1 = 0x00,
            Control2 = 0x04,
            OwnAddress1 = 0x08,
            OwnAddress2 = 0x0C,
            Timing = 0x10,
            Timeout = 0x14,
            InterruptAndStatus = 0x18,
            InterruptClear = 0x1C,
            PacketErrorChecking = 0x20,
            ReceiveData = 0x24,
            TransmitData = 0x28,
        }
    }
}
