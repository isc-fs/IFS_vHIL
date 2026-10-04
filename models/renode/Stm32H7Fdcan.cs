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
//   WireTiming         Renode's MCAN transmits a request the instant TXBAR is
//                      written, so its TX FIFO never holds anything and can't
//                      overflow. With this set, requests leave one frame time
//                      apart, as on an idle bus: each holds its FIFO element
//                      until its last bit is out (the free level and TFQF move
//                      as they would), so a burst queued faster than the bus
//                      drains fills the FIFO. Frame time = (47 + 8 x DLC) bits
//                      for a standard ID, 67 + 8 x DLC for an extended one
//                      (classic frame incl. intermission, no stuff bits: the
//                      fastest legal frame, so the model never drops more than
//                      the car), at the nominal bit rate NBTP sets from the
//                      24 MHz kernel clock (HSE, invariant 2; both firmwares
//                      select RCC_FDCANCLKSOURCE_HSE). No arbitration: another
//                      node never delays us. Survives Reset (it is the bus).
//
// NBTP is held here (Renode's MCAN drops writes to it): writable only while
// CCCR.INIT and CCE are both set, reset value 0x06000A03 (RM0468, FDCAN_NBTP;
// what Renode's MCAN reads back). Only WireTiming uses it; without it, bit
// timing changes nothing a test sees.
//
// Offsets and bits from ST's stm32h733xx.h: CCCR 0x018 (INIT 0, CCE 1), NBTP
// 0x01C (NBRP [24:16], NTSEG1 [15:8], NTSEG2 [6:0]), PSR 0x044 (EP 5, EW 6,
// BO 7), TXBC 0x0C0 (TBSA [15:2], NDTB [21:16], TFQS [29:24]), TXFQS 0x0C4
// (TFFL [5:0], TFQPI [20:16], TFQF 21), TXESC 0x0C8 (TBDS [2:0]), TXBAR
// 0x0D0; TBSA is a byte offset into the message RAM. Tx buffer element
// header (RM0468, FDCAN Tx buffer element): T0 XTD bit 30, T1 DLC [19:16].
// With no hook set, this is Renode's MCAN unchanged but for NBTP.
//
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.CAN
{
    public class STM32H7_FDCAN : MCAN, IDoubleWordPeripheral
    {
        public STM32H7_FDCAN(IMachine machine, IMultibyteWritePeripheral messageRAM)
            : base(machine, messageRAM)
        {
            ram = messageRAM as IDoubleWordPeripheral;
            wireTimer = new LimitTimer(machine.ClockSource, KernelClockHz, this, "wire", limit: 1,
                                       direction: Direction.Ascending, enabled: false,
                                       workMode: WorkMode.OneShot, eventEnabled: true);
            wireTimer.LimitReached += OnFrameSent;
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

        public bool WireTiming { get; set; }

        // -- registers ---------------------------------------------------------

        public new uint ReadDoubleWord(long offset)
        {
            if(offset == Nbtp)
            {
                return nbtp;
            }
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
                else if(held.Count + wire.Count > 0)
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
            if(offset == Nbtp)
            {
                if((base.ReadDoubleWord(Cccr) & (CccrInit | CccrCce)) == (CccrInit | CccrCce))
                {
                    nbtp = value;
                }
                return;
            }
            if(offset == Txbar && WireTiming && ram != null)
            {
                for(var i = 0; i < 32; i++)
                {
                    if((value & (1u << i)) != 0)
                    {
                        wire.Enqueue(i);
                    }
                }
                if(!wireTimer.Enabled)
                {
                    StartFrame();
                }
                return;
            }
            var wasInit = (base.ReadDoubleWord(Cccr) & CccrInit) != 0;
            if(offset == Cccr && (value & CccrCce) != 0)
            {
                held.Clear();
                wire.Clear();
                wireTimer.Enabled = false;
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
            wire.Clear();
            wireTimer.Reset();
            nbtp = NbtpReset;
            TxFifoFull = false;
            // FailInit, WireTiming and BusOffRecoveries are the test's: they survive.
        }

        // The head request is on the wire: it leaves after its frame time.
        private void StartFrame()
        {
            if(wire.Count == 0)
            {
                return;
            }
            var prescaler = ((nbtp >> 16) & 0x1FF) + 1;
            var tqPerBit = 1 + ((nbtp >> 8) & 0xFF) + 1 + (nbtp & 0x7F) + 1;
            wireTimer.Value = 0;
            wireTimer.Limit = FrameBits(wire.Peek()) * prescaler * tqPerBit;
            wireTimer.Enabled = true;
        }

        private void OnFrameSent()
        {
            if(wire.Count == 0)
            {
                return;
            }
            base.WriteDoubleWord(Txbar, 1u << wire.Dequeue());
            StartFrame();
        }

        // Bits of a classic frame from TX buffer element `index`: header T0/T1.
        private ulong FrameBits(int index)
        {
            var txbc = base.ReadDoubleWord(Txbc);
            var dataBytes = new uint[] { 8, 12, 16, 20, 24, 32, 48, 64 }[base.ReadDoubleWord(Txesc) & 0x7];
            var element = (long)(txbc & 0xFFFC) + index * (8 + dataBytes);
            var t0 = ram.ReadDoubleWord(element);
            var dlc = System.Math.Min((ram.ReadDoubleWord(element + 4) >> 16) & 0xF, 8u);
            return ((t0 & (1u << 30)) != 0 ? 67u : 47u) + 8u * dlc;
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
            var pending = held.Count + wire.Count;
            var free = (uint)System.Math.Max(0, (int)(value & TxfqsTffl) - pending);
            var put = dedicated + (((value >> 16) & 0x1F) - dedicated + (uint)pending) % size;
            value = (value & ~(TxfqsTffl | TxfqsTfqpi | TxfqsTfqf)) | free | (put << 16);
            return free == 0 ? value | TxfqsTfqf : value;
        }

        private bool busOff;
        private uint nbtp = NbtpReset;
        private readonly Queue<int> held = new Queue<int>();
        private readonly Queue<int> wire = new Queue<int>();
        private readonly LimitTimer wireTimer;
        private readonly IDoubleWordPeripheral ram;

        private const long KernelClockHz = 24000000;
        private const long Cccr = 0x018;
        private const long Nbtp = 0x01C;
        private const uint NbtpReset = 0x06000A03;
        private const long Psr = 0x044;
        private const long Txbc = 0x0C0;
        private const long Txfqs = 0x0C4;
        private const long Txesc = 0x0C8;
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
