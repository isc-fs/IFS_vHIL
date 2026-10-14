// The clock-fuzz harness (run.sh): master's BaseClockSource update handlers
// (Orig*) against PR 09's on random entries and on sequences of updates;
// prints the cases, how many took the integer path, and every mismatch.

using System;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities;

delegate bool HandlerFn(ref ClockEntry e, TimeInterval t, ref TimeInterval n);

static class Program
{
    static Random rnd = new Random(12345);
    static ulong R(ulong max) => max == 0 ? 0 : (ulong)(rnd.NextInt64(long.MaxValue) % (long)Math.Min(max, (ulong)long.MaxValue));
    static ulong Edge(ulong lim)
    {
        switch(rnd.Next(8))
        {
            case 0: return 0;
            case 1: return 1;
            case 2: return lim - 1;
            case 3: return lim;
            case 4: return R(1000);
            default: return R(lim);
        }
    }
    static readonly ulong[] Freqs = { 32768, 32000, 1000, 1000000, 8000000, 64000000, 275000000, 550000000, 1000000000, 3, 7, 12345678, 2000000000, 100000 };
    static Fraction Ratio()
    {
        switch(rnd.Next(4))
        {
            case 0: return new Fraction(Freqs[rnd.Next(Freqs.Length)] * (ulong)(1 + rnd.Next(4)), 1000000000);
            case 1: return new Fraction(1 + R(2000000000), 1000000000);
            case 2: return new Fraction(1 + R(1UL << 31), 1 + R(1UL << 31));
            default: return new Fraction(1 + R(1UL << 33), 1 + R(1UL << 33));
        }
    }
    static Fraction Residuum(Fraction ratio)
    {
        var q = ratio.Denominator;
        switch(rnd.Next(4))
        {
            case 0: return Fraction.Zero;
            case 1:
            {
                // k/d with d | q, reduced (what the handlers produce)
                var d = q;
                for(var i = 0; i < 3 && d > 1; i++)
                {
                    var f = Misc.GCD(d, 2 + R(50));
                    if(f > 1) d /= f;
                }
                return d <= 1 ? Fraction.Zero : new Fraction(1 + R(d - 1), d);
            }
            case 2: return q <= 1 ? Fraction.Zero : new Fraction(1 + R(q - 1), q);
            default: { var d = 2 + R(1000); return new Fraction(1 + R(d - 1), d); }
        }
    }
    static string S(Fraction f) => $"{f.Numerator}/{f.Denominator}{(f.Minus ? "-" : "")}";
    static string Run(HandlerFn h, ClockEntry e, ulong t, ulong n0)
    {
        var n = new TimeInterval { Ticks = n0 };
        try
        {
            var r = h(ref e, new TimeInterval { Ticks = t }, ref n);
            return $"{r} V={e.Value} R={S(e.ValueResiduum)} En={e.Enabled} N={n.Ticks}";
        }
        catch(Exception ex) { return "EX " + ex.GetType().Name; }
    }
    static int Main(string[] args)
    {
        var n = long.Parse(args[0]);
        long fast = 0, mism = 0, seqSteps = 0;
        var lim = H.IntegerArithmeticOperandLimit;
        for(long i = 0; i < n; i++)
        {
            var ratio = Ratio();
            var e = new ClockEntry { Ratio = ratio, ValueResiduum = Residuum(ratio), Period = Edge(lim), Enabled = rnd.Next(2) == 0, WorkMode = rnd.Next(2) == 0 ? WorkMode.OneShot : WorkMode.Periodic };
            var asc = rnd.Next(2) == 0;
            e.Value = rnd.Next(4) == 0 ? Edge(lim) : (e.Period == 0 ? 0 : R(e.Period + 1));
            var t = Edge(rnd.Next(3) == 0 ? (1UL << 33) : lim);
            var n0 = rnd.Next(3) == 0 ? ulong.MaxValue : R(1UL << 40);
            if(H.TryGetEntryTicks(ref e, t, out _, out _, out _, out _)) fast++;
            HandlerFn o = asc ? H.OrigHandleDirectionAscendingPositiveRatio : H.OrigHandleDirectionDescendingPositiveRatio;
            HandlerFn w = asc ? H.HandleDirectionAscendingPositiveRatio : H.HandleDirectionDescendingPositiveRatio;
            var a = Run(o, e, t, n0); var b = Run(w, e, t, n0);
            if(a != b) { if(mism++ < 20) Console.WriteLine($"MISMATCH asc={asc} ratio={S(ratio)} res={S(e.ValueResiduum)} V={e.Value} P={e.Period} t={t}: {a} | {b}"); }
            // a sequence: evolve the same entry on both paths
            if(i % 100 == 0)
            {
                var eo = e; var ew = e;
                for(var k = 0; k < 200; k++)
                {
                    var tt = rnd.Next(5) == 0 ? R(lim) : R(5000000);
                    var no = new TimeInterval { Ticks = ulong.MaxValue }; var nw = no;
                    try
                    {
                        var ro = o(ref eo, new TimeInterval { Ticks = tt }, ref no);
                        var rw = w(ref ew, new TimeInterval { Ticks = tt }, ref nw);
                        seqSteps++;
                        if(ro != rw || eo.Value != ew.Value || S(eo.ValueResiduum) != S(ew.ValueResiduum) || eo.Enabled != ew.Enabled || no.Ticks != nw.Ticks)
                        {
                            if(mism++ < 20) Console.WriteLine($"SEQ MISMATCH step {k} ratio={S(ratio)}");
                            break;
                        }
                    }
                    catch(Exception) { break; }
                }
            }
        }
        Console.WriteLine($"cases={n} fast-path={fast} sequence-steps={seqSteps} mismatches={mism} limit=2^{Math.Log2(lim)}");
        return mism == 0 ? 0 : 1;
    }
}
