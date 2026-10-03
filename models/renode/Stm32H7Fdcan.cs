//
// FDCAN of the STM32H7 with fault hooks, compiled by Renode at load time and
// placed by platforms/cpus/stm32h733.repl in place of the plain MCANs.
//
// Renode's MCAN has no error state machine: no TEC/REC, no bus-off, and it
// transmits whether or not CCCR.INIT is set. The error physics stays on the
// physical bench; what a test can check here is the firmware's reaction, so
// this forces the states the firmware observes:
//
//   ForceBusOff        PSR.BO/EP/EW read 1 and CCCR.INIT is set, as the
//                      controller does on entering bus-off. TX requests stay
//                      pending, as INIT holds them: they fill the TX FIFO
//                      (TXFQS put index, free level and TFQF move as they
//                      would) until it is full. When the firmware clears
//                      INIT, the bus-off recovery sequence completes (129 x 11
//                      recessive bits; here in no virtual time): BO clears,
//                      requests still pending go out in order, and
//                      BusOffRecoveries counts it. Setting CCCR.CCE cancels
//                      pending requests (as on the chip, and as HAL_FDCAN_Stop
//                      does), so those are dropped instead.
//   TxFifoFull         TXFQS reads TFQF = 1 and TFFL = 0 while set, so the
//                      HAL refuses to queue (HAL_FDCAN_AddMessageToTxFifoQ).
//   FailInit           CCCR.INIT never reads back as set while set, so
//                      HAL_FDCAN_Init times out waiting for it: a failed
//                      bring-up. Survives Reset, so it can apply from boot.
//
// Offsets and bits from ST's stm32h733xx.h: CCCR 0x018 (INIT 0, CCE 1), PSR
// 0x044 (EP 5, EW 6, BO 7), TXBC 0x0C0 (NDTB [21:16], TFQS [29:24]), TXFQS
// 0x0C4 (TFFL [5:0], TFQPI [20:16], TFQF 21), TXBAR 0x0D0.
// With no hook set, this is Renode's MCAN unchanged.
//
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.CAN
{
    public class STM32H7_FDCAN : MCAN, IDoubleWordPeripheral
    {
        public STM32H7_FDCAN(IMachine machine, IMultibyteWritePeripheral messageRAM)
            : base(machine, messageRAM)
        {
        }

        // -- test API (monitor) ------------------------------------------------

        public void ForceBusOff()
        {
            busOff = true;
            base.WriteDoubleWord(Cccr, base.ReadDoubleWord(Cccr) | CccrInit);
            this.Log(LogLevel.Info, "Forced bus-off");
        }

        public bool BusOff => busOff;

        public uint BusOffRecoveries { get; private set; }

        public bool TxFifoFull { get; set; }

        public bool FailInit { get; set; }

        // -- registers ---------------------------------------------------------

        public new uint ReadDoubleWord(long offset)
        {
            var value = base.ReadDoubleWord(offset);
            switch(offset)
            {
            case Psr:
                if(busOff)
                {
                    value |= PsrBo | PsrEp | PsrEw;
                }
                break;
            case Txfqs:
                if(TxFifoFull)
                {
                    value = (value & ~TxfqsTffl) | TxfqsTfqf;
                }
                else if(held.Count > 0)
                {
                    value = WithHeld(value);
                }
                break;
            case Cccr:
                if(FailInit)
                {
                    value &= ~CccrInit;
                }
                break;
            }
            return value;
        }

        public new void WriteDoubleWord(long offset, uint value)
        {
            if(offset == Txbar && busOff)
            {
                for(var i = 0; i < 32; i++)
                {
                    if((value & (1u << i)) != 0)
                    {
                        held.Enqueue(i);
                    }
                }
                return;
            }
            var wasInit = (base.ReadDoubleWord(Cccr) & CccrInit) != 0;
            if(offset == Cccr && (value & CccrCce) != 0)
            {
                held.Clear();
            }
            base.WriteDoubleWord(offset, value);
            if(offset == Cccr && busOff && wasInit && (value & CccrInit) == 0)
            {
                busOff = false;
                BusOffRecoveries++;
                this.Log(LogLevel.Info, "Bus-off recovered (INIT cleared)");
                while(held.Count > 0)
                {
                    base.WriteDoubleWord(Txbar, 1u << held.Dequeue());
                }
            }
        }

        public new void Reset()
        {
            base.Reset();
            busOff = false;
            held.Clear();
            TxFifoFull = false;
            // FailInit and BusOffRecoveries are the test's: they survive.
        }

        // TXFQS as if the held requests were in the FIFO: the put index and
        // free level move on, and the FIFO reads full once they fill it.
        private uint WithHeld(uint value)
        {
            var txbc = base.ReadDoubleWord(Txbc);
            var dedicated = (txbc >> 16) & 0x3F;
            var size = (txbc >> 24) & 0x3F;
            if(size == 0)
            {
                return value;
            }
            var free = (uint)System.Math.Max(0, (int)(value & TxfqsTffl) - held.Count);
            var put = dedicated + (((value >> 16) & 0x1F) - dedicated + (uint)held.Count) % size;
            value = (value & ~(TxfqsTffl | TxfqsTfqpi | TxfqsTfqf)) | free | (put << 16);
            return free == 0 ? value | TxfqsTfqf : value;
        }

        private bool busOff;
        private readonly Queue<int> held = new Queue<int>();

        private const long Cccr = 0x018;
        private const long Psr = 0x044;
        private const long Txbc = 0x0C0;
        private const long Txfqs = 0x0C4;
        private const long Txbar = 0x0D0;
        private const uint CccrInit = 1u << 0;
        private const uint CccrCce = 1u << 1;
        private const uint PsrEp = 1u << 5;
        private const uint PsrEw = 1u << 6;
        private const uint PsrBo = 1u << 7;
        private const uint TxfqsTffl = 0x3Fu;
        private const uint TxfqsTfqpi = 0x1Fu << 16;
        private const uint TxfqsTfqf = 1u << 21;
    }
}
