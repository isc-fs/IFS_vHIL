[FIX] ClockSource: Look the entry up again after running handlers

### Related issue

Fixes renode/renode#ISSUE_A

### Description

Bug fix in `BaseClockSource`.

`TryRemoveClockEntry` finds the entry's index, calls `UpdateLimits()` (which
runs the handlers of the entries that are due), then removes the entry at the
index it found before. `GetClockEntry` writes the entry back at the index it
found before running the entry's own handler, and reads it back by that index
after a full update (`triggerFullUpdate`), whose handlers can also run. A
handler that adds or removes a clock entry, such as a `Machine.ScheduleAction`
callback or a peripheral cancelling another timer, moves the entry in the
list, and then:

- `TryRemoveClockEntry` removes a different entry, or throws
  `ArgumentOutOfRangeException` when the index is past the end;
- `GetClockEntry` overwrites a different entry with this one: that entry is
  lost and this one is in the list twice.

This PR looks the entry up again after the handlers ran, in those three
places. If a handler removed the entry itself, `TryRemoveClockEntry` still
returns `true` (it was there when called) and `GetClockEntry` returns the
updated entry without writing it back. When no handler adds or removes
entries, which is the usual case, the second look-up finds the same index and
nothing changes.

### Usage example

Two NUnit tests in `TimeTests`, which fail on master and pass with this
change. An entry with a zero period is reached on every update, so its
handler runs inside `TryRemoveClockEntry`'s `UpdateLimits()` and inside
`GetClockEntry`; once armed, it removes an entry that comes before the one
being removed or read:

- `ShouldRemoveTheGivenEntryWhenAHandlerRemovesAnEntryBeforeIt`: entries
  `[A, Z, X, W]`, remove `X`; expected `[Z, W]`, master leaves `[Z, X]`
  (`W` was removed instead).
- `ShouldKeepOtherEntriesWhenAHandlerRunByGetClockEntryRemovesAnEntry`:
  entries `[A, Z, W]`, `GetClockEntry(Z)`; expected `[Z, W]`, master leaves
  `[Z, Z]` (`W` was overwritten).

A zero period is the simplest way to make a handler run at those points:
`UpdateLimits()` runs whatever is due, and `GetClockEntry` runs the entry's
handler when the read takes it to its limit. We have not seen this on a real
platform (an entry is rarely due exactly then); the tests construct it.

### Additional information

Found while working on a performance change to the clock source (a follow-up
PR, renode/renode#ISSUE_B), where indexing the entries by handler needs the index to be right
after handlers run.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master 3e4301cf:

- headless portable build (`./build.sh -t --no-gui`): succeeded, with this
  commit alone and with the whole series
- `UnitTests.csproj` with the two new tests and master's `BaseClockSource`:
  the two tests fail, everything else passes; with this change: all pass
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,119
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0, the difference being
  the 24 tests the series adds), and 47 Robot files around the time
  framework, the system bus, the register framework, CPU hooks, pausing,
  snapshots and Cortex-M platforms: 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails
  on master too)
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
