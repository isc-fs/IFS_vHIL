//
// Real-time pacing without catch-up, for the wall-clock bench (vhil/bench.py).
// Compiled by Renode at load time (`include @models/renode/VhilPacer.cs`).
//
//     emulation SetGlobalAdvanceImmediately true
//     emulation EnableVhilPacer 0.05
//
// Renode's own pacing compares cumulative virtual and host time, so every slow
// stretch (a machine reset, an ELF reload, a busy CI runner) is a deficit it
// later repays by running flat out: IFS_HIL's 1 Hz health frame arrived every
// 0.47 s on CI. This pacer holds virtual time to wall time at each sync point
// and forgives a deficit larger than maxLagSeconds instead of repaying it.
//
using System;
using System.Diagnostics;
using System.Threading;
using Antmicro.Renode.Core;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Testing
{
    public static class VhilPacerExtensions
    {
        public static void EnableVhilPacer(this Emulation emulation, double maxLagSeconds = 0.05)
        {
            var pacer = new VhilPacer(maxLagSeconds);
            emulation.MasterTimeSource.SyncHook += pacer.OnSync;
        }
    }

    public class VhilPacer
    {
        public VhilPacer(double maxLagSeconds)
        {
            maxLag = maxLagSeconds;
        }

        public void OnSync(TimeInterval virtualNow)
        {
            var v = virtualNow.TotalSeconds;
            var wall = clock.Elapsed.TotalSeconds;
            if(!started)
            {
                Rebase(v, wall);
                started = true;
                return;
            }
            // Positive: virtual time is ahead of the wall clock, wait for it.
            var ahead = (v - virtualRef) - (wall - wallRef);
            if(ahead >= 0.001)
            {
                Thread.Sleep(TimeSpan.FromSeconds(ahead));
            }
            else if(-ahead > maxLag)
            {
                Rebase(v, wall);   // behind: forgive, never sprint to catch up
            }
        }

        private void Rebase(double v, double wall)
        {
            virtualRef = v;
            wallRef = wall;
        }

        private readonly double maxLag;
        private readonly Stopwatch clock = Stopwatch.StartNew();
        private double virtualRef;
        private double wallRef;
        private bool started;
    }
}
