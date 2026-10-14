ClockSource: Compute clock entry updates in integers when exact

### Related issue

Part of renode/renode#ISSUE_B.

### Description

Performance improvement in `BaseClockSource`, no behaviour change. Two
commits:

**LimitTimerTests: Test fractional ticks and limits.** Timers whose
frequency doesn't divide the emulator's tick rate (1 GHz) count in fractions
of a tick (`ClockEntry.ValueResiduum`) and reach their limit between two
emulator ticks. The new tests check the value after each of 100 uneven
advances in both directions, and that the timer goes off on the first
emulator tick after its limit and not before, for a ratio with a small
denominator (3 MHz, 3/1000) and a large one (32,768 Hz, 4096/125,000,000),
with operands below and above 2^31 (so both paths of the next commit). They
pass on master too: they pin what the next commit must keep.

**ClockSource: Compute clock entry updates in integers when exact.** On every
clock source update, `HandleDirectionAscendingPositiveRatio` /
`HandleDirectionDescendingPositiveRatio` run for each enabled entry and do
its arithmetic in `Fraction`: `emulatorTicks * Ratio + ValueResiduum`, the
integer and fractional parts, then `(Period - Value - ValueResiduum) /
Ratio` for the ticks to the limit. Almost every one of those operations
takes a GCD or an LCM (`Misc.GCD`, a loop of divisions).

For an entry whose ratio is p/q (always reduced: `new Fraction(frequency,
TicksPerSecond)`) and whose residuum is a multiple of 1/q, every value
involved is a multiple of 1/q, so the same results come from integers:

- the entry ticks in `t` emulator ticks are x/q with x = t * p + r * q
  (r the residuum), of which x / q are whole and (x % q)/q is the new
  residuum, built with `new Fraction(x % q, q)`, which reduces it as the
  `Fraction` path's result is reduced;
- the emulator ticks to the limit are d/p with d = (Period - Value) * q -
  x % q (ascending) or Value * q - x % q (descending), rounded up as
  before (`d / p`, plus one if `d % p != 0` and the quotient is below
  `uint.MaxValue`).

Every `Fraction` the handlers produce is reduced (the fractional part of a
reduced fraction is reduced; `+` reduces; `*` by an integer cross-reduces),
so the residuum comes out in the same form, numerator and denominator, not
only the same value. The residuum is a multiple of 1/q unless the entry's
step changed without a new frequency (`ClockEntry.With(step:)` keeps the
residuum); then, and when any of p, q, `t`, `Period` or `Value` is 2^31 or
more (where `x` could pass 2^63), or for a negative or zero ratio, the
handlers take the `Fraction` path as before.

The range covers the usual cases: q divides 10^9, so it is below 2^31 for
every frequency (125,000,000 for 32,768 Hz, 1,000 for 3 MHz, 1 for any
multiple of 1 GHz); `t` is the time since the last update (2.1 s at 2^31
ns); periods of 16-bit timers and most 32-bit ones are below 2^31.

Checked against the `Fraction` path outside Renode: a harness (in our
measurement tooling, `tooling/clock-fuzz/`) runs master's two handlers and
the new ones on 20 million random entries (realistic and arbitrary ratios,
residua with and without a denominator dividing q, values, periods and
times at and around 0, 1 and 2^31, both directions, one-shot and periodic)
and 200,000 sequences of 200 updates each on both paths: no difference in the result,
the value, the residuum (numerator and denominator), the enabled flag or the
nearest limit; 39 % of the random cases took the integer path. The same
harness reports thousands of differences for a deliberately wrong rounding.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository.

Every enabled clock entry goes through these handlers on every update.
The gain is small, and below what we can resolve in host time:
`tooling/bench/stm32-timer.resc` in our measurements (STM32F4's TIM2 at
10 MHz with a 1 MHz update event, a Cortex-M4 at 100 MIPS spinning), Linux
arm64 in Docker, 4 cores, interleaved, gives 3.26 s for master and 3.43 s
with this PR (median of 5; fastest runs 3.17 and 3.14 s), and 1.29 vs 1.30 s
for the whole series with and without it (median of 7): within this host's
noise (about ±10 % on that workload). That workload's update work is
dominated by the timer's own clock entry exchanges and look-ups, which PRs
02 and 08 remove.

With PR 08 (STM32_Timer's update event), on our STM32H733 firmware the host
profile's share of `BaseClockSource.Advance` (inclusive, steady state) went
from 4.8 % to 3.8 % and from 6.6 % to 5.3 % of all samples on two boards.

With the whole series (renode/renode#ISSUE_B, plus renode/renode#ISSUE_C's
hook exclusion; master vs master + every PR, median of 5, interleaved): the
bus-performance loop runs 2.76x as fast, a register read 2.24x, a register
write 1.91x, an STM32 timer's update path 2.64x; our STM32H733 systems (one
and two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 15-62 %
faster in steady state.

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master
3e4301cf:

- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,119
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the 24 tests the series adds), and 47 Robot files (among them every STM32
  platform suite, `timer-reconfiguration`, `virtual-time`,
  `opcodes-execution-hooks` and `execution-tracing`): 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails
  on master too)
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
- the new `LimitTimerTests` (3 tests, 20 cases) pass on master's code and
  with the change
