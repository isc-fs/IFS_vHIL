//
// Test probes for IFS_vHIL: what a test sees of a running system, stamped in
// virtual time. Compiled by Renode at load time (`include @models/renode/VhilProbe.cs`).
//
//   VhilCanProbe   one per CAN bus, attached to its hub as a tester. Records
//                  every frame with its virtual timestamp and sends frames now,
//                  at an exact virtual time, or periodically (and records
//                  those too, apart: Sent, not Frames).
//                      emulation CreateVhilCanProbe "probe_can_acu"
//                      connector Connect probe_can_acu can_acu
//   VhilGpioProbe  one per board. Watches any GPIO output by name and keeps
//                  its edge history, and drives GPIO inputs from outside the
//                  MCU: a driven level holds across the board's resets.
//                      emulation CreateVhilGpioProbe "probe_gpio_ecu" "ecu"
//                      probe_gpio_ecu Watch "sysbus.gpioPortB" 4
//                      probe_gpio_ecu Drive "sysbus.gpioPortE" 3 true
//
// Both answer the monitor in plain text, one record per line, so the Python
// side (vhil/sim.py) parses without a serializer. Times are microseconds of
// virtual time.
//
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.CAN;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CAN;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities;

namespace Antmicro.Renode.Testing
{
    public static class VhilProbeExtensions
    {
        public static void CreateVhilCanProbe(this Emulation emulation, string name)
        {
            emulation.ExternalsManager.AddExternal(new VhilCanProbe(), name);
        }

        public static void CreateVhilGpioProbe(this Emulation emulation, string name, string machineName)
        {
            if(!emulation.TryGetMachineByName(machineName, out var machine))
            {
                throw new ArgumentException($"no machine '{machineName}'");
            }
            emulation.ExternalsManager.AddExternal(new VhilGpioProbe(machine), name);
        }
    }

    // Derives from Renode's CANTester because CANHub delivers to a CANTester
    // from the master time source: a probe belongs to no machine, and the
    // hub's per-machine delivery path would throw. ICAN is re-implemented so
    // received frames land here, stamped, instead of in CANTester's list.
    public class VhilCanProbe : CANTester, ICAN
    {
        public VhilCanProbe() : base(TimeInterval.FromSeconds(1))
        {
        }

        void ICAN.OnFrameReceived(CANMessageFrame frame)
        {
            lock(sync)
            {
                received.Add(new Record(NowMicros(), frame));
            }
        }

        // -- on an arbitrated bus (models/renode/VhilCanBus.cs) ----------------
        //
        // The bus delivers at the first sync point at or after a frame's end
        // and says when that was: records carry the bus's time, not the sync
        // point's. What the probe sends is recorded when its frame is over
        // (it may wait for the bus, or never get on it), not when it is sent.

        public bool OnArbitratedBus { get; set; }

        public void OnFrameReceivedAt(CANMessageFrame frame, ulong timeUs)
        {
            lock(sync)
            {
                received.Add(new Record(timeUs, frame));
            }
        }

        public void OnFrameSentAt(CANMessageFrame frame, ulong timeUs)
        {
            lock(sync)
            {
                sent.Add(new Record(timeUs, frame));
            }
        }

        // -- observe --------------------------------------------------------

        // "t_us id ext hex" per line, for frames received at or after sinceUs.
        // ids: "" for all, or a comma list ("0x100,0x704").
        public string Frames(string ids = "", ulong sinceUs = 0)
        {
            return Format(received, ids, sinceUs);
        }

        // The frames this probe itself put on the bus (Send, SendAt,
        // SendPeriodic, SendBatch, SendSequence), stamped when they went out
        // (on an arbitrated bus: when their last EOF bit did), in the same
        // format. Kept apart from Frames/Count, which stay what
        // the probe received from the bus.
        public string Sent(string ids = "", ulong sinceUs = 0)
        {
            return Format(sent, ids, sinceUs);
        }

        private string Format(List<Record> records, string ids, ulong sinceUs)
        {
            var wanted = ParseIds(ids);
            var sb = new StringBuilder();
            lock(sync)
            {
                foreach(var r in records)
                {
                    if(r.TimeUs >= sinceUs && (wanted == null || wanted.Contains(r.Frame.Id)))
                    {
                        sb.Append(r.TimeUs).Append(' ')
                          .Append("0x").Append(r.Frame.Id.ToString("X")).Append(' ')
                          .Append(r.Frame.ExtendedFormat ? 1 : 0).Append(' ')
                          .Append(Hex(r.Frame.Data)).Append('\n');
                    }
                }
            }
            return sb.ToString();
        }

        public int Count(string ids = "", ulong sinceUs = 0)
        {
            var wanted = ParseIds(ids);
            lock(sync)
            {
                return received.Count(r => r.TimeUs >= sinceUs && (wanted == null || wanted.Contains(r.Frame.Id)));
            }
        }

        public void Clear()
        {
            lock(sync)
            {
                received.Clear();
            }
        }

        // -- stimulate ------------------------------------------------------

        // Goes out at the current virtual instant once time next advances.
        // Not SendFrame directly: on a paused emulation that starts it.
        public void Send(uint id, string hex, bool extended = false)
        {
            SendAt(0, id, hex, extended);
        }

        // Several standard frames now, in order, in one monitor call:
        // "id:hex id:hex ..." (ids in decimal). The co-simulation port sends a
        // step's frames this way.
        public void SendBatch(string frames)
        {
            foreach(var item in frames.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var parts = item.Split(':');
                Send(uint.Parse(parts[0]), parts.Length > 1 ? parts[1] : "");
            }
        }

        // Standard frames streamed from now at one frame per gapUs on average:
        // "id:hex id:hex ..." (ids in decimal). They go `burst` at a time, a
        // burst every burst * gapUs: each synced action costs a pause of the
        // emulation, and lands on a sync-quantum boundary anyway. Each burst
        // schedules the next from inside its own synced callback, as
        // SendPeriodic's Tick does: a future time scheduled from the monitor
        // is never sent (#73), one scheduled from a callback is.
        public void SendSequence(string frames, ulong gapUs, int burst = 1)
        {
            var list = new List<CANMessageFrame>();
            foreach(var item in frames.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var parts = item.Split(':');
                list.Add(new CANMessageFrame(uint.Parse(parts[0]), Bytes(parts.Length > 1 ? parts[1] : ""), false));
            }
            if(list.Count > 0)
            {
                SequenceStep(list, 0, NowMicros(), gapUs, Math.Max(burst, 1));
            }
        }

        // Send once at an absolute virtual time (us); in the past = now.
        // Two hops: called from the monitor thread, a future timestamp is
        // built in that thread's time domain and the master time source runs
        // the action at its next sync instead, i.e. now (#73); scheduled from
        // inside a synced callback, as the periodic sender's later ticks are,
        // it fires on time.
        public void SendAt(ulong atUs, uint id, string hex, bool extended = false)
        {
            var frame = new CANMessageFrame(id, Bytes(hex), extended);
            if(atUs <= NowMicros())
            {
                Schedule(0, () => Emit(frame));
                return;
            }
            Schedule(0, () => Schedule(atUs, () => Emit(frame)));
        }

        // Send every periodUs from startUs (0 = now) until StopPeriodic(key).
        // The payload can be changed while it runs with UpdatePeriodic.
        public void SendPeriodic(string key, uint id, string hex, ulong periodUs, ulong startUs = 0, bool extended = false)
        {
            if(periodUs == 0)
            {
                throw new ArgumentException("periodUs must be > 0");
            }
            var job = new Periodic { Id = id, Data = Bytes(hex), Extended = extended, PeriodUs = periodUs };
            lock(sync)
            {
                if(periodic.TryGetValue(key, out var old))
                {
                    old.Stopped = true;
                }
                periodic[key] = job;
            }
            Tick(job, startUs == 0 ? NowMicros() : startUs);
        }

        public void UpdatePeriodic(string key, string hex)
        {
            lock(sync)
            {
                if(!periodic.TryGetValue(key, out var job))
                {
                    throw new ArgumentException($"no periodic sender '{key}'");
                }
                job.Data = Bytes(hex);
            }
        }

        public void StopPeriodic(string key)
        {
            lock(sync)
            {
                if(periodic.TryGetValue(key, out var job))
                {
                    job.Stopped = true;
                    periodic.Remove(key);
                }
            }
        }

        public ulong Now()
        {
            return NowMicros();
        }

        // -- helpers --------------------------------------------------------

        // Every send goes through here, in the synced context that sends it.
        private void Emit(CANMessageFrame frame)
        {
            if(!OnArbitratedBus)
            {
                lock(sync)
                {
                    sent.Add(new Record(NowMicros(), frame));
                }
            }
            SendFrame(frame);
        }

        private void Tick(Periodic job, ulong atUs, bool synced = false)
        {
            if(!synced && atUs > NowMicros())
            {
                // A future start from the monitor thread: hop into the synced
                // context once, as SendAt does, then schedule the start (#130:
                // re-checking there hopped again and cost a sync quantum).
                Schedule(0, () => Tick(job, atUs, synced: true));
                return;
            }
            Schedule(atUs, () =>
            {
                if(job.Stopped)
                {
                    return;
                }
                Emit(new CANMessageFrame(job.Id, job.Data, job.Extended));
                Tick(job, atUs + job.PeriodUs);
            });
        }

        private void SequenceStep(List<CANMessageFrame> list, int index, ulong atUs, ulong gapUs, int burst)
        {
            Schedule(atUs, () =>
            {
                var end = Math.Min(index + burst, list.Count);
                for(var i = index; i < end; i++)
                {
                    Emit(list[i]);
                }
                if(end < list.Count)
                {
                    SequenceStep(list, end, atUs + gapUs * (ulong)burst, gapUs, burst);
                }
            });
        }

        private static void Schedule(ulong atUs, Action action)
        {
            var now = TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp();
            var at = TimeInterval.FromMicroseconds(Math.Max(atUs, (ulong)now.TimeElapsed.TotalMicroseconds));
            EmulationManager.Instance.CurrentEmulation.MasterTimeSource.ExecuteInSyncedState(
                _ => action(), new TimeStamp(at, now.Domain));
        }

        internal static ulong NowMicros()
        {
            return (ulong)TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp().TimeElapsed.TotalMicroseconds;
        }

        private static HashSet<uint> ParseIds(string ids)
        {
            if(string.IsNullOrWhiteSpace(ids))
            {
                return null;
            }
            return new HashSet<uint>(ids.Split(',').Select(s => s.Trim()).Where(s => s.Length > 0).Select(s =>
                s.StartsWith("0x", StringComparison.OrdinalIgnoreCase)
                    ? uint.Parse(s.Substring(2), NumberStyles.HexNumber)
                    : uint.Parse(s)));
        }

        private static byte[] Bytes(string hex)
        {
            hex = (hex ?? "").Replace(" ", "");
            if(hex.Length % 2 != 0)
            {
                throw new ArgumentException("hex payload needs an even number of digits");
            }
            return Enumerable.Range(0, hex.Length / 2)
                .Select(i => byte.Parse(hex.Substring(2 * i, 2), NumberStyles.HexNumber)).ToArray();
        }

        private static string Hex(byte[] data)
        {
            return string.Concat(data.Select(b => b.ToString("x2")));
        }

        private class Record
        {
            public Record(ulong timeUs, CANMessageFrame frame)
            {
                TimeUs = timeUs;
                Frame = frame;
            }
            public ulong TimeUs { get; }
            public CANMessageFrame Frame { get; }
        }

        private class Periodic
        {
            public uint Id;
            public byte[] Data;
            public bool Extended;
            public ulong PeriodUs;
            public volatile bool Stopped;
        }

        private readonly object sync = new object();
        private readonly List<Record> received = new List<Record>();
        private readonly List<Record> sent = new List<Record>();
        private readonly Dictionary<string, Periodic> periodic = new Dictionary<string, Periodic>();
    }

    // An external, not a machine peripheral: a peripheral placed `@ none` has
    // no name the monitor can reach.
    public class VhilGpioProbe : IGPIOReceiver, IExternal
    {
        public VhilGpioProbe(IMachine machine)
        {
            this.machine = machine;
            // A level driven from outside the MCU (a switch, a pull-up on the
            // board) doesn't change because the MCU resets, but Renode's
            // GPIO ports clear their inputs on machine Reset. Put them back
            // once the reset is done (MachineReset fires after the
            // peripherals' Reset), before the firmware runs again.
            machine.MachineReset += _ => Reapply();
        }

        // Drive an input pin of a GPIO port from outside, e.g.
        // Drive "sysbus.gpioPortE" 3 true. The level holds until driven again,
        // across resets and power cycles.
        public void Drive(string port, int pin, bool level)
        {
            var receiver = Input(port);
            bool hooked;
            lock(sync)
            {
                driven[Tuple.Create(port, pin)] = level;
                hooked = !hookedPorts.Add(port);
            }
            if(!hooked)
            {
                // Renode's STM32 GPIO port re-derives an input's level from its
                // own pull configuration when the firmware configures the pin
                // (a GPIO_NOPULL input reads 0 after HAL_GPIO_Init), dropping
                // the outside driver. The pin's level is the driver's, so IDR
                // (offset 0x10, RM0468 §11.4.5) reads it whatever the port
                // thinks.
                machine.SystemBus.SetHookAfterPeripheralRead<uint>((IBusPeripheral)receiver, (value, offset) =>
                    offset == IdrOffset ? ApplyDriven(port, value) : value);
            }
            receiver.OnGPIO(pin, level);
        }

        private uint ApplyDriven(string port, uint idr)
        {
            lock(sync)
            {
                foreach(var d in driven)
                {
                    if(d.Key.Item1 == port)
                    {
                        idr = d.Value ? idr | (1u << d.Key.Item2) : idr & ~(1u << d.Key.Item2);
                    }
                }
            }
            return idr;
        }

        public void Reset()
        {
            // Watches and history survive a machine reset: a power cycle is
            // exactly when a test wants to see the outputs.
        }

        // Watch one output pin of a GPIO port, e.g. Watch "sysbus.gpioPortB" 4.
        // The pin is named "<port>:<pin>" in Edges/Level output.
        public void Watch(string port, int pin)
        {
            if(!machine.TryGetByName<IPeripheral>(port, out var peripheral))
            {
                throw new ArgumentException($"no peripheral '{port}'");
            }
            if(!(peripheral is INumberedGPIOOutput outputs) || !outputs.Connections.TryGetValue(pin, out var gpio))
            {
                throw new ArgumentException($"'{port}' has no GPIO output {pin}");
            }
            var name = $"{port}:{pin}";
            int channel;
            lock(sync)
            {
                if(channels.Contains(name))
                {
                    return;
                }
                channel = channels.Count;
                channels.Add(name);
            }
            gpio.Connect(this, channel);
        }

        public void OnGPIO(int number, bool value)
        {
            lock(sync)
            {
                if(number < 0 || number >= channels.Count)
                {
                    return;
                }
                var name = channels[number];
                // Pins start low, so a first report of low is not an edge.
                if((levels.TryGetValue(name, out var last) ? last : false) == value)
                {
                    return;   // not an edge
                }
                levels[name] = value;
                edges.Add(Tuple.Create(VhilCanProbe.NowMicros(), name, value));
            }
        }

        // "t_us name 0|1" per line, for edges at or after sinceUs.
        public string Edges(string name = "", ulong sinceUs = 0)
        {
            var sb = new StringBuilder();
            lock(sync)
            {
                foreach(var e in edges)
                {
                    if(e.Item1 >= sinceUs && (name == "" || e.Item2 == name))
                    {
                        sb.Append(e.Item1).Append(' ').Append(e.Item2).Append(' ').Append(e.Item3 ? 1 : 0).Append('\n');
                    }
                }
            }
            return sb.ToString();
        }

        private IGPIOReceiver Input(string port)
        {
            if(!machine.TryGetByName<IPeripheral>(port, out var peripheral) || !(peripheral is IGPIOReceiver receiver))
            {
                throw new ArgumentException($"no GPIO port '{port}'");
            }
            return receiver;
        }

        private void Reapply()
        {
            List<KeyValuePair<Tuple<string, int>, bool>> levels;
            lock(sync)
            {
                levels = driven.ToList();
            }
            foreach(var d in levels)
            {
                Input(d.Key.Item1).OnGPIO(d.Key.Item2, d.Value);
            }
        }

        public bool Level(string name)
        {
            lock(sync)
            {
                return levels.TryGetValue(name, out var v) && v;
            }
        }

        public void Clear()
        {
            lock(sync)
            {
                edges.Clear();
            }
        }

        private readonly IMachine machine;
        private readonly object sync = new object();
        private readonly List<string> channels = new List<string>();
        private readonly Dictionary<string, bool> levels = new Dictionary<string, bool>();
        private readonly Dictionary<Tuple<string, int>, bool> driven = new Dictionary<Tuple<string, int>, bool>();
        private readonly HashSet<string> hookedPorts = new HashSet<string>();
        private const long IdrOffset = 0x10;
        private readonly List<Tuple<ulong, string, bool>> edges = new List<Tuple<ulong, string, bool>>();
    }
}
