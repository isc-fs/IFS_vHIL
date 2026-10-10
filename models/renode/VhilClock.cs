//
// The wall-clock bench's clock (vhil/bench.py, vhil/benchclock.py), compiled
// by Renode at load time:
//
//     emulation EnableVhilClock "/tmp/vhil-xxxx/bench.clock"
//
// IFS_HIL's suites time the bench with the host's clock, but a host that
// cannot emulate the system at real time (#154: two boards on a CI runner run
// at 0.1-0.9x) makes every wall-clock deadline cover less firmware time than
// it says (#243: a 6 s heartbeat wait saw 0.6 s of the ECU's boot). This
// publishes the emulation's virtual time against the host's real-time clock,
// so the test process can run on the bench's time instead: at every sync
// point, one (virtual seconds, host Unix seconds) pair into a ring in a file
// the test process maps.
//
// Layout, little-endian:
//   0   8 bytes  "VHILCLK1"
//   8   u64      pairs written so far (count); the newest is count - 1
//   16  u64      ring capacity N
//   64  N x 16   pair i at 64 + (i mod N) * 16: f64 virtual s, f64 host s
// A pair is written before the count that covers it. A reader takes the
// count, reads pairs well inside the last N, and reads the count again.
//
using System;
using System.IO;
using System.IO.MemoryMappedFiles;
using System.Threading;
using Antmicro.Renode.Core;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Testing
{
    public static class VhilClockExtensions
    {
        public static void EnableVhilClock(this Emulation emulation, string path)
        {
            var clock = new VhilClock(path);
            emulation.MasterTimeSource.SyncHook += clock.OnSync;
            clocks.Add(clock);   // the file stays mapped for the emulation's life
        }

        private static readonly System.Collections.Generic.List<VhilClock> clocks =
            new System.Collections.Generic.List<VhilClock>();
    }

    public class VhilClock
    {
        public VhilClock(string path)
        {
            file = MemoryMappedFile.CreateFromFile(path, FileMode.Create, null, Header + Capacity * Pair);
            view = file.CreateViewAccessor(0, Header + Capacity * Pair);
            var magic = System.Text.Encoding.ASCII.GetBytes("VHILCLK1");
            view.WriteArray(0, magic, 0, magic.Length);
            view.Write(8, 0UL);
            view.Write(16, (ulong)Capacity);
        }

        public void OnSync(TimeInterval virtualNow)
        {
            var host = (DateTime.UtcNow - Epoch).TotalSeconds;
            var at = Header + (long)(count % Capacity) * Pair;
            view.Write(at, virtualNow.TotalSeconds);
            view.Write(at + 8, host);
            Thread.MemoryBarrier();
            count++;
            view.Write(8, count);
        }

        // 16 s of history at the bench's 500 us quantum.
        private const int Capacity = 32768;
        private const int Header = 64;
        private const int Pair = 16;
        private static readonly DateTime Epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        private readonly MemoryMappedFile file;
        private readonly MemoryMappedViewAccessor view;
        private ulong count;
    }
}
