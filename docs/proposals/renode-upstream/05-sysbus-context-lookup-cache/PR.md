SystemBus: Keep each thread's last context look-up

### Related issue

Part of renode/renode#ISSUE_B.

### Description

Performance improvement in `SystemBus`, no behaviour change.

Every bus access looks up its initiator's peripheral collection
(`ContextKeyDictionary.TryGetValue`): a lock and one to three dictionary
look-ups (`cpuAllAccess`, or `cpuInStateCache` and the per-state cache when
the access carries an initiator state). A CPU
thread asks for the same initiator and state on almost every access.

`TryGetValue` now keeps each thread's last successful look-up (key, state,
value, version) and returns it while the dictionary's `version` is
unchanged. The version changes under the lock whenever a value the method
returned could change: in `DropCaches`, which every mutation calls
(`WithStateCollection`, `RemoveContextKey`), and in `AddContextKey` when it
adds a key. The latter matters because `AddContextKey` replaces
`cpuAllAccess[key]` without dropping the caches, and `RemoveContextKey`
leaves the old `cpuAllAccess` value in place, so remove-then-add of the same
key changes what a look-up returns. Failed look-ups are not cached; the
cached value is the same object the locked path would return. The version is
read with `Volatile.Read` and written with `Volatile.Write` under the lock.

The entries belong to the dictionary, one per thread that looked something
up (a `Dictionary<int, LastLookup>` by managed thread id, created and used
under the lock, `[Transient]` for snapshots), and each thread refers to its
last one through a `[ThreadStatic]` `WeakReference<LastLookup>`, retargeted
(`SetTarget`) instead of reallocated. So:

- a hit costs a weak reference dereference and four compares, with no lock,
  no allocation and no synchronisation of its own; only the owning thread
  reads or writes an entry;
- SMP machines (one bus, a thread per core) don't evict each other's
  entries; a thread that alternates between machines (serial execution)
  takes the locked path when it switches, then hits again;
- a thread that outlives a machine (the Monitor's, a thread pool thread)
  doesn't keep the machine's bus collections (or the initiator CPU, and
  through it the machine) alive: its weak reference is all that points at
  the entry once the dictionary is garbage.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository;
the numbers below come from the scripts in renode/renode#ISSUE_B.

This PR removes a lock and dictionary look-ups per access, not
allocations (the loops in renode/renode#ISSUE_B allocate the same).
The system bus changes of the series (03, 04, 05) measured together on
1.17.0 on our STM32H733 systems: +4 % to +51 % in steady state (median of
3), most on the board whose firmware is mostly register traffic (SPI to a
chain of battery monitors, SD card).

Host time: alone, within this host's noise on the loops (±20 % run to
run there); within the whole series, leaving this PR out makes a bus read
12 % and a register read 7 % slower (median of 7, interleaved).

With the whole series (renode/renode#ISSUE_B, plus renode/renode#ISSUE_C's
hook exclusion; master vs master + every PR, median of 5, interleaved): the
bus-performance loop runs 2.76x as fast, a register read 2.24x, a register
write 1.91x, an STM32 timer's update path 2.64x; our STM32H733 systems (one
and two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 15-62 %
faster in steady state.

### Additional information

The per-core registrations, bus isolation and TrustZone (state-aware
look-ups) Robot suites exercise the cached paths; see below.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master 3e4301cf:

- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,119
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the 24 tests the series adds), and 47 Robot files (among them
  `per-core-registration`, `bus-isolation`, `offset-bus-registrations` and
  `arm-cortex-m-trustzone`, which use per-initiator and state-aware
  look-ups): 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails
  on master too)
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
