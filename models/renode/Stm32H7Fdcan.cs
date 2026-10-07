//
// FDCAN of the STM32H7 with fault hooks, compiled by Renode at load time and
// placed by platforms/cpus/stm32h733.repl in place of the plain MCANs.
//
// Renode's MCAN has no error state machine: no TEC/REC, no bus-off, and it
// transmits whether or not CCCR.INIT is set. On a bus with Renode's CAN hub
// (the default) the error physics stays on the physical bench; what a test can
// check is the firmware's reaction, so this forces the states the firmware
// observes:
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
//                      On an arbitrated bus it changes nothing: the bus times
//                      every frame (below).
//
// On a bus with `arbitration: true` (models/renode/VhilCanBus.cs, #174) the
// controller joins the bus's arbiter instead, through IVhilCanController
// below, and the bus decides when each request goes out:
//
//   - The transmit side is this model's, not Renode's MCAN's: TXBAR requests
//     are held here, stamped with the virtual time they were written, and
//     offered in the order the M_CAN's Tx handler scans them (RM0468 "FDCAN
//     Tx handling": dedicated Tx buffers and the Tx queue by lowest ID, the
//     Tx FIFO in put order, mixed modes by the lower ID of the two). TXBRP,
//     TXBTO, TXBCF, TXFQS and IR.TC/TCF read this model's state: a request is
//     pending until the bus says its frame is over (its last EOF bit), then
//     TXBTO and IR.TC are set, or, cancelled, TXBCF and IR.TCF. The Tx FIFO /
//     queue occupies buffers NDTB..NDTB+TFQS-1, so its put and get indices
//     start at NDTB (Renode's MCAN starts them at 0, which with dedicated
//     buffers - the AMS has 16 - hands the HAL a dedicated buffer as the
//     FIFO's put index).
//   - Not modelled on an arbitrated bus, and logged once when enabled: the
//     Tx interrupts (IE.TCE/TCFE/TFEE and the Tx event FIFO's), and the Tx
//     event FIFO itself (an element asking for an event, T1.EFC). Neither
//     firmware uses them (TxEventFifoControl = FDCAN_NO_TX_EVENTS, RX FIFO0
//     notifications only).
//   - Nothing goes out while CCCR.INIT is set (RM0468: INIT stops all bus
//     activity), and a controller in INIT, bus-off or bus monitoring mode
//     (CCCR.MON) neither receives nor acknowledges.
//   - CCCR.DAR (disabled automatic retransmission): a frame that loses
//     arbitration or gets no ACK is cancelled (TXBCF, IR.TCF, its FIFO element
//     freed) instead of retried, as RM0468 "Disabled automatic retransmission"
//     and the M_CAN user manual describe. Otherwise it is retried.
//   - Error counting is the bus's (ISO 11898-1 fault confinement, minimal: ACK
//     errors only). ECR.TEC and ECR.CEL, PSR.LEC (3 = Ack error, 0 = no error,
//     7 after a read), PSR.EW (TEC >= 96) and PSR.EP (TEC >= 128) read what the
//     bus counted, from the time the error happened. REC stays 0: receive
//     errors are not modelled.
//   - Not modelled, and logged once when the firmware enables them: the error
//     interrupts IR.ELO/EP/EW/BO/PEA/PED. Polling ECR and PSR, as both
//     firmwares do (HAL_FDCAN_GetProtocolStatus), sees the state.
//   - Cancelling a request (TXBCR) applies when it is written; a frame the bus
//     already decided is not cancelled (as on the chip once transmission has
//     started).
//
// NBTP is held here (Renode's MCAN drops writes to it): writable only while
// CCCR.INIT and CCE are both set, reset value 0x06000A03 (RM0468, FDCAN_NBTP;
// what Renode's MCAN reads back). WireTiming and the arbitrated bus use it;
// without them, bit timing changes nothing a test sees.
//
// Offsets and bits from ST's stm32h733xx.h (FDCAN_GlobalTypeDef :325-372, bit
// positions :4361-4475 and :4657-4675): CCCR 0x018 (INIT 0, CCE 1, MON 5, DAR
// 6), NBTP 0x01C (NBRP [24:16], NTSEG1 [15:8], NTSEG2 [6:0]), ECR 0x040 (TEC
// [7:0], REC [14:8], CEL [23:16]), PSR 0x044 (LEC [2:0], EP 5, EW 6, BO 7), IR
// 0x050, IE 0x054 (ELOE 22, EPE 23, EWE 24, BOE 25, PEAE 27, PEDE 28), TXBC
// 0x0C0 (TBSA [15:2], NDTB [21:16], TFQS [29:24], TFQM 30), TXFQS 0x0C4 (TFFL
// [5:0], TFGI [12:8], TFQPI [20:16], TFQF 21), TXESC 0x0C8 (TBDS [2:0]),
// TXBRP 0x0CC, TXBAR 0x0D0, TXBCR 0x0D4, TXBTO 0x0D8, TXBCF 0x0DC; IR/IE TC 9,
// TCF 10, TFE 11, TEFN 12-15. TBSA is a byte offset into the message RAM. Tx
// buffer element header (RM0468, FDCAN Tx buffer element): T0 ID [28:0] (a
// standard ID in [28:18]), RTR 29, XTD 30; T1 DLC [19:16], FDF 21, EFC 23.
// With no hook set and no arbitrated bus, this is Renode's MCAN unchanged but
// for NBTP.
//
using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.CAN;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.CAN
{
    // The bus side a controller talks to (models/renode/VhilCanBus.cs). Called
    // from the controller's machine thread.
    public interface IVhilCanBus
    {
        // The controller's requests or its ability to transmit changed (TXBAR,
        // TXBCR, INIT): the bus may decide frames up to the caller's now.
        void RequestsChanged(IVhilCanController node);

        // The error counters went back to 0 (reset, bus-off recovery).
        void CountersReset(IVhilCanController node);
    }

    // One transmission request, as the controller offers it to the bus.
    public sealed class VhilCanRequest
    {
        public int Buffer;              // Tx buffer element index
        public ulong TimeNs;            // virtual time TXBAR was written
        public bool Fifo;               // a Tx FIFO element (else dedicated / queue)
        public CANMessageFrame Frame;
        public int Generation;          // the controller's, when requested
        public bool Committed;          // the bus has decided its outcome
        public bool CancelRequested;
    }

    // The controller side of an arbitrated bus. The bus calls the queries and
    // Commit under its own lock, in whichever thread decides (the machine's,
    // or the master time source's at a sync point while every machine waits);
    // Complete, SetErrorState and Receive run in the controller's machine, at
    // the virtual time the bus scheduled them for.
    public interface IVhilCanController : ICAN
    {
        IMachine OwnerMachine { get; }
        void Join(IVhilCanBus bus);
        ulong BitRate { get; }                       // bit/s, from NBTP
        bool AutoRetransmissionDisabled { get; }     // CCCR.DAR
        bool Acknowledges { get; }                   // takes part and ACKs
        bool Receives { get; }
        int Tec { get; }                             // as the bus last set it
        // Earliest virtual time (ns) at which a request is offered, or null.
        ulong? EarliestRequestNs();
        // The request the Tx handler would put on the bus at atNs.
        VhilCanRequest Candidate(ulong atNs);
        void Commit(VhilCanRequest request);
        void Complete(VhilCanRequest request, bool transmitted);
        void SetErrorState(int tec, bool error);
        void Receive(CANMessageFrame frame);
    }

    public class STM32H7_FDCAN : MCAN, IDoubleWordPeripheral, IVhilCanController
    {
        public STM32H7_FDCAN(IMachine machine, IMultibyteWritePeripheral messageRAM)
            : base(machine, messageRAM)
        {
            owner = machine;
            ram = messageRAM as IDoubleWordPeripheral;
            bytes = messageRAM;
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
            Changed();
        }

        public bool BusOff => busOff;

        public uint BusOffRecoveries { get; private set; }

        public bool TxFifoFull { get; set; }

        public bool FailInit { get; set; }

        public bool WireTiming { get; set; }

        public bool Arbitrated => bus != null;

        // On an arbitrated bus: "buffer time_ns id fifo committed" per request
        // held for the bus, in request order.
        public string Requests()
        {
            lock(sync)
            {
                return string.Concat(pending.Select(r => $"{r.Buffer} {r.TimeNs} 0x{r.Frame.Id:X} "
                                                         + $"{(r.Fifo ? 1 : 0)} {(r.Committed ? 1 : 0)}\n"));
            }
        }

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
                if(bus != null)
                {
                    value = (value & ~(PsrLec | PsrEp | PsrEw)) | lec;
                    lec = LecNoChange;
                    value |= tec >= 128 ? PsrEp : 0;
                    value |= tec >= 96 ? PsrEw : 0;
                }
                if(busOff)
                {
                    value |= PsrBo | PsrEp | PsrEw;
                }
                break;
            case Ecr:
                if(bus != null)
                {
                    value = (uint)tec | (cel << 16);
                    cel = 0;   // CEL is cleared when ECR is read (RM0468, FDCAN_ECR)
                }
                break;
            case Txfqs:
                if(TxFifoFull)
                {
                    value = (value & ~TxfqsTffl) | TxfqsTfqf;
                }
                else if(bus != null)
                {
                    value = FifoStatus();
                }
                else if(held.Count + wire.Count > 0)
                {
                    value = WithHeld(value, held.Count + wire.Count);
                }
                break;
            case Txbrp:
                if(bus != null)
                {
                    lock(sync)
                    {
                        value = pending.Aggregate(0u, (m, r) => m | (1u << r.Buffer));
                    }
                }
                break;
            case Txbto:
                value = bus != null ? occurred : value;
                break;
            case Txbcf:
                value = bus != null ? cancelled : value;
                break;
            case Ir:
                value |= bus != null ? txFlags : 0;
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
            if(bus != null)
            {
                if(offset == Txbar)
                {
                    Request(value);
                    return;
                }
                if(offset == Txbcr)
                {
                    Cancel(value);
                    return;
                }
                if(offset == Ir)
                {
                    txFlags &= ~value;   // write 1 to clear
                }
                if(offset == Ie && (value & UnmodelledInterrupts) != 0 && !warnedInterrupts)
                {
                    warnedInterrupts = true;
                    this.Log(LogLevel.Warning, "IE 0x{0:X8} enables FDCAN interrupts this model never raises on "
                             + "an arbitrated bus (Tx complete/cancel/FIFO empty/event FIFO, errors; #174): poll "
                             + "TXBTO, TXFQS, ECR and PSR instead", value);
                }
            }
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
                lock(sync)
                {
                    // CCE resets TXBRP, TXBTO, TXBCF and TXFQS (RM0468,
                    // FDCAN_CCCR): every request is dropped, and a frame the
                    // bus already decided completes nothing here.
                    pending.Clear();
                    generation++;
                    occurred = cancelled = 0;
                    fifoPut = 0;
                }
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
                if(bus != null)
                {
                    // The recovery sequence resets the error counters (RM0468,
                    // FDCAN bus-off recovery).
                    tec = 0;
                    bus.CountersReset(this);
                }
            }
            if(offset == Cccr && wasInit != ((value & CccrInit) != 0))
            {
                Changed();
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
            lock(sync)
            {
                pending.Clear();
                generation++;
                occurred = cancelled = txFlags = 0;
                fifoPut = 0;
            }
            tec = 0;
            cel = 0;
            lec = LecNoChange;
            bus?.CountersReset(this);
            // FailInit, WireTiming and BusOffRecoveries are the test's: they survive.
        }

        // -- IVhilCanController ------------------------------------------------

        public IMachine OwnerMachine => owner;

        public void Join(IVhilCanBus arbiter)
        {
            bus = arbiter;
            if(WireTiming && bus != null)
            {
                this.Log(LogLevel.Info, "WireTiming has no effect on an arbitrated bus: the bus times every frame");
            }
        }

        public ulong BitRate
        {
            get
            {
                var prescaler = ((nbtp >> 16) & 0x1FF) + 1;
                var tqPerBit = 1 + ((nbtp >> 8) & 0xFF) + 1 + (nbtp & 0x7F) + 1;
                return (ulong)(KernelClockHz / (prescaler * tqPerBit));
            }
        }

        public bool AutoRetransmissionDisabled => (base.ReadDoubleWord(Cccr) & CccrDar) != 0;

        public bool Receives => !busOff && (base.ReadDoubleWord(Cccr) & CccrInit) == 0;

        public bool Acknowledges => Receives && (base.ReadDoubleWord(Cccr) & CccrMon) == 0;

        public int Tec => tec;

        public ulong? EarliestRequestNs()
        {
            if(!Receives)
            {
                return null;   // INIT or bus-off: nothing goes out
            }
            lock(sync)
            {
                ulong? earliest = null;
                foreach(var r in Offerable())
                {
                    earliest = earliest == null ? r.TimeNs : Math.Min(earliest.Value, r.TimeNs);
                }
                return earliest;
            }
        }

        // RM0468 "Transmit prioritization": the dedicated buffers and the Tx
        // queue by lowest ID, the Tx FIFO by its get index (the oldest
        // request); between the two, the lower ID. A FIFO element waits for
        // every element before it, decided or not.
        public VhilCanRequest Candidate(ulong atNs)
        {
            if(!Receives)
            {
                return null;
            }
            lock(sync)
            {
                VhilCanRequest best = null;
                foreach(var r in Offerable())
                {
                    if(r.TimeNs > atNs)
                    {
                        continue;
                    }
                    var order = best == null ? -1 : VhilCanFrameBits.Compare(r.Frame, best.Frame);
                    if(order < 0 || (order == 0 && r.Buffer < best.Buffer))
                    {
                        best = r;
                    }
                }
                return best;
            }
        }

        // The requests the Tx handler may pick from: every undecided dedicated
        // or queue buffer, and the oldest undecided FIFO element (those before
        // it are decided: on the bus, or done and waiting for the end of their
        // frame to complete here). Under `sync`.
        private IEnumerable<VhilCanRequest> Offerable()
        {
            var fifoOffered = false;
            foreach(var r in pending)
            {
                if(r.Committed || (r.Fifo && fifoOffered))
                {
                    continue;
                }
                fifoOffered |= r.Fifo;
                yield return r;
            }
        }

        public void Commit(VhilCanRequest request)
        {
            lock(sync)
            {
                request.Committed = true;
            }
        }

        // The bus's verdict, at the end of the frame (or of its error frame,
        // or of the arbitration it lost with DAR set).
        public void Complete(VhilCanRequest request, bool transmitted)
        {
            lock(sync)
            {
                if(request.Generation != generation || !pending.Remove(request))
                {
                    return;   // reset, or cancelled by CCE since the bus decided
                }
                if(transmitted)
                {
                    occurred |= 1u << request.Buffer;
                    txFlags |= IrTc;
                }
                else
                {
                    cancelled |= 1u << request.Buffer;
                    txFlags |= IrTcf;
                }
            }
        }

        public void SetErrorState(int newTec, bool error)
        {
            tec = newTec;
            lec = error ? LecAckError : LecNoError;
            if(error && cel < 255)
            {
                cel++;
            }
        }

        public void Receive(CANMessageFrame frame)
        {
            OnFrameReceived(frame);
        }

        // -- arbitrated TX -----------------------------------------------------

        private void Request(uint value)
        {
            var txbc = base.ReadDoubleWord(Txbc);
            var dedicated = (int)((txbc >> 16) & 0x3F);
            var total = dedicated + (int)((txbc >> 24) & 0x3F);
            var queueMode = (txbc & TxbcTfqm) != 0;
            if((base.ReadDoubleWord(Cccr) & CccrCce) != 0)
            {
                return;   // TXBAR is ignored while CCE is set (RM0468, FDCAN_TXBAR)
            }
            var now = NowNs();
            var size = total - dedicated;
            lock(sync)
            {
                for(var i = 0; i < Math.Min(total, 32); i++)
                {
                    if((value & (1u << i)) == 0 || pending.Any(r => r.Buffer == i))
                    {
                        continue;   // a pending buffer's request stands (RM0468, FDCAN_TXBAR)
                    }
                    var fifo = i >= dedicated && !queueMode;
                    pending.Add(new VhilCanRequest
                    {
                        Buffer = i, TimeNs = now, Fifo = fifo, Frame = ReadElement(i), Generation = generation,
                    });
                    occurred &= ~(1u << i);
                    cancelled &= ~(1u << i);
                    if(fifo)
                    {
                        fifoPut = (i - dedicated + 1) % size;
                    }
                }
            }
            Changed();
        }

        private void Cancel(uint value)
        {
            var any = false;
            for(var i = 0; i < 32; i++)
            {
                if((value & (1u << i)) == 0)
                {
                    continue;
                }
                VhilCanRequest request;
                lock(sync)
                {
                    request = pending.FirstOrDefault(r => r.Buffer == i);
                    if(request == null)
                    {
                        continue;   // nothing pending: no effect
                    }
                    if(request.Committed)
                    {
                        request.CancelRequested = true;   // already on the bus
                        continue;
                    }
                    pending.Remove(request);
                    cancelled |= 1u << i;
                    txFlags |= IrTcf;
                    any = true;
                }
            }
            if(any)
            {
                Changed();
            }
        }

        private void Changed()
        {
            bus?.RequestsChanged(this);
        }

        // TXFQS of the Tx FIFO or queue in buffers NDTB..NDTB+TFQS-1: free
        // level, get index (the oldest pending FIFO element), put index, full.
        private uint FifoStatus()
        {
            var txbc = base.ReadDoubleWord(Txbc);
            var dedicated = (int)((txbc >> 16) & 0x3F);
            var size = (int)((txbc >> 24) & 0x3F);
            if(size == 0)
            {
                return 0;
            }
            lock(sync)
            {
                int free, put, get;
                if((txbc & TxbcTfqm) != 0)
                {
                    var busy = new HashSet<int>(pending.Select(r => r.Buffer));
                    var slots = Enumerable.Range(dedicated, size).Where(i => !busy.Contains(i)).ToList();
                    free = slots.Count;
                    put = free > 0 ? slots[0] : 0;
                    get = 0;
                }
                else
                {
                    var fifo = pending.Where(r => r.Fifo).ToList();
                    free = size - fifo.Count;
                    put = dedicated + fifoPut;
                    get = fifo.Count > 0 ? fifo[0].Buffer : put;
                }
                var value = (uint)free | ((uint)get << 8) | ((uint)put << 16);
                return free == 0 ? value | TxfqsTfqf : value;
            }
        }

        private CANMessageFrame ReadElement(int index)
        {
            var txbc = base.ReadDoubleWord(Txbc);
            var dataBytes = new uint[] { 8, 12, 16, 20, 24, 32, 48, 64 }[base.ReadDoubleWord(Txesc) & 0x7];
            var element = (long)(txbc & 0xFFFC) + index * (8 + dataBytes);
            var t0 = ram.ReadDoubleWord(element);
            var t1 = ram.ReadDoubleWord(element + 4);
            var extended = (t0 & (1u << 30)) != 0;
            var remote = (t0 & (1u << 29)) != 0;
            var dlc = (t1 >> 16) & 0xF;
            if((t1 & (1u << 21)) != 0 && !warnedFd)
            {
                warnedFd = true;
                this.Log(LogLevel.Warning, "CAN FD frame: the bus times it as a classic frame (#174)");
            }
            if((t1 & (1u << 23)) != 0 && !warnedEvents)
            {
                warnedEvents = true;
                this.Log(LogLevel.Warning, "Tx buffer {0} asks for a Tx event (EFC): the Tx event FIFO is not "
                         + "modelled on an arbitrated bus (#174)", index);
            }
            var length = remote ? 0 : (int)Math.Min(dlc, 8u);
            var data = bytes.ReadBytes(element + 8, length);
            var id = extended ? t0 & 0x1FFFFFFF : (t0 >> 18) & 0x7FF;
            return new CANMessageFrame(id, data, extended, remote);
        }

        private ulong NowNs()
        {
            if(owner.SystemBus.TryGetCurrentCPU(out var cpu))
            {
                cpu.SyncTime();
            }
            return owner.ElapsedVirtualTime.TimeElapsed.Ticks;
        }

        // -- WireTiming (no arbiter) -------------------------------------------

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

        // TXFQS as if `pending` more requests were in the FIFO: the put index
        // and free level move on, and the FIFO reads full once they fill it.
        private uint WithHeld(uint value, int pendingCount)
        {
            var txbc = base.ReadDoubleWord(Txbc);
            var dedicated = (txbc >> 16) & 0x3F;
            var size = (txbc >> 24) & 0x3F;
            if(size == 0 || pendingCount == 0)
            {
                return value;
            }
            var free = (uint)System.Math.Max(0, (int)(value & TxfqsTffl) - pendingCount);
            var put = dedicated + (((value >> 16) & 0x1F) - dedicated + (uint)pendingCount) % size;
            value = (value & ~(TxfqsTffl | TxfqsTfqpi | TxfqsTfqf)) | free | (put << 16);
            return free == 0 ? value | TxfqsTfqf : value;
        }

        private bool busOff;
        private uint nbtp = NbtpReset;
        private readonly Queue<int> held = new Queue<int>();
        private readonly Queue<int> wire = new Queue<int>();
        private readonly LimitTimer wireTimer;
        private readonly IDoubleWordPeripheral ram;
        private readonly IMultibyteWritePeripheral bytes;
        private readonly IMachine owner;

        // Arbitrated bus state.
        private IVhilCanBus bus;
        private readonly object sync = new object();
        private readonly List<VhilCanRequest> pending = new List<VhilCanRequest>();   // request order
        private int generation;
        private uint occurred;          // TXBTO
        private uint cancelled;         // TXBCF
        private uint txFlags;           // IR.TC, IR.TCF
        private int fifoPut;            // Tx FIFO put index, from NDTB
        private int tec;
        private uint cel;
        private uint lec = LecNoChange;
        private bool warnedInterrupts;
        private bool warnedFd;
        private bool warnedEvents;

        private const long KernelClockHz = 24000000;
        private const long Cccr = 0x018;
        private const long Nbtp = 0x01C;
        private const uint NbtpReset = 0x06000A03;
        private const long Ecr = 0x040;
        private const long Psr = 0x044;
        private const long Ir = 0x050;
        private const long Ie = 0x054;
        private const long Txbc = 0x0C0;
        private const long Txfqs = 0x0C4;
        private const long Txesc = 0x0C8;
        private const long Txbrp = 0x0CC;
        private const long Txbar = 0x0D0;
        private const long Txbcr = 0x0D4;
        private const long Txbto = 0x0D8;
        private const long Txbcf = 0x0DC;
        private const uint CccrInit = 1u << 0;
        private const uint CccrCce = 1u << 1;
        private const uint CccrMon = 1u << 5;
        private const uint CccrDar = 1u << 6;
        private const uint PsrLec = 0x7u;
        private const uint PsrEp = 1u << 5;
        private const uint PsrEw = 1u << 6;
        private const uint PsrBo = 1u << 7;
        private const uint LecNoError = 0;
        private const uint LecAckError = 3;
        private const uint LecNoChange = 7;
        private const uint IrTc = 1u << 9;
        private const uint IrTcf = 1u << 10;
        // TCE, TCFE, TFEE, TEFNE/TEFWE/TEFFE/TEFLE; ELOE, EPE, EWE, BOE, PEAE, PEDE.
        private const uint UnmodelledInterrupts = (0x7Fu << 9) | (0xFu << 22) | (3u << 27);
        private const uint TxbcTfqm = 1u << 30;
        private const uint TxfqsTffl = 0x3Fu;
        private const uint TxfqsTfqpi = 0x1Fu << 16;
        private const uint TxfqsTfqf = 1u << 21;
    }

    // A classic CAN frame's bits on the wire (ISO 11898-1 frame formats; Bosch
    // CAN 2.0B part B section 3): SOF, arbitration field, control field, data,
    // CRC-15, then CRC delimiter, ACK slot and delimiter, 7 bits of EOF.
    // Bit stuffing (a complementary bit after five equal ones) applies from
    // SOF to the end of the CRC sequence, and is counted exactly from the
    // frame's contents. Shared by the bus (models/renode/VhilCanBus.cs); the
    // same computation in Python is vhil/canframe.py.
    public static class VhilCanFrameBits
    {
        // Unstuffed bits from SOF through the CRC sequence.
        public static List<bool> Stream(CANMessageFrame frame)
        {
            var bits = new List<bool>(130);
            bits.Add(false);                                   // SOF (dominant)
            var dlc = DataLengthCode(frame);
            if(frame.ExtendedFormat)
            {
                Push(bits, frame.Id >> 18, 11);                // base ID
                bits.Add(true);                                // SRR
                bits.Add(true);                                // IDE
                Push(bits, frame.Id & 0x3FFFF, 18);            // ID extension
                bits.Add(frame.RemoteFrame);                   // RTR
                bits.Add(false);                               // r1
                bits.Add(false);                               // r0
            }
            else
            {
                Push(bits, frame.Id & 0x7FF, 11);
                bits.Add(frame.RemoteFrame);                   // RTR
                bits.Add(false);                               // IDE
                bits.Add(false);                               // r0
            }
            Push(bits, dlc, 4);
            if(!frame.RemoteFrame)
            {
                foreach(var b in frame.Data.Take(8))
                {
                    Push(bits, b, 8);
                }
            }
            var crc = Crc15(bits);
            Push(bits, crc, 15);
            return bits;
        }

        // Stuff bits the transmitter inserts between SOF and the CRC's last bit.
        public static int StuffBits(List<bool> bits)
        {
            var count = 0;
            var run = 0;
            var last = false;
            for(var i = 0; i < bits.Count; i++)
            {
                if(i > 0 && bits[i] == last)
                {
                    run++;
                }
                else
                {
                    run = 1;
                    last = bits[i];
                }
                if(run == 5)
                {
                    count++;
                    last = !last;   // the stuff bit starts the next run
                    run = 1;
                }
            }
            return count;
        }

        // Bits from SOF to the end of the CRC sequence, stuff bits included.
        public static int StuffedThroughCrc(CANMessageFrame frame)
        {
            var bits = Stream(frame);
            return bits.Count + StuffBits(bits);
        }

        // SOF through the last EOF bit: + CRC delimiter, ACK slot, ACK delimiter, EOF.
        public static int Bits(CANMessageFrame frame)
        {
            return StuffedThroughCrc(frame) + 3 + 7;
        }

        // Arbitration order on the wire: the first bit where the two differ,
        // dominant (0) wins. Base ID, then RTR/SRR, then IDE, then the ID
        // extension and the extended RTR. < 0: a wins; 0: same arbitration field.
        public static int Compare(CANMessageFrame a, CANMessageFrame b)
        {
            var x = Arbitration(a);
            var y = Arbitration(b);
            for(var i = 0; i < Math.Min(x.Count, y.Count); i++)
            {
                if(x[i] != y[i])
                {
                    return x[i] ? 1 : -1;
                }
            }
            return x.Count.CompareTo(y.Count);
        }

        public static List<bool> Arbitration(CANMessageFrame frame)
        {
            var bits = Stream(frame);
            // SOF excluded; standard: 11 + RTR + IDE; extended: 11 + SRR + IDE + 18 + RTR.
            return bits.GetRange(1, frame.ExtendedFormat ? 32 : 13);
        }

        public static uint DataLengthCode(CANMessageFrame frame)
        {
            return (uint)Math.Min(frame.Data?.Length ?? 0, 8);
        }

        private static void Push(List<bool> bits, uint value, int width)
        {
            for(var i = width - 1; i >= 0; i--)
            {
                bits.Add(((value >> i) & 1) != 0);
            }
        }

        // CRC-15/CAN, generator x^15 + x^14 + x^10 + x^8 + x^7 + x^4 + x^3 + 1
        // (0x4599), over SOF through the data field, unstuffed.
        private static uint Crc15(List<bool> bits)
        {
            uint crc = 0;
            foreach(var bit in bits)
            {
                var next = bit ^ (((crc >> 14) & 1) != 0);
                crc = (crc << 1) & 0x7FFF;
                if(next)
                {
                    crc ^= 0x4599;
                }
            }
            return crc;
        }
    }
}
