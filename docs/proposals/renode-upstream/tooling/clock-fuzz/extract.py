"""Builds Handlers.cs for the clock-fuzz harness (Program.cs): master's two
BaseClockSource update handlers (renamed Orig*) and PR 09's, with the helpers
they call, over stand-ins for ClockEntry and TimeInterval that keep only what
the handlers use. Fraction.cs and Misc's GCD/LCM are master's.

    python3 extract.py <master BaseClockSource.cs> <PR 09 BaseClockSource.cs> <out dir>

then, with master's src/Emulator/Main/Utilities/Fraction.cs copied into <out dir>
(run.sh does all of it):
    dotnet run -c Release -- <cases>
"""
import re
import sys


def grab(src, name):
    i = src.rfind('\n', 0, src.index(name)) + 1
    j = src.index('{', src.index(')', i))
    depth = 0
    k = j
    while True:
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1


orig = open(sys.argv[1]).read()
new = open(sys.argv[2]).read()
parts = [grab(orig, 'private static bool ' + n).replace('HandleDirection', 'OrigHandleDirection')
         for n in ['HandleDirectionDescendingPositiveRatio', 'HandleDirectionAscendingPositiveRatio']]
parts += [grab(new, n) for n in ['private static bool TryGetEntryTicks', 'private static Fraction Residuum',
                                 'private static ulong WholeTicksToLimit',
                                 'private static bool HandleDirectionDescendingPositiveRatio',
                                 'private static bool HandleDirectionAscendingPositiveRatio']]
limit = re.search(r'private const ulong IntegerArithmeticOperandLimit = [^;]+;', new).group(0)
body = '\n\n'.join(parts) + '\n        ' + limit.replace('private', 'public') + '\n'
open(sys.argv[3] + '/Handlers.cs', 'w').write('''using System;
using Antmicro.Renode.Utilities;
namespace Antmicro.Renode.Time {
public enum WorkMode { Periodic, OneShot }
public struct TimeInterval { public ulong Ticks; public TimeInterval WithTicksMin(ulong t) => new TimeInterval { Ticks = Math.Min(Ticks, t) }; }
public struct ClockEntry {
  public ulong Value; public Fraction ValueResiduum; public ulong Period; public bool Enabled; public WorkMode WorkMode; public Fraction Ratio;
  public ClockEntry With(bool? enabled = null) { var r = this; r.Enabled = enabled ?? Enabled; return r; }
}
public static class H {
''' + body.replace('private static', 'public static') + '''
}}
namespace Antmicro.Renode.Utilities { public static class Misc {
  public static ulong LCM(ulong a, ulong b) { return a / GCD(a, b) * b; }
  public static ulong GCD(ulong a, ulong b) { while(a != 0 && b != 0) { if(a > b) { a %= b; } else { b %= a; } } return a | b; }
}}
''')
