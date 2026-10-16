SystemBus: Skip locked range look-ups and the tag search's closure

### Related issue

Part of renode/renode#ISSUE_B.

### Description

Performance improvement in `SystemBus`'s access path, no behaviour change.
Two small commits:

**SystemBus: Skip the locked range look-ups while no range is locked.**
Every bus access calls `IsAddressRangeLocked`, which takes the context
dictionary's lock twice, does up to three dictionary look-ups and calls
`ContainsOverlappingRange` (`ranges.Any` with a closure) on up to two
collections, although most platforms never lock an address range. A `volatile bool` set by
`SetAddressRangeLocked` the first time a range is locked (inside its paused
state, before the range is added) lets it return `false` without the
look-ups until then. No collection holds a range before the first lock, so
the answer is the same; once set the flag stays set (unlocking takes the
usual path). It is serialized with the bus, so a snapshot taken after a
range was locked restores it set; a snapshot from a build without the field
would load it unset, but snapshots are not portable across Renode builds
anyway.

**SystemBus: Look up tags without allocating a closure.** Every access calls
`TryGetTag`, whose `Misc.BinarySearch(r => address.CompareTo(r.StartAddress))`
allocates a closure capturing the address. A static `FindTagIndex` runs the
same algorithm (same bounds, midpoints and comparisons, so the same index or
insertion point) without it.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository;
the numbers below come from the scripts in renode/renode#ISSUE_B.

Measured on master (Linux arm64 in Docker, 4 cores), with the loops in
renode/renode#ISSUE_B (.NET's allocation counter, deterministic to 1 %):

| per access | master | this PR |
|---|---|---|
| bus read (`TrivialPeripheral`) | 658 B | 377 B |
| register read (`STM32_GPIOPort` IDR) | 786 B | 507 B |
| register write (`STM32_GPIOPort` OSPEEDR) | 1,360 B | 1,077 B |

Most of it is `IsAddressRangeLocked`'s `ContainsOverlappingRange`
(`ranges.Any` with a closure: a closure and an enumerator per collection).

Host time, this PR alone against master (median of 5, interleaved, 4
cores; this host's bus loops vary by up to ±20 % run to run): bus read
1.45x as fast (5.00 → 3.45 s for 0.25 s of virtual time; fastest runs 4.20
→ 2.78 s), register read 1.40x, register write 1.14x.

With the whole series (renode/renode#ISSUE_B, plus renode/renode#ISSUE_C's
hook exclusion; master vs master + every PR, median of 5, interleaved): the
bus-performance loop runs 2.76x as fast, a register read 2.24x, a register
write 1.91x, an STM32 timer's update path 2.64x; our STM32H733 systems (one
and two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 15-62 %
faster in steady state.

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master 3e4301cf:

- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,119
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the 24 tests the series adds), and 47 Robot files (among them `sysbus`,
  `sysbus-tag`, `bus-isolation`, `per-core-registration`,
  `arm-cortex-m-trustzone` and `memory-access`): 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails
  on master too)
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
