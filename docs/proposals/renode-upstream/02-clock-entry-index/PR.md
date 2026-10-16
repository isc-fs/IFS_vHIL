ClockSource: Find clock entries by handler with a dictionary

### Related issue

Part of renode/renode#ISSUE_B. Builds on renode/renode-infrastructure#PR_01:
the first commit here is that PR's bug fix, please review this one after it.

### Description

Performance improvement in `BaseClockSource`, no behaviour change. Two
commits on top of #PR_01:

**ClockSource: Find clock entries by handler with a dictionary.** Every timer
reload or read (`ExchangeClockEntryWith`, `GetClockEntry`: a timer model
reading its value or reloading its limit) and every `Machine.ScheduleAction`
(`AddClockEntry`, then `TryRemoveClockEntry` when the action runs) finds its
entry with a linear search comparing delegates, allocating a closure each
time. On a two-machine STM32H7 workload (host `perf`, steady state) that
search (`Misc.IndexOf`, `Delegate.Equals`) was 5.5 % of all samples; with
this change it is 0.1 %.

This keeps a `Dictionary<Action, int>` from each handler to the index of its
first entry beside the list, updated when entries are added, replaced (when
the handler changes) or removed (the entries after a removed one move down by
one). It returns what the linear search returned:

- the same equality (`Delegate.Equals`, which `==` on delegates and the
  dictionary's default comparer both use);
- the first entry when a handler has several (which `AddClockEntry`
  prevents, but `ExchangeClockEntryWith`'s factory does not check);
- entries without a handler are still searched linearly (a dictionary key
  can't be null).

`EjectClockEntries` clears it with the list. Snapshots serialize it like the
list (Migrant rebuilds dictionaries entry by entry, so the delegates'
hash codes are recomputed on load).

**ClockSource: Use plain fields for the update flags.** `reupdateNeeded` and
`updateAlreadyInProgress` are `ThreadLocal<bool>`, read several times per
update. Both are only accessed with `sync` held: `Update` is only called
from `AdvanceInner`, which holds it and keeps it while the handlers run;
`GetClockEntry` holds it too. No other thread can see them set, so plain
fields behave the same without the thread-local look-ups. The list of
handlers already run is reused instead of allocated per update: only the
outermost `AdvanceInner` uses it (a nested one finds
`updateAlreadyInProgress` set). `Dispose` has nothing left to dispose.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository;
the numbers below come from the scripts in renode/renode#ISSUE_B.

Measured on master (Linux arm64 in Docker, 4 cores). The loops in
renode/renode#ISSUE_B use no timers, so this PR changes nothing there; its
cost shows on timer-heavy systems. On our STM32H733 systems (real-time
factor, steady state), the same change measured on 1.17.0: +6 % to +42 %
(median of 3, interleaved), and in a host profile (`perf`, two machines in
lock-step) the look-up went from 5.5 % of all samples to 0.1 %.

Host time, this PR alone against master (median of 5, interleaved, 4
cores): an STM32 timer with a 1 MHz update event (`stm32-timer` in our
measurements: STM32F4's TIM2, its update event looking up its own and its
channels' clock entries) runs 1.78x as fast (3.26 → 1.83 s for 0.25 s of
virtual time), allocating 63 % less; the bus loops, which use no timers,
are unchanged within this host's noise.

With the whole series (renode/renode#ISSUE_B, plus renode/renode#ISSUE_C's
hook exclusion; master vs master + every PR, median of 5, interleaved): the
bus-performance loop runs 2.76x as fast, a register read 2.24x, a register
write 1.91x, an STM32 timer's update path 2.64x; our STM32H733 systems (one
and two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 15-62 %
faster in steady state.

### Additional information

Test added: `TimeTests.ShouldFindEveryEntryByHandlerAfterAddingAndRemovingEntries`
(removals at the start, middle and end, re-adding, exchanging, then every
look-up, the duplicate check and the list order). It passes on master too:
it pins the behaviour the index must keep.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master 3e4301cf:

- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,119
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the 24 tests the series adds), and 47 Robot files (among them
  `timer-reconfiguration`, `virtual-time`, `precise-pause`, `cpu-step`, the
  snapshot suites, the Cortex-M platforms and the demos): 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails
  on master too)
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
