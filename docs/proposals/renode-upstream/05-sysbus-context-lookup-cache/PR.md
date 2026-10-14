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

`TryGetValue` now keeps each thread's last successful look-up in a
`[ThreadStatic]` entry (owner, key, state, value, version), reused in place,
and returns it while the dictionary's `version` is unchanged. The version
changes under the lock whenever a value the method returned could change:
in `DropCaches`, which every mutation calls (`WithStateCollection`,
`RemoveContextKey`), and in `AddContextKey` when it adds a key. The latter
matters because `AddContextKey` replaces `cpuAllAccess[key]` without dropping
the caches, and `RemoveContextKey` leaves the old `cpuAllAccess` value in
place, so remove-then-add of the same key changes what a look-up returns.
Failed look-ups are not cached; the cached value is the same object the
locked path would return. The version is read with `Volatile.Read` and
written with `Volatile.Write` under the lock.

Being per thread, the entry needs no allocation and no synchronisation of
its own, and SMP machines (one bus, a thread per core) don't evict each
other's entries; a thread that alternates between machines (serial
execution) just takes the locked path more often.

Caveat: the per-thread entry holds a reference to the dictionary, the key and
the value of the thread's last look-up until that thread looks up again, so a
thread that outlives a machine (e.g. the Monitor's) can keep one cleared
machine's bus collections reachable a little longer. It is one entry per
thread and generic instantiation; if that matters, `Clear`/`Dispose` could
reset the calling thread's entry, or the entry could hold a
`WeakReference` to the owner.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository;
the numbers below come from the scripts in renode/renode#ISSUE_B.

This PR removes a lock and dictionary look-ups per access, not
allocations (the loops in renode/renode#ISSUE_B allocate the same).
The system bus changes of the series (03, 04, 05) measured together on
1.17.0 on our STM32H733 systems: +4 % to +51 % in steady state (median of
3), most on the board whose firmware is mostly register traffic (SPI to a
chain of battery monitors, SD card).

With the whole series (renode/renode#ISSUE_B; master vs master + the seven
PRs, median of 5, interleaved): the bus-performance loop runs 2.38x as fast,
a register read 1.69x, a register write 2.23x; our STM32H733 systems (one and
two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 13-53 % faster in
steady state.

### Additional information

The per-core registrations, bus isolation and TrustZone (state-aware
look-ups) Robot suites exercise the cached paths; see below.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 59f8f5d0 with renode-infrastructure master 5474222a:

- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,099
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the four tests the series adds), and 38 Robot suites (among them
  `per-core-registration`, `bus-isolation`, `offset-bus-registrations` and
  `arm-cortex-m-trustzone`, which use per-initiator and state-aware
  look-ups): 330 passed, 1 skipped, 0 failed, as on master
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
