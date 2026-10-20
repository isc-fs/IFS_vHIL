SlaveTimeSource: Optionally run on the master's thread

### Related issue

renode/renode#ISSUE_D.

### Description

New opt-in mode for `SlaveTimeSource`, off by default; nothing changes
unless it is turned on.

A slave time source hands every grant from its master to its own sinks on a
dispatcher thread: per quantum, the master wakes the slave's thread, the
slave's thread wakes its sinks' threads (a machine's CPU threads), and the
reports come back the same way. A machine created with a time source of its
own (`new Machine(createLocalTimeSource: true)`) therefore costs two thread
hand-offs a quantum instead of one, and N such machines run 2N + 1 threads
that spin while they wait. Machines on the master time source (`mach
create`, the default since 63702bc7, "[#32007] Machine: Use
MasterTimeSource by default") are not affected.

`SlaveTimeSource.RunsOnMasterThread`, set before the slave is attached to
its master, removes the slave's thread. Three commits:

**TimeSourceBase: Split `InnerExecute` into a begin and an end half.** A
refactor with no behaviour change: `InnerExecuteBegin` takes the lock,
updates the nearest sync point, latches the handles and grants them time
(or unblocks them); `InnerExecuteEnd` waits for them, unlatches them and
runs the sync phase. `InnerExecute` calls one after the other, so the same
steps run in the same order on the same thread; in serial execution
everything still happens in the second half. `PhaseExecutor` gets the
matching split (every phase but the last, then the last).

**TimeHandle: Allow requesting a time interval without waiting.**
`RequestTimeInterval(noWait: true)` returns `false` instead of waiting for a
grant or for the previous unlatch, and on a blocking handle unblocks it in
place instead of waiting for the source to unblock it: for a sink whose
source makes the request on the source's own thread, where waiting would
wait for itself. The default is unchanged.

**SlaveTimeSource: Optionally run on the master's thread.** With
`RunsOnMasterThread`, the master begins the slave's iteration when it grants
the slave time or unblocks it (`ExecuteGrantPhase` / `ExecuteUnblockPhase`
call `BeginOnMasterThread`: the slave requests the interval with `noWait`
and runs `InnerExecuteBegin`, granting it to its sinks) and ends it when it
waits for the slave (`ExecuteWaitPhase` calls `EndOnMasterThread`: the
slave runs `InnerExecuteEnd`, waiting for its sinks and running its sync
phase, and reports back before the master's `WaitUntilDone`). A grant
longer than the slave's quantum (a residuum) is run quantum by quantum in
the second half, as `DispatchInner` does. `TimeDomainsManager` gets
`SwapCurrentThread`, so the slave's work on the master's thread is stamped
with the slave's time, as on its own thread.

How it relates to the existing semantics:

- The slave's sinks still run on their own threads, granted in the
  master's grant phase beside the master's other sinks, so machines still
  run in parallel; only the hop through the slave's thread is gone.
- Each machine keeps its own time source: its own quantum, sync points,
  `SyncHook`, delayed actions (`ExecuteInSyncedState`,
  `ExecuteInNearestSyncedState`), `Pause`/`Resume` and blocking, as with the
  thread.
- The slaves' sync phases run on the master's thread, one after another in
  the master's wait phase, instead of concurrently on their threads. The
  master waited for every slave before its own sync phase anyway, so a
  quantum still ends when the last slave is done; what changes is that two
  slaves' sync actions never overlap (and a long one delays the slaves
  waited for after it).
- `IsOnSyncPhaseThread` of the slave is true on the master's thread during
  the slave's sync phase, as on the slave's thread before.
- The slave no longer calls `WaitIfBlocked` (a 10 ms wait that only saves
  host CPU while a sink is blocked; the comment there says the framework
  does not need it): the master's own loop waits on its own event.
- Lock order: the master holds its `sync` while it takes the slave's
  (master, then slave), the order in which a thread could already hold
  both.
- The mode is not snapshotted state: the property is set by whoever creates
  the slave, and the per-iteration fields are `[Transient]` (a snapshot is
  taken between iterations).

Opt-in or default: this PR makes it opt-in, for review. The slave time
source tests pass both ways, as do the time-framework suites and the
multi-node suites with every machine on a slave time source (below), so it
could become the default for `createLocalTimeSource` machines; we left that
to you. Nested slaves (a slave of a slave, both on their master's thread)
are not tested.

### Usage example

```python
from Antmicro.Renode.Core import EmulationManager, Machine
m = Machine(True)                             # a time source of its own
m.LocalTimeSource.RunsOnMasterThread = True   # before AddMachine attaches it
EmulationManager.Instance.CurrentEmulation.AddMachine(m, "m0")
```

Measured on master and master + this PR (Linux arm64 in Docker, 4 cores,
median of 7, interleaved; host seconds for the virtual time given, and the
process's CPU seconds per virtual second; the setup in renode/renode#ISSUE_D):

| Two machines | master time source (`mach create`) | slave time sources (master) | slave time sources, this PR, off | slave time sources, this PR, `RunsOnMasterThread` |
|---|---|---|---|---|
| two Cortex-M4s spinning, 10 us quanta, 0.5 s | 0.68 s, 5.1 CPU s/s | 1.31 s, 9.8 | 1.29 s, 9.4 | **1.00 s, 7.6** |
| `nrf52840-ble-zephyr.resc` (BLE central + peripheral, 10 us quanta), 5 s | 2.87 s, 2.1 CPU s/s | 8.01 s, 6.1 | 8.09 s, 6.1 | **3.83 s, 2.8** |

With the mode on, the BLE demo on slave time sources runs 2.1x as fast as
on master and takes 54 % less CPU: 1.3x the host time of the same demo on
the master time source instead of 2.8x. The spinning pair runs 1.31x as
fast (-23 % CPU), as each of its 50,000 quanta still waits for two busy CPU
threads. With the mode off
(and for machines on the master time source), nothing changes beyond this
host's noise (within ±2 %).

### Additional information

Tests: the eleven `SlaveTimeSource` tests in `TimeSourceTests` (same and
different quanta, not-aligned quanta, disconnection, delayed actions in
synced states, blocking sinks, blocking at the end of the interval) are
parametrised to run with and without `RunsOnMasterThread`.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master 3e4301cf:

- headless portable build (`./build.sh -t --no-gui`): succeeded with the
  PR, and with its first two commits alone (whose `UnitTests` pass as on
  master, 621 passed)
- the NUnit projects of `tests/tests.yaml` (`UnitTests`, `PeripheralsTests`,
  `MonitorTests`, `RenodeTests`): 1,106 passed, 18 skipped, 0 failed
  (master: 1,095 / 18 / 0; the difference is the eleven slave time source
  tests' second case), and 47 Robot files (among them `virtual-time`,
  `precise-pause`, `pause-without-sleep`, `cpu-pause-halt`,
  `real-time-clock-mode`, `timer-reconfiguration`, `instructions-counting`,
  `cpu-step`, the snapshot suites, the Cortex-M platforms and the demos):
  422 passed, 5 skipped, 3 failed, the same as master (the failures are in
  `ramn.robot` and fail on master too)
- Robot suites with every machine on a slave time source (a test-only
  change to `Machine`'s constructor, not part of this PR), on its thread
  and on the master's: ten files (the time-framework suites above without
  the precise pauses, `sync_pc`, `snapshot-tracking`, and the multi-node
  `NRF52840`, `nucleo_h753zi` and `MCAN`), 98 cases: master (thread) 68
  passed, 30 failed, 1 skipped; this PR, thread 67 / 30 / 2; this PR,
  `RunsOnMasterThread` 69 / 29 / 1. The same 28 cases fail in all three:
  slave time sources are a path the suites don't otherwise take, and they
  fail with it on master already (timer and instruction-count checks, most
  of `nucleo_h753zi`). The rest differ only by cases flaky on master's
  threads as well (`cpu-pause-halt`'s ten cases run three times per mode:
  2, 3 and 2 of the 30 runs fail on master's threads, this PR's threads and
  `RunsOnMasterThread`, among the halt/pause/reset cases, a different one
  each time; `MCAN`'s Linux boot and ISO-TP exchange end close to the 120 s
  test timeout used here). Separately: with a slave time
  source, a precise pause (`precise-pause.robot`) never returns on master,
  reported in renode/renode#ISSUE_D.
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, two and three
  boards in lock-step, each on a slave time source of its own, CAN between
  them, 10 s of virtual time from power-on), every CAN frame's timing in ns,
  every GPIO edge with the instruction count at its write, radio payloads,
  and each CPU's executed instructions, PC, register state and RAM digest
  are the same with `RunsOnMasterThread` as on master (and with this PR,
  mode off). There, three boards in lock-step run 26-27 % faster (boot and
  steady state, -24 % CPU) and two boards take 7-11 % less CPU at the same
  speed (the ECU firmware's own work dominates on master, which lacks our
  other patches); one board, and our wall-clock path that has no slave
  time sources, are unchanged. With our other patches on 1.17.0, the same
  change made two and three boards 20-23 % faster in steady state with
  29-36 % less CPU (x86, 4 vCPUs)
