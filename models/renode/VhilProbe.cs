//
// Test probes for IFS_vHIL: what a test sees of a running system, stamped in
// virtual time. Compiled by Renode at load time (`include @models/renode/VhilProbe.cs`).
//
//   VhilCanProbe   one per CAN bus, attached to its hub as a tester. Records
//                  every frame with its virtual timestamp and sends frames now,
//                  at an exact virtual time, or periodically.
//                      emulation CreateVhilCanProbe "probe_can_acu"
//                      connector Connect probe_can_acu can_acu
//   VhilGpioProbe  one per board. Watches any GPIO output by name and keeps
//                  its edge history.
//                      emulation CreateVhilGpioProbe "probe_gpio_ecu" "ecu"
//                      probe_gpio_ecu Watch "sysbus.gpioPortB" 4
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

        // -- observe --------------------------------------------------------

        // "t_us id ext hex" per line, for frames received at or after sinceUs.
        // ids: "" for all, or a comma list ("0x100,0x704").
        public string Frames(string ids = "", ulong sinceUs = 0)
        {
            var wanted = ParseIds(ids);
            var sb = new StringBuilder();
            lock(sync)
            {
                foreach(var r in received)
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

        // Send once at an absolute virtual time (us); in the past = now.
        public void SendAt(ulong atUs, uint id, string hex, bool extended = false)
        {
            var frame = new CANMessageFrame(id, Bytes(hex), extended);
            Schedule(atUs, () => SendFrame(frame));
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

        private void Tick(Periodic job, ulong atUs)
        {
            Schedule(atUs, () =>
            {
                if(job.Stopped)
                {
                    return;
                }
                SendFrame(new CANMessageFrame(job.Id, job.Data, job.Extended));
                Tick(job, atUs + job.PeriodUs);
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
        private readonly Dictionary<string, Periodic> periodic = new Dictionary<string, Periodic>();
    }

    // An external, not a machine peripheral: a peripheral placed `@ none` has
    // no name the monitor can reach.
    public class VhilGpioProbe : IGPIOReceiver, IExternal
    {
        public VhilGpioProbe(IMachine machine)
        {
            this.machine = machine;
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
        private readonly List<Tuple<ulong, string, bool>> edges = new List<Tuple<ulong, string, bool>>();
    }
}
