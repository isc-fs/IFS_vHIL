//
// SPI of the STM32H7 with a fault hook, compiled by Renode at load time and
// placed by platforms/cpus/stm32h733.repl in place of Renode's STM32H7_SPI.
// Renode's model unchanged until the hook is set.
//
// Renode's STM32H7_SPI never fails a transfer: TXP always reads 1, every
// byte reaches the slave the moment it is written, and UDR/OVR/MODF are
// tags. The HAL's error paths (stm32h7xx_hal_spi.c, SPI_CloseTransfer and the
// timeout branches of HAL_SPI_Transmit / HAL_SPI_TransmitReceive) and the
// firmware's reaction to a failed isoSPI transfer were unreachable. This adds
// the failure a master on the car can actually see:
//
//   Stall / StallNextTransfers n
//                      The transfer never runs: SCK never toggles, so nothing
//                      reaches the slave and SR.EOT never sets (RM0468, "SPI
//                      master transfer": EOT is set once TSIZE data frames
//                      have been shifted). Data written to TXDR stays in the
//                      TxFIFO; once it holds FifoBytes, SR.TXP reads 0. RXP and
//                      DXP read 0 (nothing was received), TXC 0. The HAL waits
//                      out its timeout and returns HAL_TIMEOUT (TXP or RXP
//                      never comes) or HAL_ERROR (EOT never comes,
//                      HAL_SPI_ERROR_FLAG). Clearing SPE (SPI_CloseTransfer)
//                      ends the transfer and flushes the FIFO, as on the chip.
//                      A slave framed by a GPIO chip select sees the select
//                      toggle with no clocks in between: for the LTC6820 that
//                      is a wake pulse (models/renode/IsoSpi.cs). `Stall`
//                      stalls every transfer while set; `StallNextTransfers n`
//                      the next n to start (CR1.CSTART with SPE set), so a
//                      test can aim one at a given command (tests/sim, a CPU
//                      hook on HAL_SPI_Transmit arming it).
//
// Not modelled, because the AMS (the only SPI1 user) cannot meet them:
//   MODF  needs the NSS input driven low while master; AMS MX_SPI1_Init uses
//         SPI_NSS_SOFT (IFS08-CE-AMS main.c:622), SSI held high.
//   OVR   the HAL's polled full-duplex loop never lets the TxFIFO run more
//         than one FIFO ahead of what it has read (HAL_SPI_TransmitReceive,
//         `initial_RxXferCount < initial_TxXferCount + fifo_length`), and
//         a master only clocks what is in its TxFIFO, so the RxFIFO cannot
//         overflow. The HAL also ignores OVR on a TX-only transfer.
//   UDR   a slave-mode error.
//
// Writes to bits Renode's model tags: the HAL writes CFG2.COMM on every
// transmit and IFCR.TXTFC on every transfer's close (SPI_CloseTransfer), and
// Renode's register framework logs a warning for each, formatted on the CPU's
// thread (half the AMS's log lines, #260). A tagged bit is neither stored nor
// acted on (PeripheralRegister.WriteInner: only defined fields take a write;
// the warning is the whole effect), so after the first write of a given
// pattern at an offset, which Renode logs and the peripheral guard
// (vhil/peripheral_guard.py) checks against configs/peripherals.yaml, the
// same pattern goes in with the tagged bits as the register already holds
// them: no warning, the same register state.
//
using System;
using System.Collections.Generic;
using System.Linq.Expressions;
using System.Reflection;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    public class STM32H7_SPIWithFaults : STM32H7_SPI, IDoubleWordPeripheral, IWordPeripheral, IBytePeripheral
    {
        public STM32H7_SPIWithFaults(IMachine machine) : base(machine)
        {
        }

        public override void Reset()
        {
            base.Reset();
            stalled = false;
            queuedBytes = 0;
            // Stall and StallNextTransfers are the fault, not the chip's
            // state: they survive the MCU's reset.
        }

        // -- test API (monitor: `sysbus.spi1 StallNextTransfers 1`) ------------

        public bool Stall { get; set; }

        public void StallNextTransfers(int n)
        {
            pendingStalls = Math.Max(0, n);
        }

        public int PendingStalls => pendingStalls;

        // Transfers that never ran, since the platform loaded.
        public int StalledTransfers => stalledTransfers;

        // -- registers ---------------------------------------------------------

        public new uint ReadDoubleWord(long offset)
        {
            var value = base.ReadDoubleWord(offset);
            if(offset == Sr && stalled)
            {
                value &= ~(SrRxp | SrDxp | SrEot | SrTxc);
                if(queuedBytes >= FifoBytes)
                {
                    value &= ~SrTxp;
                }
            }
            return value;
        }

        public new void WriteDoubleWord(long offset, uint value)
        {
            Write(offset, value, 4);
        }

        public new ushort ReadWord(long offset)
        {
            return (ushort)ReadDoubleWord(offset);
        }

        public new void WriteWord(long offset, ushort value)
        {
            Write(offset, value, 2);
        }

        public new byte ReadByte(long offset)
        {
            return (byte)ReadDoubleWord(offset);
        }

        public new void WriteByte(long offset, byte value)
        {
            Write(offset, value, 1);
        }

        private void Write(long offset, uint value, int width)
        {
            if(offset == Txdr && stalled)
            {
                // SCK never runs: the frame stays in the TxFIFO.
                queuedBytes = Math.Min(FifoBytes, queuedBytes + width);
                return;
            }
            if(offset == Cr1)
            {
                if((value & Cr1Spe) == 0)
                {
                    // SPE cleared: the transfer is over, the FIFOs flushed.
                    stalled = false;
                    queuedBytes = 0;
                }
                else if((value & Cr1Cstart) != 0 && !stalled && (Stall || pendingStalls > 0))
                {
                    if(!Stall)
                    {
                        pendingStalls--;
                    }
                    stalled = true;
                    queuedBytes = 0;
                    stalledTransfers++;
                    this.Log(LogLevel.Info, "Transfer stalled (fault hook)");
                    return;   // CSTART never takes: no transfer starts
                }
            }
            if(offset == Cfg2 || offset == Ifcr)
            {
                value = QuietTags(offset, value);
            }
            base.WriteDoubleWord(offset, value);
        }

        // The value to write so that Renode logs a tagged-bit write only the
        // first time (see the header). (underlying ^ value) & ~defined is what
        // Renode reports as unhandled.
        private uint QuietTags(long offset, uint value)
        {
            if(!tagged.TryGetValue(offset, out var register))
            {
                register = TaggedRegister.Of(this, offset);
                tagged[offset] = register;
            }
            if(register == null)
            {
                return value;
            }
            var underlying = register.Underlying();
            var unhandled = (underlying ^ value) & ~register.Defined;
            if(unhandled == 0 || logged.Add(((ulong)offset << 32) | (unhandled & 0xFFFFFFFF)))
            {
                return value;
            }
            return (uint)((value & register.Defined) | (underlying & ~register.Defined));
        }

        // One register of Renode's model: its defined-fields mask and current
        // value (PeripheralRegister.definedFieldsMask, UnderlyingValue), read
        // by reflection; null if this Renode keeps them otherwise.
        private sealed class TaggedRegister
        {
            public static TaggedRegister Of(STM32H7_SPI spi, long offset)
            {
                try
                {
                    var flags = BindingFlags.Instance | BindingFlags.NonPublic;
                    var collection = typeof(STM32H7_SPI).GetField("registers", flags)?.GetValue(spi);
                    var map = collection?.GetType().BaseType?.GetField("registers", flags)?.GetValue(collection)
                        as System.Collections.IDictionary;
                    var selector = map?[offset];
                    var register = selector?.GetType().GetMethod("GetRegister", flags)?.Invoke(selector, null);
                    var definedField = typeof(PeripheralRegister).GetField("definedFieldsMask", flags);
                    var underlyingField = typeof(PeripheralRegister).GetField("UnderlyingValue", flags);
                    if(!(register is PeripheralRegister) || definedField == null || underlyingField == null)
                    {
                        return null;
                    }
                    var target = Expression.Constant(register);
                    var read = Expression.Lambda<Func<ulong>>(
                        Expression.Field(Expression.Convert(target, typeof(PeripheralRegister)), underlyingField)).Compile();
                    return new TaggedRegister { Defined = (ulong)definedField.GetValue(register), Underlying = read };
                }
                catch(Exception)
                {
                    return null;
                }
            }

            public ulong Defined;
            public Func<ulong> Underlying;
        }

        private readonly Dictionary<long, TaggedRegister> tagged = new Dictionary<long, TaggedRegister>();
        private readonly HashSet<ulong> logged = new HashSet<ulong>();

        private bool stalled;
        private int queuedBytes;
        private int pendingStalls;
        private int stalledTransfers;

        // SPI1..3 have a 16 x 8-bit FIFO (RM0468, "SPI implementation").
        private const int FifoBytes = 16;

        private const long Cr1 = 0x00;
        private const long Cfg2 = 0x0C;
        private const long Ifcr = 0x18;
        private const long Sr = 0x14;
        private const long Txdr = 0x20;
        private const uint Cr1Spe = 1u << 0;
        private const uint Cr1Cstart = 1u << 9;
        private const uint SrRxp = 1u << 0;
        private const uint SrTxp = 1u << 1;
        private const uint SrDxp = 1u << 2;
        private const uint SrEot = 1u << 3;
        private const uint SrTxc = 1u << 12;
    }
}
