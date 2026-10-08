//
// A CAN bus with arbitration, frame time, ACK and minimal error counting, in
// virtual time (#174): a drop-in for Renode's CAN hub on a bus a system marks
// `arbitration: true`. Compiled by Renode at load time, after the platform's
// models (it builds on IVhilCanController, models/renode/Stm32H7Fdcan.cs).
//
//     emulation CreateVhilCanBus "can_acu"
//     connector Connect sysbus.fdcan2_h7 can_acu
//
// Renode's hub hands every frame to every other node the instant it is sent:
// no bit time, no contention, no ACK. This bus models the bus at frame
// granularity (no bit-level physics), after ISO 11898-1 and the M_CAN's Tx
// handling (RM0468, FDCAN):
//
//   Offers      Each FDCAN offers the request its Tx handler would send
//               (Stm32H7Fdcan.cs), stamped with the virtual time TXBAR was
//               written. Any other node (the test probe, the SocketCAN bridge)
//               offers what it sends, in order, from the time it sent it.
//   Arbitration When the bus is idle and someone offers, every node with an
//               offer at that instant starts its frame and the arbitration
//               field decides, bit by bit, dominant winning: base ID, RTR/SRR,
//               IDE, ID extension, RTR (ISO 11898-1 bus access; VhilCanFrameBits).
//               The others wait for the next idle bus, or with CCCR.DAR set
//               their request is cancelled (RM0468 "Disabled automatic
//               retransmission"). Two offers with the same arbitration field
//               are a configuration error: logged, and the first node wins.
//   Frame time  The bus is busy for the frame's bits at the transmitter's bit
//               rate (NBTP): SOF to EOF with the exact stuff-bit count from
//               the frame's contents, then 3 bits of intermission. A receiver
//               gets the frame at the next-to-last EOF bit, the transmitter
//               completes at the last (ISO 11898-1 frame validation).
//   ACK         A frame no other node acknowledges (no node, or every other
//               one in INIT, bus-off or bus monitoring mode, or at another bit
//               rate) gets an ACK error at the ACK slot: error flag (6 bits),
//               error delimiter (8), intermission (3), and the transmitter
//               retries (or with DAR, gives up). The probe acknowledges unless
//               told not to (SetAck), as the rest of the car would.
//   Errors      Fault confinement, ACK errors only (Bosch CAN 2.0B part B
//               section 8 rules 3 and 7, as ISO 11898-1): the transmitter's
//               TEC goes +8 per error flag, but not when it is already error-
//               passive and the error is a missing ACK (rule 3, exception 1),
//               and -1 per successful frame. A lone node so climbs to 128,
//               error-passive, and stays there, retrying. An error-passive
//               transmitter waits 8 more bits (suspend transmission) after each
//               frame. Bit, stuff, CRC and form errors, REC and bus-off from
//               errors are not modelled (ForceBusOff forces bus-off).
//   Bit rate    A controller whose NBTP rate differs from the frame's neither
//               receives nor acknowledges it (logged once). Nodes with no bit
//               timing (the probe, the bridge) follow the bus: the rate of its
//               first running controller, else DefaultBitRate.
//
// Virtual time. Boards are separate Renode machines that run in parallel and
// meet at a sync point every quantum (time.quantum_s, 500 us), so an offer
// from one board is only known to the others at the next sync point. The bus
// therefore decides a frame once every offer that could compete with it is
// known, and computes its start and end exactly:
//   - with at most one machine on the bus (a board and the probe), as soon as
//     its controller offers (nothing else can offer between sync points);
//   - with several, at each sync point, for the frames that start before it.
// (a frame due exactly at a sync point waits for that sync point's phase,
// where the probe's offers for that instant come in). Then it schedules
// each effect (RX, TX complete, error state) in each
// machine at the frame's exact virtual time when that is still ahead, else at
// the sync point (up to one quantum late, never early; counted in Stats). A
// frame that starts and ends inside one quantum on a multi-board bus is
// therefore seen late; a quantum below the shortest frame (44 bit times, 88 us
// at 500 kbit/s) makes every effect exact, at a cost in wall time measured in
// #174. The timeline (Timeline, Load) is exact either way.
//
// Test API (monitor): Timeline sinceUs, Load fromUs toUs, Nodes, Stats,
// SetAck "<node>" true|false, Tec "<node>", DefaultBitRate. Times in the
// timeline are ns of virtual time.
//
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.CAN;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.CAN;
using Antmicro.Renode.Testing;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities;

namespace Antmicro.Renode.Tools.Network
{
    public static class VhilCanBusExtensions
    {
        public static void CreateVhilCanBus(this Emulation emulation, string name)
        {
            emulation.ExternalsManager.AddExternal(new VhilCanBus(), name);
        }
    }

    public sealed class VhilCanBus : IExternal, IConnectable<ICAN>, IVhilCanBus
    {
        public VhilCanBus()
        {
            master = EmulationManager.Instance.CurrentEmulation.MasterTimeSource;
            master.SyncHook += OnSync;
        }

        // The bit rate of nodes with no bit timing of their own, until a
        // controller on the bus runs: 500 kbit/s, every ISC bus's.
        public ulong DefaultBitRate { get; set; } = 500000;

        public int MaxRecords { get; set; } = 200000;

        // -- IConnectable ------------------------------------------------------

        public void AttachTo(ICAN iface)
        {
            lock(sync)
            {
                if(nodes.Any(n => n.Iface == iface))
                {
                    throw new RecoverableException("this CAN interface is already on the bus");
                }
                var node = new Node { Iface = iface, Index = nodes.Count, Ctrl = iface as IVhilCanController };
                if(node.Ctrl != null)
                {
                    node.Machine = node.Ctrl.OwnerMachine;
                }
                else if(!(iface is CANTester))
                {
                    try
                    {
                        node.Machine = iface.GetMachine();
                    }
                    catch(Exception)
                    {
                        node.Machine = null;
                    }
                }
                node.Name = NameOf(iface, node.Machine);
                nodes.Add(node);
                eager = nodes.Where(n => n.Ctrl != null).Select(n => n.Machine).Distinct().Count() <= 1;
                if(node.Ctrl != null)
                {
                    node.Ctrl.Join(this);
                }
                else
                {
                    var type = iface.GetType();
                    node.ReceivedAt = type.GetMethod("OnFrameReceivedAt", new[] { typeof(CANMessageFrame), typeof(ulong) });
                    node.SentAt = type.GetMethod("OnFrameSentAt", new[] { typeof(CANMessageFrame), typeof(ulong) });
                    type.GetProperty("OnArbitratedBus")?.SetValue(iface, true);
                    node.Handler = frame => OfferPassive(node, frame);
                    iface.FrameSent += node.Handler;
                }
            }
        }

        public void DetachFrom(ICAN iface)
        {
            lock(sync)
            {
                var node = nodes.FirstOrDefault(n => n.Iface == iface);
                if(node == null)
                {
                    return;
                }
                nodes.Remove(node);
                if(node.Ctrl != null)
                {
                    node.Ctrl.Join(null);
                }
                else
                {
                    iface.FrameSent -= node.Handler;
                }
            }
        }

        // -- IVhilCanBus -------------------------------------------------------

        public void RequestsChanged(IVhilCanController ctrl)
        {
            Node node;
            lock(sync)
            {
                node = nodes.FirstOrDefault(n => n.Ctrl == ctrl);
            }
            // Only from the board's own CPU, and only when no other board can
            // offer in between: otherwise the next sync point decides.
            if(node == null || !eager || node.Machine == null || !node.Machine.SystemBus.TryGetCurrentCPU(out var cpu))
            {
                return;
            }
            cpu.SyncTime();
            AdvanceInMachine(node.Machine.ElapsedVirtualTime.TimeElapsed.Ticks);
        }

        public void CountersReset(IVhilCanController ctrl)
        {
            lock(sync)
            {
                var node = nodes.FirstOrDefault(n => n.Ctrl == ctrl);
                if(node != null)
                {
                    node.Tec = 0;
                }
            }
        }

        // -- test API ------------------------------------------------------------

        // "start_ns end_ns free_ns offer_ns node id ext rtr dlc outcome hex" per
        // frame decided at or after sinceUs. end: the last EOF bit, or the end
        // of the error frame; free: when the bus is idle again (intermission).
        // outcome: ok, ack (no ACK: error frame), lost (lost arbitration with
        // DAR: cancelled, never on the bus past its arbitration field).
        public string Timeline(ulong sinceUs = 0)
        {
            var sb = new StringBuilder();
            lock(sync)
            {
                foreach(var r in records.Skip(FirstAtOrAfter(sinceUs * 1000)))
                {
                    sb.Append(r.StartNs).Append(' ').Append(r.EndNs).Append(' ').Append(r.FreeNs).Append(' ')
                      .Append(r.OfferNs).Append(' ').Append(r.Node).Append(' ')
                      .Append("0x").Append(r.Frame.Id.ToString("X")).Append(' ')
                      .Append(r.Frame.ExtendedFormat ? 1 : 0).Append(' ')
                      .Append(r.Frame.RemoteFrame ? 1 : 0).Append(' ')
                      .Append(VhilCanFrameBits.DataLengthCode(r.Frame)).Append(' ')
                      .Append(r.Outcome).Append(' ')
                      .Append(string.Concat(r.Frame.Data.Select(b => b.ToString("x2")))).Append('\n');
                }
            }
            return sb.ToString();
        }

        // Fraction of [fromUs, toUs) the bus was busy: frames and error frames
        // from SOF through intermission.
        public double Load(ulong fromUs, ulong toUs)
        {
            if(toUs <= fromUs)
            {
                return 0;
            }
            ulong from = fromUs * 1000, to = toUs * 1000, busy = 0;
            lock(sync)
            {
                // A frame starts at most one frame (< 200 bits, 400 us at the
                // slowest rate the H7 is run at here) before `from`.
                var startAt = FirstAtOrAfter(from > 2000000 ? from - 2000000 : 0);
                for(var i = startAt; i < records.Count && records[i].StartNs < to; i++)
                {
                    var r = records[i];
                    if(r.Outcome == "lost")
                    {
                        continue;
                    }
                    var s = Math.Max(r.StartNs, from);
                    var e = Math.Min(r.FreeNs, to);
                    if(e > s)
                    {
                        busy += e - s;
                    }
                }
            }
            return (double)busy / (to - from);
        }

        // "name kind machine tec ack sent ack_errors lost bitrate" per node.
        public string Nodes()
        {
            var sb = new StringBuilder();
            lock(sync)
            {
                foreach(var n in nodes)
                {
                    sb.Append(n.Name).Append(' ').Append(n.Ctrl != null ? "controller" : "passive").Append(' ')
                      .Append(n.Machine != null ? MachineName(n.Machine) : "-").Append(' ')
                      .Append(n.Tec).Append(' ').Append(Acks(n) ? 1 : 0).Append(' ')
                      .Append(n.Sent).Append(' ').Append(n.AckErrors).Append(' ').Append(n.Lost).Append(' ')
                      .Append(n.Ctrl != null ? n.Ctrl.BitRate : BusBitRate()).Append('\n');
                }
            }
            return sb.ToString();
        }

        public string Stats()
        {
            lock(sync)
            {
                return string.Format(CultureInfo.InvariantCulture,
                    "frames {0} ack_errors {1} lost {2} busy_ns {3} late {4} max_late_ns {5} dropped_records {6} eager {7}\n",
                    framesOk, ackErrors, lost, busyNs, late, maxLateNs, droppedRecords, eager ? 1 : 0);
            }
        }

        // Whether a node with no controller of its own (the probe, the bridge)
        // acknowledges frames. A controller acknowledges by its own state.
        public void SetAck(string node, bool ack)
        {
            lock(sync)
            {
                var n = Find(node);
                if(n.Ctrl != null)
                {
                    throw new RecoverableException($"'{node}' is a controller: it acknowledges by its own state (INIT, MON)");
                }
                n.Ack = ack;
            }
        }

        public int Tec(string node)
        {
            lock(sync)
            {
                return Find(node).Tec;
            }
        }

        // -- the bus -------------------------------------------------------------

        private void OfferPassive(Node node, CANMessageFrame frame)
        {
            var ts = TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp();
            var now = ts.Domain == master.Domain ? ts.TimeElapsed.Ticks : master.ElapsedVirtualTime.Ticks;
            lock(sync)
            {
                node.Queue.Enqueue(new Offer { Frame = frame, TimeNs = now });
            }
            // Decided by the next sync point's Advance, after every action of
            // this one: two nodes sending at the same instant then compete.
        }

        private void OnSync(TimeInterval elapsed)
        {
            // Last in this sync point's actions (they run in time, then id,
            // order), so every node's offers up to now are in.
            master.ExecuteInSyncedState(_ => Advance(elapsed.Ticks), new TimeStamp(elapsed, master.Domain));
        }

        // Decide every frame that starts at or before `until` (ns).
        private void Advance(ulong until)
        {
            lock(sync)
            {
                while(true)
                {
                    ulong? first = null;
                    foreach(var n in nodes)
                    {
                        var e = Earliest(n);
                        if(e != null && (first == null || e.Value < first.Value))
                        {
                            first = e;
                        }
                    }
                    if(first == null)
                    {
                        break;
                    }
                    var start = Math.Max(busFreeNs, first.Value);
                    if(start > until)
                    {
                        ScheduleWake(start);
                        break;
                    }
                    if(!Decide(start))
                    {
                        break;
                    }
                }
            }
        }

        private bool Decide(ulong start)
        {
            var contenders = new List<Tuple<Node, Offer>>();
            foreach(var n in nodes)
            {
                var offer = OfferAt(n, start);
                if(offer != null)
                {
                    contenders.Add(Tuple.Create(n, offer));
                }
            }
            if(contenders.Count == 0)
            {
                return false;   // a node's state changed under us: the next call decides
            }
            contenders.Sort((a, b) =>
            {
                var order = VhilCanFrameBits.Compare(a.Item2.Frame, b.Item2.Frame);
                return order != 0 ? order : a.Item1.Index.CompareTo(b.Item1.Index);
            });
            var winner = contenders[0].Item1;
            var offer0 = contenders[0].Item2;
            var frame = offer0.Frame;
            var rate = winner.Ctrl != null ? winner.Ctrl.BitRate : BusBitRate();
            var arbitrationEnd = start + BitsNs(1 + (frame.ExtendedFormat ? 32 : 13), rate);

            foreach(var c in contenders.Skip(1))
            {
                if(VhilCanFrameBits.Compare(c.Item2.Frame, frame) == 0 && warnedSameId.Add(frame.Id))
                {
                    this.Log(LogLevel.Error, "{0} and {1} both send ID 0x{2:X}: two nodes may not share an ID "
                             + "(a configuration error); {0} wins", winner.Name, c.Item1.Name, frame.Id);
                }
                if(c.Item1.Ctrl != null && c.Item1.Ctrl.AutoRetransmissionDisabled)
                {
                    // Lost arbitration with DAR: cancelled, not retried.
                    var loser = c.Item1;
                    var request = c.Item2.Request;
                    loser.Ctrl.Commit(request);
                    loser.Lost++;
                    lost++;
                    AddRecord(new Record { StartNs = start, EndNs = arbitrationEnd, FreeNs = arbitrationEnd,
                                           OfferNs = c.Item2.TimeNs, Node = loser.Name, Frame = c.Item2.Frame, Outcome = "lost" });
                    At(loser, arbitrationEnd, () => loser.Ctrl.Complete(request, false));
                }
            }

            var receivers = new List<Node>();
            var ack = false;
            foreach(var n in nodes)
            {
                if(n == winner)
                {
                    continue;
                }
                if(n.Ctrl != null)
                {
                    if(!n.Ctrl.Receives)
                    {
                        continue;
                    }
                    if(n.Ctrl.BitRate != rate)
                    {
                        if(warnedRate.Add(Tuple.Create(winner.Index, n.Index)))
                        {
                            this.Log(LogLevel.Warning, "{0} runs at {1} bit/s, {2} at {3}: it neither receives nor "
                                     + "acknowledges its frames", n.Name, n.Ctrl.BitRate, winner.Name, rate);
                        }
                        continue;
                    }
                }
                receivers.Add(n);
                ack |= Acks(n);
            }

            var stuffed = VhilCanFrameBits.StuffedThroughCrc(frame);
            Record record;
            if(ack)
            {
                var eof = start + BitsNs(stuffed + 10, rate);
                var free = eof + BitsNs(3, rate);
                winner.Tec = Math.Max(0, winner.Tec - 1);                       // rule 7
                winner.NotBeforeNs = free + (winner.Tec >= 128 ? BitsNs(8, rate) : 0);   // suspend transmission
                winner.Sent++;
                framesOk++;
                var tec = winner.Tec;
                Take(winner, offer0);
                if(winner.Ctrl != null)
                {
                    var request = offer0.Request;
                    At(winner, eof, () =>
                    {
                        winner.Ctrl.SetErrorState(tec, false);
                        winner.Ctrl.Complete(request, true);
                    });
                }
                else if(winner.SentAt != null)
                {
                    At(winner, eof, () => winner.SentAt.Invoke(winner.Iface, new object[] { frame, eof / 1000 }));
                }
                var rx = eof - BitsNs(1, rate);
                foreach(var n in receivers)
                {
                    var node = n;
                    if(node.Ctrl != null)
                    {
                        At(node, rx, () => node.Ctrl.Receive(frame));
                    }
                    else if(node.ReceivedAt != null)
                    {
                        At(node, rx, () => node.ReceivedAt.Invoke(node.Iface, new object[] { frame, rx / 1000 }));
                    }
                    else
                    {
                        At(node, rx, () => node.Iface.OnFrameReceived(frame));
                    }
                }
                record = new Record { StartNs = start, EndNs = eof, FreeNs = free, Outcome = "ok" };
                busFreeNs = free;
            }
            else
            {
                var ackSlot = start + BitsNs(stuffed + 2, rate);       // CRC delimiter, ACK slot
                if(winner.Tec < 128)
                {
                    winner.Tec = Math.Min(255, winner.Tec + 8);          // rule 3
                }                                                        // else exception 1
                var errorEnd = ackSlot + BitsNs(6 + 8, rate);          // error flag, delimiter
                var free = errorEnd + BitsNs(3, rate);
                winner.NotBeforeNs = free + (winner.Tec >= 128 ? BitsNs(8, rate) : 0);
                winner.AckErrors++;
                ackErrors++;
                var tec = winner.Tec;
                if(winner.Ctrl != null)
                {
                    var request = offer0.Request;
                    var giveUp = winner.Ctrl.AutoRetransmissionDisabled;
                    if(giveUp)
                    {
                        winner.Ctrl.Commit(request);
                    }
                    At(winner, ackSlot, () => winner.Ctrl.SetErrorState(tec, true));
                    if(giveUp)
                    {
                        At(winner, errorEnd, () => winner.Ctrl.Complete(request, false));
                    }
                }
                record = new Record { StartNs = start, EndNs = errorEnd, FreeNs = free, Outcome = "ack" };
                busFreeNs = free;
            }
            record.OfferNs = offer0.TimeNs;
            record.Node = winner.Name;
            record.Frame = frame;
            busyNs += record.FreeNs - record.StartNs;
            AddRecord(record);
            return true;
        }

        // When a node next offers (ns), or null.
        private ulong? Earliest(Node n)
        {
            ulong? t;
            if(n.Ctrl != null)
            {
                t = n.Ctrl.EarliestRequestNs();
            }
            else
            {
                t = n.Queue.Count > 0 ? n.Queue.Peek().TimeNs : (ulong?)null;
            }
            return t == null ? null : (ulong?)Math.Max(t.Value, n.NotBeforeNs);
        }

        private Offer OfferAt(Node n, ulong at)
        {
            if(at < n.NotBeforeNs)
            {
                return null;
            }
            if(n.Ctrl != null)
            {
                var r = n.Ctrl.Candidate(at);
                return r == null ? null : new Offer { Frame = r.Frame, TimeNs = r.TimeNs, Request = r };
            }
            return n.Queue.Count > 0 && n.Queue.Peek().TimeNs <= at ? n.Queue.Peek() : null;
        }

        private void Take(Node n, Offer offer)
        {
            if(n.Ctrl != null)
            {
                n.Ctrl.Commit(offer.Request);
            }
            else
            {
                n.Queue.Dequeue();
            }
        }

        private bool Acks(Node n)
        {
            return n.Ctrl != null ? n.Ctrl.Acknowledges : n.Ack;
        }

        private ulong BusBitRate()
        {
            foreach(var n in nodes)
            {
                if(n.Ctrl != null && n.Ctrl.Receives)
                {
                    return n.Ctrl.BitRate;
                }
            }
            return DefaultBitRate;
        }

        private static ulong BitsNs(int bits, ulong rate)
        {
            return (ulong)Math.Round(bits * 1e9 / rate);
        }

        // Run `action` in the node's machine at virtual time `at` (ns): exactly
        // when the machine has not reached it yet, else as soon as it runs
        // again (counted as late). A node outside any machine (the probe) gets
        // it at the first sync point at or after `at`, told `at`.
        private void At(Node node, ulong at, Action action)
        {
            var machine = node.Machine;
            if(machine == null)
            {
                master.ExecuteInSyncedState(_ => action(), new TimeStamp(TimeInterval.FromTicks(at), master.Domain));
                return;
            }
            if(machine.SystemBus.TryGetCurrentCPU(out var cpu))
            {
                cpu.SyncTime();
            }
            var now = machine.ElapsedVirtualTime.TimeElapsed.Ticks;
            if(at < now)
            {
                late++;
                maxLateNs = Math.Max(maxLateNs, now - at);
            }
            machine.ScheduleAction(TimeInterval.FromTicks(at > now ? at - now : 1), _ => action(), "vhil-can");
        }

        // With one board on the bus, wake the bus in that board when the next
        // frame may start; with several, the sync points do.
        private void ScheduleWake(ulong at)
        {
            if(!eager || (wakeNs != null && wakeNs.Value <= at))
            {
                return;
            }
            var node = nodes.FirstOrDefault(n => n.Ctrl != null && n.Machine != null);
            if(node == null)
            {
                return;
            }
            wakeNs = at;
            At(node, at, () => Wake(node, at));
        }

        // A wake runs in the board's clock source, which holds its own lock
        // meanwhile; another thread may hold the bus and be waiting for that
        // clock source (Decide scheduling an effect in the board). So the
        // wake never waits for the bus: when it is taken, it tries again a
        // microsecond later (#193: that wait deadlocked ecu-ams).
        private void Wake(Node node, ulong at)
        {
            if(!System.Threading.Monitor.TryEnter(sync))
            {
                node.Machine.ScheduleAction(TimeInterval.FromTicks(RetryNs), _ => Wake(node, at), "vhil-can");
                return;
            }
            try
            {
                if(wakeNs == at)
                {
                    wakeNs = null;
                }
                AdvanceInMachine(at);   // re-enters the bus lock held here
            }
            finally
            {
                System.Threading.Monitor.Exit(sync);
            }
        }

        // From the board's own thread: up to `now`, but not the next sync
        // point itself. The probe offers at sync points, in the sync phase,
        // after the board has run up to it: a frame starting exactly there is
        // decided in that phase (OnSync), with the probe's offers in.
        private void AdvanceInMachine(ulong now)
        {
            var sync = master.NearestSyncPoint.Ticks;
            Advance(sync > 0 ? Math.Min(now, sync - 1) : now);
        }

        private void AddRecord(Record record)
        {
            records.Add(record);
            if(records.Count > MaxRecords)
            {
                var drop = records.Count / 2;
                records.RemoveRange(0, drop);
                droppedRecords += drop;
            }
        }

        // Index of the first record that starts at or after `ns`.
        private int FirstAtOrAfter(ulong ns)
        {
            int lo = 0, hi = records.Count;
            while(lo < hi)
            {
                var mid = (lo + hi) / 2;
                if(records[mid].StartNs < ns)
                {
                    lo = mid + 1;
                }
                else
                {
                    hi = mid;
                }
            }
            return lo;
        }

        private Node Find(string name)
        {
            var n = nodes.FirstOrDefault(x => x.Name == name);
            if(n == null)
            {
                throw new RecoverableException($"no node '{name}' on this bus ({string.Join(", ", nodes.Select(x => x.Name))})");
            }
            return n;
        }

        private static string NameOf(ICAN iface, IMachine machine)
        {
            var emulation = EmulationManager.Instance.CurrentEmulation;
            if(iface is IExternal external && emulation.ExternalsManager.TryGetName(external, out var name))
            {
                return name;
            }
            if(machine != null)
            {
                return $"{MachineName(machine)}:{machine.GetAnyNameOrTypeName(iface)}";
            }
            return iface.GetType().Name;
        }

        private static string MachineName(IMachine machine)
        {
            return EmulationManager.Instance.CurrentEmulation.TryGetMachineName(machine, out var name) ? name : "?";
        }

        private sealed class Node
        {
            public ICAN Iface;
            public IVhilCanController Ctrl;
            public IMachine Machine;
            public string Name;
            public int Index;
            public bool Ack = true;
            public int Tec;
            public ulong NotBeforeNs;
            public readonly Queue<Offer> Queue = new Queue<Offer>();
            public Action<CANMessageFrame> Handler;
            public MethodInfo ReceivedAt;
            public MethodInfo SentAt;
            public long Sent;
            public long AckErrors;
            public long Lost;
        }

        private sealed class Offer
        {
            public CANMessageFrame Frame;
            public ulong TimeNs;
            public VhilCanRequest Request;
        }

        private sealed class Record
        {
            public ulong StartNs;
            public ulong EndNs;
            public ulong FreeNs;
            public ulong OfferNs;
            public string Node;
            public CANMessageFrame Frame;
            public string Outcome;
        }

        private readonly object sync = new object();
        private readonly MasterTimeSource master;
        private readonly List<Node> nodes = new List<Node>();
        private readonly List<Record> records = new List<Record>();
        private readonly HashSet<uint> warnedSameId = new HashSet<uint>();
        private readonly HashSet<Tuple<int, int>> warnedRate = new HashSet<Tuple<int, int>>();
        private bool eager = true;
        private const ulong RetryNs = 1000;   // a wake that found the bus taken (Wake)
        private ulong busFreeNs;
        private ulong? wakeNs;
        private long framesOk;
        private long ackErrors;
        private long lost;
        private ulong busyNs;
        private long late;
        private ulong maxLateNs;
        private long droppedRecords;
    }
}
