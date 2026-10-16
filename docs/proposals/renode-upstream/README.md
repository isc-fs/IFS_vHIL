# Upstreaming the Renode patch queue

The vHIL builds Renode 1.17.0 with nine local patches to renode-infrastructure
and two to tlib ([`docker/renode/patches/`](../../../docker/renode/patches/),
study: [`../renode-fork-study.md`](../renode-fork-study.md)). This directory
holds them rewritten as pull requests to the three upstream repositories,
rebased on upstream master, split the way upstream reviews, tested with
Renode's own suites and measured on upstream's own workloads. **Nothing here
has been posted**: Raul submits them after signing the CLA.

Which repository each change belongs in:

- [renode/renode-infrastructure](https://github.com/renode/renode-infrastructure),
  the emulator's C# core: PRs 01-08, 11, 13, 14, 15 and 17;
- [antmicro/tlib](https://github.com/antmicro/tlib), the translation library
  (C, a submodule of renode-infrastructure at `src/Emulator/Cores/tlib`):
  PR 10, which PR 11 needs, and PR 16, which PR 17 needs;
- [renode/renode](https://github.com/renode/renode), which carries the
  submodule pointer (Antmicro bumps it when a change lands), the Robot
  tests and the issues: PR 12 (tests for 10 + 11), 14's change to one
  Robot expectation, PR 18 (tests for 16 + 17), and issues A, B, C, D and
  E.

## The pull requests

Base: renode master `9b5195e6`, renode-infrastructure master `3e4301cf`
(the commit renode master points at) and tlib master `0dd9a30c` (the commit
renode-infrastructure points at), the heads on 2026-10-14. PRs 01-07 were
first prepared on `59f8f5d0` / `5474222a`; their patches apply unchanged to
the new base (`git am`, same trees), so only 05's (reworked, below) is new.
PR 13 (local patch 0006, added 2026-10-15) is on the same base: the heads
had not moved. PRs 15-18 (local patches 0008 and tlib/0002, added
2026-10-16) are on renode master `d0c70ee3`, renode-infrastructure
`48cee122` (the commit it points at, also infrastructure's master head) and
tlib `0dd9a30c` (unchanged), the heads on 2026-10-16; local patch 0009 needs
no PR (below).

| # | Repository | Directory | Title | Commits | Size | Depends on |
|---|---|---|---|---|---|---|
| 01 | renode-infrastructure | [`01-clock-entry-stale-index/`](01-clock-entry-stale-index/) | [FIX] ClockSource: Look the entry up again after running handlers | 1 | +82/-7, 2 files (56 lines are tests) | - |
| 02 | renode-infrastructure | [`02-clock-entry-index/`](02-clock-entry-index/) | ClockSource: Find clock entries by handler with a dictionary | 2 | +149/-47 over 01, 2 files (34 lines are a test) | 01 |
| 03 | renode-infrastructure | [`03-sysbus-locked-ranges-and-tags/`](03-sysbus-locked-ranges-and-tags/) | SystemBus: Skip locked range look-ups and the tag search's closure | 2 | +37/-1, 1 file | - |
| 04 | renode-infrastructure | [`04-sysbus-access-context/`](04-sysbus-access-context/) | SystemBus: Set the access context without allocating | 1 | +83/-11, 2 files (27 lines are a test) | - |
| 05 | renode-infrastructure | [`05-sysbus-context-lookup-cache/`](05-sysbus-context-lookup-cache/) | SystemBus: Keep each thread's last context look-up | 1 | +75/-0, 1 file | - |
| 06 | renode-infrastructure | [`06-register-framework-allocations/`](06-register-framework-allocations/) | PeripheralRegister: Avoid allocations on every register access | 2 | +60/-30, 1 file | - |
| 07 | renode-infrastructure | [`07-cpu-hot-path-allocations/`](07-cpu-hot-path-allocations/) | TranslationCPU, Machine: Avoid allocations and noisy log formatting on hot paths | 2 | +25/-6, 4 files | - |
| 08 | renode-infrastructure | [`08-stm32-timer-unused-channels/`](08-stm32-timer-unused-channels/) | STM32_Timer: Leave unused compare channels alone on update events | 1 | +14/-1, 1 file | - |
| 10 | **antmicro/tlib** | [`10-tlib-pre-opcode-hook-exclusions/`](10-tlib-pre-opcode-hook-exclusions/) | Allow excluding a pre-opcode execution hook at an address | 1 | +89/-1, 4 files | - |
| 11 | renode-infrastructure | [`11-cpu-pre-opcode-hook-exclusions/`](11-cpu-pre-opcode-hook-exclusions/) | TranslationCPU: Allow excluding a pre-opcode execution hook at an address | 1 (+ the tlib bump) | +50/-0, 1 file | 10 (in the tlib submodule) |
| 12 | **renode/renode** | [`12-tests-pre-opcode-hook-exclusions/`](12-tests-pre-opcode-hook-exclusions/) | tests: Test excluding a pre-opcode execution hook at an address | 1 | +53/-0, 1 file (tests) | 11 (in `src/Infrastructure`) |
| 13 | renode-infrastructure | [`13-slave-time-source-on-master-thread/`](13-slave-time-source-on-master-thread/) | SlaveTimeSource: Optionally run on the master's thread | 3 | +371/-89, 5 files (+44/-33 in the tests) | - |
| 14 | renode-infrastructure (+ one renode/renode test) | [`14-stm32-timer-period/`](14-stm32-timer-period/) | [FIX] STM32_Timer: Count ARR + 1 values per update period | 1 (+ 1 in renode) | +199/-8, 2 files (159 lines are tests); renode: +2/-2 | - |
| 15 | renode-infrastructure | [`15-packet-field-attributes/`](15-packet-field-attributes/) | Packet: Look up each field's attributes once | 1 | +70/-18, 2 files (35 lines are a test) | - |
| 16 | **antmicro/tlib** | [`16-tlib-memory-access-io-only/`](16-tlib-memory-access-io-only/) | Allow reporting only IO accesses to the memory access hook | 1 | +17/-5, 3 files | - |
| 17 | renode-infrastructure | [`17-cpu-memory-access-io-only/`](17-cpu-memory-access-io-only/) | TranslationCPU: Allow a memory access hook for IO accesses only | 1 (+ the tlib bump) | +22/-2, 1 file | 16 (in the tlib submodule) |
| 18 | **renode/renode** | [`18-tests-memory-access-hook-io-only/`](18-tests-memory-access-hook-io-only/) | tests: Test a memory access hook limited to IO accesses | 1 | +77/-0, 2 files (tests) | 17 (in `src/Infrastructure`) |

Each directory has the `git format-patch` output against its repository's
master (`*.patch`, author and committer `Raul Moran <rmg@raulmoranguerra.com>`,
no trailers, ready to `git am`; 02's patches apply on top of 01's; 11's
and 17's patches are the C# change alone, without the commit that moves the
tlib submodule, see "Submitting") and `PR.md`: the title on its first line, then
the body, in the sections of upstream's PR template (Related issue,
Description, Usage example, Additional information). [`ISSUES.md`](ISSUES.md)
has the five issues to open first in renode/renode. The measurements:
[`results.md`](results.md). The scripts: [`tooling/`](tooling/).

What the series does, in one line each:

- **01** (bug fix): `TryRemoveClockEntry` and `GetClockEntry` use an index
  found before running handlers that can add or remove entries; with tests
  that fail on master.
- **02**: a handler-to-index dictionary instead of a linear delegate search
  on every timer reload/read and `ScheduleAction` (5.5 % of samples to
  0.1 %); plain fields for two `ThreadLocal<bool>` only touched under the
  lock.
- **03**: no locked-range look-ups until a range is ever locked; tag search
  without a closure.
- **04**: per-thread stack of reusable context entries instead of a `Tuple`,
  closure and `DisposableWrapper` per bus access.
- **05**: per-thread cache of the last context look-up, invalidated by a
  version; the entries belong to the dictionary and threads hold them
  weakly.
- **06**: register framework: changed-fields list only when a field
  changes, no `Distinct`/LINQ on the common path, unhandled-write messages
  built only when they can be logged.
- **07**: pause guard's `new object()` per bus access, LINQ before every
  execution chunk, noisy messages formatted before being dropped.
- **08**: `STM32_Timer`'s update event stops re-applying unused compare
  channels (eight clock entry exchanges per period on a timer with no
  channel in use).
- **10 + 11 + 12** (new API): a pre-opcode execution hook can be excluded at
  an address (`ExcludePreOpcodeExecutionHookAt`), so a hook that only has
  work at some of the addresses its mask matches stops costing a managed
  call at the others; tlib side, C# side, Robot tests.
- **13** (opt-in time-framework mode): a `SlaveTimeSource` can run on its
  master's thread (`RunsOnMasterThread`), so a machine with a time source
  of its own costs one thread hand-off a quantum instead of two, and N such
  machines N + 1 threads instead of 2N + 1. Three commits: `InnerExecute`
  split into a begin and an end half (refactor), `RequestTimeInterval`'s
  `noWait`, the mode itself; the slave time-source tests run both ways.
- **14** (bug fix): `STM32_Timer` raised its update event every ARR counts
  instead of ARR + 1 (the clock entry's limit was ARR), so every period a
  firmware derives from a timer ran fast by 1 / (ARR + 1): a 1 kHz HAL
  timebase 0.1 %. With NUnit tests that fail on master; one Robot test
  (`nucleo_wba52cg`'s input capture) encoded the old period and gets the
  chip's values, a renode/renode commit to land with the submodule bump.
- **15**: `Packet`'s field wrappers look each attribute up once, when they
  are built, instead of calling `GetCustomAttributes` for every attribute
  read of every field of every value decoded or encoded (`MCAN` decodes
  every filter element for every frame it receives).
- **16 + 17 + 18** (new mode of an existing API): a memory access hook can
  be limited to IO (peripheral) accesses (`SetHookAtMemoryAccess(hook,
  ioAccessesOnly: true)`, tlib's `MEMORY_ACCESS_EVENT_IO_ONLY`): RAM hits are
  not reported and translated code keeps the TLB fast path, so a hook that
  watches peripherals stops costing a helper call and a managed call per RAM
  access; tlib side, C# side, Robot tests.

How they map to the local queue:

| Local patch | Upstream PRs | Differences from the local patch |
|---|---|---|
| 0001 (P2, clock source) | 01 + 02 | The `TryRemoveClockEntry` re-lookup is its own bug-fix PR (01), extended to the same stale index in `GetClockEntry` (after the entry's own handler, and after a full update), with tests that fail on master. 02 keeps the first entry per handler (what the linear search found, also with duplicates), searches null handlers linearly, updates the index incrementally on removal, and has a test |
| 0002 (P3, system bus) | 03 + 04 + 05 | The tag search mirrors `Misc.BinarySearch` exactly. The access context restores the depth from before each push (stock's "put the previous context back", also out of order) and drops the thread-static cache. The context look-up cache is per thread, never allocates on a hit, is also invalidated when a context key is (re-)added, which stock code can do without dropping its caches, and is owned by the dictionary with threads referring to it weakly (no machine kept alive by a thread that outlives it) |
| 0003 (P5, allocations) | 06 + 07 | No reused scratch list for writes (it needed a re-entrancy flag): the changed-fields list is allocated when the first field changes; no enumerator boxing; log checks use `Logger.MinimumLogLevel <= level`, as `PL330_DMA` already does |
| 0004 (timers) | 08 | Only the timer half is submitted. The clock source's integer arithmetic (prepared as PR 09) was dropped: exact, but no measurable host-time gain |
| 0005 + tlib/0001 (hook exclusion) | 10 + 11 + 12 | tlib keys the set by (pc, hook id) pairs; the local patch packs them as `(pc << 3) \| id`, which collides for hook ids of 8 or more (`CPU_HOOKS_MASKS_LIMIT` is 256; harmless for the vHIL, whose only pre-opcode hook is id 0). Same API names and semantics, so `VhilFastForward` uses the upstream version as is; tests added |
| 0006 (slave time source on the master's thread) | 13 | Rebased onto upstream's simplified `InnerExecute` (#100387, July 2026: unblock the not-ready handles, else grant the ready ones), split into three commits (the `InnerExecute`/`PhaseExecutor` split alone first, then `noWait`, then the mode); `PhaseExecutor`'s now unused `Execute`/`ExecuteInParallel` give way to the two halves; the `noWait` branch simplified to the same outcomes; the per-iteration fields `[Transient]`. Same property and semantics, so `VhilMachine` turns it on as is |
| 0007 (STM32 timer period, a fidelity fix) | 14 | The same change; 14 adds `STM32_TimerTests` (four NUnit tests that fail on master) and the `nucleo_wba52cg` expectation. Unlike 0001-0006 it changes what a run computes, on purpose (isc-fs/IFS_vHIL#292) |
| 0008 (Packet attributes) | 15 | The attributes are read once, in the wrapper's constructor (read-only fields), instead of lazily into a `ConcurrentDictionary` per wrapper and a nullable byte-order flag; adds a `PacketTests` test (passes on master too). Same values |
| 0009 (NVIC RETTOBASE) | none | Upstream already has it: renode-infrastructure e7a8584c ("[GH#941] NVIC: Make ICSR.RETTOBASE take external interrupts into account", July 2026, after 1.17.0) computes RETTOBASE as `activeIRQs.Count() <= 1`, with no reflection or LINQ over the enum (`Count()` on a `Stack` is its `Count`). It is also a fidelity fix: it counts external interrupts too, where 1.17.0 and 0009 count only the system exceptions among the active ones. 0009 goes on the next bump; the vHIL then gets upstream's semantics |
| tlib/0002 (memory access flag 2) | 16 + 17 + 18 | Independent of tlib/0001 (PR 10): other files and lines. The mode gets a name (`enum memory_access_event`, `MEMORY_ACCESS_EVENT_IO_ONLY`) and `tlib_on_memory_access_event_enabled(2)` sets it and keeps the TLB fast path, where the local patch only made the softmmu helpers test bit 0 and `VhilFastForward` writes 2 into tlib's state by itself; 17 adds the C# API (`SetHookAtMemoryAccess(hook, ioAccessesOnly)`) the vHIL lacked, and 18 its tests. A run of the vHIL is the same: its raw write of 2 means IO only with 16 as with tlib/0002 |

The differences are review-driven, not behavioural: the vHIL's fingerprint
is byte-identical between master and master with every PR up to 13, and
with 15-18, as it was between 1.17.0 and 1.17.0 with the local queue. 14 is
a fidelity fix and changes it on purpose: only timing, by one timer count
per period (the vHIL's `docker/renode/README.md`).

## Will upstream want the hook exclusion (10-12)?

Probably, but it is the one part of the series that is a new API rather than
a fix or a speed-up, so it goes through an issue first (issue C) and may come
back redesigned. The case for it is general, not vHIL-specific: opcode
execution hooks are matched by mask only, and many hooks only have work at
some of the matching addresses and learn which after the first call there
(coverage of an instruction class, an inventory of where an instruction
occurs, detecting an idiom such as a busy-wait loop or a semihosting-like
magic sequence). Today such a hook pays a native-to-managed call at every
other execution, with no way out short of not hooking at all; with the
exclusion its cost follows the distinct addresses, not the executions
(results.md: a hook on every `nop` of a loop takes the loop from 0.023 s
to 0.68 s; excluding itself after the first call, 0.022 s). The change is small, off the translated code's
hot path (one compare per matching instruction at translation, only while
the set is non-empty) and invisible to existing users.

What may make Antmicro hesitate: it is a third public entry point on a
young API (opcode hooks came in 2025), its state lives outside `CPUState`
(not in snapshots, like the hooks themselves), and they may prefer another
shape (a hook returning "not here again", exclusions for post-opcode hooks
too, or address ranges at install time). Issue C offers to adapt. Whatever
they decide, the vHIL keeps the local patch until a release carries an
equivalent; if they decline, it stays local for good, which costs a rebase
of two small patches per Renode release.

## Will upstream want the slave time source on the master's thread (13)?

Less certain than the rest. Since 2022 (renode-infrastructure 63702bc7,
"[#32007] Machine: Use MasterTimeSource by default") every `mach create` machine runs on the
emulation's master time source; a machine gets a `SlaveTimeSource` of its
own only through `new Machine(createLocalTimeSource: true)`, which nothing in
renode, renode-infrastructure or the tests calls. So 13 speeds up a path
upstream itself moved away from: it helps whoever builds machines with a
time source of their own (an integration creating machines from C# or
Python, as the vHIL does for invariant 11: each board's timers on its own
time, so lock-step runs are deterministic), and nothing else. Issue D says
so, and asks whether they would rather have it on by default for such
machines, behind a `Machine` argument, or not at all.

What speaks for it: it is opt-in, off by default with no change, and the
time framework's own tests run both ways; with slaves it takes a
two-machine emulation from 8.01 s to 3.83 s of host time on Renode's own
BLE demo with its machines on slave time sources (2.87 s on the master time
source: from 2.8x that to 1.3x), the minimal two-machine loop from 1.31 s
to 1.00 s, and the vHIL's three-board lock-step system 27 % faster
(results.md). What may make Antmicro
hesitate: it touches the time framework's core loop (`InnerExecute`), which
they reworked in July (#100387), and it changes where a slave's sync phase
runs (the master's thread, one slave after another, instead of each
slave's thread); a precise pause hangs on a slave time source on master
already (results.md), so slaves are a path they test little. If they decline
it, the vHIL keeps patch 0006 (one patch to rebase per release).

## Will upstream want the IO-only memory access hook (16-18)?

Probably: it is a new mode of an existing API rather than a new mechanism,
small (17 lines of tlib, 22 of C#), off unless asked for, and it removes a
cost every peripheral-watching hook pays today (the run above 5.6 times
slower with a hook set; as fast as without one when it is limited to IO
accesses). Who gains upstream: MMIO tracing of a driver, the profiler's
peripheral entries (`EnableProfiling` logs `MemoryIORead`/`MemoryIOWrite`
as `PeripheralEntry`), and any detector or model that reacts to a CPU's
peripheral accesses. Like 10-12 it goes through an issue (E) first.

What may make Antmicro hesitate: the mode also reports slow (unaligned,
page-spanning) RAM accesses, because tlib's slow path serves IO and RAM
alike (the operation tells them apart; reporting exactly IO would need the
slow path to track which half was IO); they may prefer a separate hook for
peripheral accesses, or the flag on `ICPUWithMemoryAccessHooks`. Issue E
offers to adapt. It also mentions an older gap it does not fix: enabling
an every-access hook after code was translated keeps the blocks translated
with the TLB fast path, so on x86 hosts their RAM hits go unreported until
retranslation. If they decline, the vHIL keeps tlib/0002 (and its raw write
of the flag) as now.

## Local patch 0009: already upstream

The NVIC's RETTOBASE change needs no PR: renode-infrastructure e7a8584c
("[GH#941] NVIC: Make ICSR.RETTOBASE take external interrupts into
account", 2026-07-11, after 1.17.0) replaced the reflection and LINQ with
`activeIRQs.Count() <= 1`, constant time. It is a fidelity fix as well:
1.17.0 (and the vHIL's 0009, which kept its result) count only the system
exceptions among the active ones, upstream counts every active exception,
as ARMv7-M defines ICSR.RETTOBASE (set when no exception other than the
one being handled is active). The vHIL
drops 0009 on the next Renode bump and gets upstream's semantics then; on
FreeRTOS firmware, which reads ICSR for VECTACTIVE and not RETTOBASE, no
observable change is expected.

## Results in short

Details and every number: [`results.md`](results.md). Measured on a quiet
host (no other emulation running; every run checked).

- **Renode's tests**, master vs master + every PR: NUnit 1,095 → 1,119
  passed (the 24 new tests), 18 skipped, 0 failed; 47 Robot files 422 → 427
  passed (12's four new cases and a `ramn` flake), 5 skipped, 3 → 2 failed
  (all in `ramn.robot`, failing on master as well). 01's tests fail on
  master; 12's four fail without 11. (Measured with PR 09 in the series; its 20 tests are gone with it.) Each PR builds alone and passes
  `dotnet format` (`clang-format` for 10).
- **Identity**: the vHIL's fingerprint (bus timelines, GPIO edges with
  instruction counts, CPU state and RAM digests, 3.5 s of ecu, ams and
  ecu-ams) is the same file on master and on all.
- **Speed, all**: the vHIL's systems 9-62 % faster (steady state 15-62 %);
  upstream's bus-performance loop 2.76x, a register read 2.24x, a register
  write 1.91x, an STM32 timer's update path 2.64x as fast; allocation per
  bus read 656 → 92 B, per register write 1.36 kB → 280 B.
- **Per PR**: beyond this host's noise, 02 (timer path 1.78x), 03 and 04
  (bus and register reads 1.35-1.45x each), 06 (register write 1.25x), 08
  (timer path 1.26x; 25 % of the series' timer speed) and 11 (a hook that
  excludes itself costs nothing: 0.68 s → 0.022 s on a `nop` loop, the same
  as no hook). 05 and 07 are within the noise alone (05: removing it from
  the series costs 7-12 %).
- **Renode's demos**: no measurable change (they allocate 600-3,800 times
  less than the loops: idle or in translated code most of the time).
- **13** (measured alone against master, 2026-10-15): the slave time source
  tests pass both ways (NUnit 1,106 passed vs master's 1,095, the eleven
  second cases), Robot as master (422 / 5 / 3); with every machine on a
  slave time source (a test-only switch), ten time-framework and
  multi-node Robot files fail the same 28 cases on master's threads and
  with the mode on (slaves are a path upstream's suites don't take, and
  they fail on master already; a precise pause even hangs there). With
  the mode on, Renode's two-node BLE demo on slave time sources runs 2.09x
  as fast (8.01 → 3.83 s, CPU -54 %), two spinning machines 1.31x; the
  vHIL's three-board lock-step system 26-27 % faster (two boards: -7 to
  -11 % CPU at the same speed); mode off, single machines and `mach create`
  machines unchanged; the vHIL's fingerprint (ecu, ams, ecu-ams,
  ecu-ams-ecu, 10 s) byte-identical across master, 13 and 13 with the mode
  off.
- **14** (measured alone against master, 2026-10-15; renode master
  8c5a2440, renode-infrastructure master 3e4301cf): `PeripheralsTests` 117
  passed, 5 skipped (master: 113, the 4 new tests failing); `UnitTests`
  621 / 6 skipped as master; every STM32 platform Robot suite plus
  `timer-reconfiguration` and `virtual-time` pass as on master, with
  `nucleo_wba52cg`'s input capture at the chip's values (IC2 50000 and
  33333 instead of 49999 and 33332, the second duty 49 % instead of 50 %);
  `dotnet format` clean. On the vHIL (the same change as local 0007): the
  HAL tick is 1000 us instead of 999 us and the uDV's IMU frame on a 20 ms
  grid within 1 us (722 us of drift over 91 frames before); frames and
  edges otherwise the same, in the same order.
- **15-18** (measured 2026-10-16 on renode master d0c70ee3,
  renode-infrastructure 48cee122, tlib 0dd9a30c; results.md, "PRs 15-18"):
  Renode's tests as master (`UnitTests` 622 vs 621: 15's test, which also
  passes on master's code; `PeripheralsTests` 113; 14 Robot files, the
  CAN, Ethernet, USB/SMMU and hook suites, 141 passed as on master, and
  18's five on top; one Zynq Ethernet case failed once on all with a guest
  `libc` error and passed 3 of 3 on re-runs, as on master); 18's four new cases fail on
  master, its three IO-only ones with 17 but without 16. An MCAN in
  loopback with 16 filter elements 17x as fast with 15 (23.40 → 1.35 s,
  allocation -95 %); a memory access hook limited to IO accesses as fast as
  no hook (0.39 s vs 0.37 s; every access 2.08 s). The vHIL's fingerprint
  (ecu, ams, ecu-ams, udv, ecu-ams-udv, ecu-ams-ecu, 10 s) byte-identical
  between master and master + 15-18. The vHIL's systems on upstream master
  (where they run slowly: no `CMP.W` fast-forward, none of 01-05): 15
  within the noise; 16 + 17 boots 17-43 % faster where the polling
  detector arms (udv, ams, ecu sim, ecu-ams-udv sim), udv steady 20-29 %.
- **Found on the way**: the local tlib patch's (pc, hook) key collides for
  hook ids of 8 or more (fixed in 10; harmless for the vHIL); 11's first
  revision left the invalidation of already translated blocks to the caller
  (nothing does it by itself: the hook kept being called), caught by the
  hook benchmark and fixed, with a test; and **upstream master already
  breaks the vHIL's counter-loop fast-forward**: since 2026-09-25 opcode
  hooks see a 32-bit Thumb-2 instruction as the whole word, so
  `VhilFastForward`'s `CMP.W` mask never matches (0 hook calls on master and
  all). A bump item for the vHIL, not for these PRs (results.md, end of
  "Speed").

## Dropped

- **09, ClockSource integer arithmetic.** Raul dropped it (2026-10-14). It was exact, checked against the `Fraction` path on 60 million cases, and tested, but it showed no measurable host-time gain alone, within the series, or by leave-one-out. Its preparation is in this branch's history (commit before the drop) if it's ever wanted.

## Order and dependencies

01 first; 02 after it (02's commits sit on 01's: the index must be right
after handlers run, which is 01's fix). 03 to 08 are independent of each
other and of 01/02: each applies to master alone, and 01-08 apply together
in any order (03, 04 and 05 touch different parts of `SystemBus.cs`). 10-12 are a chain across
repositories: 10 (tlib) must be merged and the tlib submodule moved before
11 can land (11 adds `[Import]`s of 10's exports, and a missing import fails
every CPU's creation), and 12's tests need 11 in renode's
`src/Infrastructure`. 13 is independent of all of them: it changes
`TimeSourceBase`, `TimeHandle`, `SlaveTimeSource` and `TimeDomainsManager`,
none of which 01-12 touch (01/02 change `BaseClockSource`), applies to master
alone and on top of 01-08 + 11 (checked with `git am`). 14 touches
`STM32_Timer` like 08, in other lines, and applies to master alone (its
renode/renode commit needs 14 in `src/Infrastructure`). 15 changes only
`Packet.cs` and `PacketTests.cs`, which nothing else in the series
touches. 16-18 are a chain like 10-12, independent of it: 16 (tlib) before
17 can do what it says (17 adds no import, so it builds and runs on an older
tlib, where mode 2 is "every access" and 18's three IO-only cases fail), and
18's tests need 17 and 16 in renode's submodules. 16 and 10 both touch tlib's
`exports.c`, in other functions, and 17 and 11 (and 07) `TranslationCPU.cs`,
in other members: all of 01-08, 11, 13, 14, 15 and 17 apply to
renode-infrastructure master together in either order, 10 and 16 to tlib,
12 and 18 to renode (checked with `git am`, 2026-10-16). Suggested order,
smallest risk first:

1. 01 (bug fix, with failing-on-master tests)
2. 03, 07 and 08 (a few lines each), and 14 (a bug fix with failing-on-master
   tests; its renode test commit once the submodule has it)
3. 06, 04, 05
4. 02 (once 01 is in, or at least reviewed)
5. issue C; once Antmicro agrees on the shape: 10, then 11 (after tlib's
   master has 10 and renode-infrastructure's submodule points there), then 12
6. issue D; once Antmicro answers (opt-in as is, on by default for
   machines with a time source of their own, or not at all): 13
7. 15 with the issue B series (any time: a few lines and a test)
8. issue E; once Antmicro agrees on the shape: 16, then 17 (after tlib's
   master has 16 and the submodule points there), then 18

Opening 03-08 and 15 at once is fine too; each says it is one of a series
from issue B. Upstream merges through its own tree (often rewriting: they
simplified renode-infrastructure#217 and "modified and merged" #207; tlib#45
landed with the issue added to the subject and the submodule bumped by
Antmicro in renode-infrastructure), so expect each PR to be closed rather
than merged, with its commit on master.

## Submitting (Raul)

### 1. The CLA

Sign Antmicro's Contributor License Agreement: renode-infrastructure's at
<https://cla-assistant.io/renode/renode-infrastructure> (the CLA-assistant
bot also posts this link on the first PR) and renode's at
<https://cla-assistant.io/renode/renode> (needed for PR 12 and asked by
upstream's PR template; renode/renode's PRs carry the same `license/cla`
check). Sign with the GitHub account that opens the PRs; the commits' author
email (`rmg@raulmoranguerra.com`) must be one of that account's verified
emails, or CLA-assistant reports the commits as unsigned. To author them
with another address, rewrite them after `git am` (step 3) and before
pushing: `git -c user.email=<address> rebase --exec 'git commit --amend --no-edit --reset-author' origin/master`.

**tlib (PR 10):** antmicro/tlib has no CLA check: none of its recent PRs
(#41-#49, external ones included) carries a `license/cla` status, and it has
no CONTRIBUTING file. Renode's CONTRIBUTING.md says contributing to the
project requires the CLA, so the renode/renode-infrastructure signature is
what covers it; if a CLA-assistant link does appear on the tlib PR, sign
that one too (<https://cla-assistant.io/antmicro/tlib>). No other agreement
is needed.

### 2. The issues

Upstream's CONTRIBUTING.md asks for an issue even when you send the fix
yourself. Open [`ISSUES.md`](ISSUES.md)'s issue A (bug report template),
issue B (feature request template, the performance series), issue C
(feature request template, the hook exclusion), issue D (feature request
template, the slave time source on the master's thread) and issue E
(feature request template, the IO-only memory access hook) at
<https://github.com/renode/renode/issues/new/choose>. Note their numbers:
`A`, `B`, `C`, `D` and `E` below. For C, D and E, wait for a maintainer's
answer before opening 10-12 / 13 / 16-18 if you prefer not to spend review
time on a shape they would change.

### 3. Fork, apply, push, open

renode-infrastructure, PRs 01-08, 13, 14 and 15:

```sh
gh repo fork renode/renode-infrastructure --clone=false     # once, under your account
git clone https://github.com/renode/renode-infrastructure && cd renode-infrastructure
git remote add fork https://github.com/<you>/renode-infrastructure
git fetch origin master
P=<path to IFS_vHIL>/docs/proposals/renode-upstream
A=<issue A number>; B=<issue B number>; C=<issue C number>; D=<issue D number>; E=<issue E number>

# Fill the placeholders in the PR bodies (PR_01 once 01 is open, for 02;
# PR_10, PR_11, PR_12, PR_16, PR_17, PR_18 once those are open)
sed -i '' "s/ISSUE_A/$A/g; s/ISSUE_B/$B/g; s/ISSUE_C/$C/g; s/ISSUE_D/$D/g; s/ISSUE_E/$E/g" $P/*/PR.md

# One PR: a branch named <issue>-<short_name> (CONTRIBUTING.md) off master
pr() {  # pr <dir> <branch>
    git switch -c "$2" origin/master
    git am "$P/$1"/*.patch
    git push fork HEAD
    gh pr create --repo renode/renode-infrastructure --base master --head "<you>:$2" \
        --title "$(head -1 "$P/$1/PR.md")" --body-file <(tail -n +3 "$P/$1/PR.md")
}
pr 01-clock-entry-stale-index        $A-clock_entry_stale_index
pr 03-sysbus-locked-ranges-and-tags  $B-sysbus_locked_ranges_and_tags
pr 07-cpu-hot-path-allocations       $B-cpu_hot_path_allocations
pr 08-stm32-timer-unused-channels    $B-stm32_timer_unused_channels
pr 06-register-framework-allocations $B-register_framework_allocations
pr 04-sysbus-access-context          $B-sysbus_access_context
pr 05-sysbus-context-lookup-cache    $B-sysbus_context_lookup_cache
pr 13-slave-time-source-on-master-thread $D-slave_time_source_on_master_thread   # after issue D's answer
pr 14-stm32-timer-period             stm32_timer_period   # no issue
pr 15-packet-field-attributes        $B-packet_field_attributes
```

14's `renode/` patch goes to renode/renode, like 12, once Antmicro
has moved `src/Infrastructure` to a commit with 14 (it changes one
expectation that only holds with the fix):

```sh
git switch -c stm32_timer_period_tests origin/master      # in the renode clone
git am $P/14-stm32-timer-period/renode/*.patch
git push fork HEAD   # then a PR, or mention it on 14 for Antmicro to fold into the bump
```

02 goes on 01's branch until 01 lands, and is opened against master with
01's PR number filled in (its body says it contains 01's commit):

```sh
sed -i '' "s/PR_01/<01's PR number>/g" $P/02-clock-entry-index/PR.md
git switch -c $B-clock_entry_index $A-clock_entry_stale_index
git am $P/02-clock-entry-index/*.patch
git push fork HEAD
gh pr create --repo renode/renode-infrastructure --base master --head "<you>:$B-clock_entry_index" \
    --title "$(head -1 $P/02-clock-entry-index/PR.md)" --body-file <(tail -n +3 $P/02-clock-entry-index/PR.md)
# once 01's commit is on upstream master:
git fetch origin && git rebase origin/master && git push --force-with-lease fork HEAD
```

The hook exclusion, 10 then 11 then 12 (after issue C):

```sh
# 10: antmicro/tlib
gh repo fork antmicro/tlib --clone=false
git clone https://github.com/antmicro/tlib && cd tlib
git remote add fork https://github.com/<you>/tlib
git switch -c $C-pre_opcode_hook_exclusions origin/master
git am $P/10-tlib-pre-opcode-hook-exclusions/*.patch
git push fork HEAD
sed -i '' "s/PR_11/the renode-infrastructure PR (to follow)/g; s/PR_12/the renode PR (to follow)/g" $P/10-*/PR.md
gh pr create --repo antmicro/tlib --base master --head "<you>:$C-pre_opcode_hook_exclusions" \
    --title "$(head -1 $P/10-*/PR.md)" --body-file <(tail -n +3 $P/10-*/PR.md)

# 11: renode-infrastructure, the C# change on top of a commit moving the
# submodule to 10's commit (your fork's tlib branch until 10 is merged;
# once it is, to tlib master and that commit is the ordinary bump)
cd renode-infrastructure
git switch -c $C-pre_opcode_hook_exclusions origin/master
git submodule update --init src/Emulator/Cores/tlib
git -C src/Emulator/Cores/tlib fetch https://github.com/<you>/tlib $C-pre_opcode_hook_exclusions
git -C src/Emulator/Cores/tlib checkout FETCH_HEAD
git commit -am "tlib: Bump"
git am $P/11-cpu-pre-opcode-hook-exclusions/*.patch
git push fork HEAD
sed -i '' "s/PR_10/<10's number>/g; s/PR_12/<12's number, or 'a renode PR to follow'>/g" $P/11-*/PR.md
gh pr create --repo renode/renode-infrastructure --base master --head "<you>:$C-pre_opcode_hook_exclusions" \
    --title "$(head -1 $P/11-*/PR.md)" --body-file <(tail -n +3 $P/11-*/PR.md)

# 12: renode/renode, the tests (renode's CI runs them once src/Infrastructure has 11)
gh repo fork renode/renode --clone=false
git clone https://github.com/renode/renode && cd renode
git remote add fork https://github.com/<you>/renode
git switch -c $C-pre_opcode_hook_exclusion_tests origin/master
git am $P/12-tests-pre-opcode-hook-exclusions/*.patch
git push fork HEAD
sed -i '' "s/PR_10/<10's number>/g; s/PR_11/<11's number>/g" $P/12-*/PR.md
gh pr create --repo renode/renode --base master --head "<you>:$C-pre_opcode_hook_exclusion_tests" \
    --title "$(head -1 $P/12-*/PR.md)" --body-file <(tail -n +3 $P/12-*/PR.md)
```

Then fill 10's body with 11's and 12's numbers (`gh pr edit`).

The IO-only memory access hook, 16 then 17 then 18 (after issue E), the
same way as 10-12:

```sh
# 16: antmicro/tlib (in the tlib clone)
git switch -c $E-memory_access_io_only origin/master
git am $P/16-tlib-memory-access-io-only/*.patch
git push fork HEAD
sed -i '' "s/PR_17/the renode-infrastructure PR (to follow)/g; s/PR_18/the renode PR (to follow)/g" $P/16-*/PR.md
gh pr create --repo antmicro/tlib --base master --head "<you>:$E-memory_access_io_only" \
    --title "$(head -1 $P/16-*/PR.md)" --body-file <(tail -n +3 $P/16-*/PR.md)

# 17: renode-infrastructure, on a commit moving the submodule to 16's commit
cd renode-infrastructure
git switch -c $E-memory_access_io_only origin/master
git submodule update --init src/Emulator/Cores/tlib
git -C src/Emulator/Cores/tlib fetch https://github.com/<you>/tlib $E-memory_access_io_only
git -C src/Emulator/Cores/tlib checkout FETCH_HEAD
git commit -am "tlib: Bump"
git am $P/17-cpu-memory-access-io-only/*.patch
git push fork HEAD
sed -i '' "s/PR_16/<16's number>/g; s/PR_18/<18's number, or 'a renode PR to follow'>/g" $P/17-*/PR.md
gh pr create --repo renode/renode-infrastructure --base master --head "<you>:$E-memory_access_io_only" \
    --title "$(head -1 $P/17-*/PR.md)" --body-file <(tail -n +3 $P/17-*/PR.md)

# 18: renode/renode (in the renode clone)
git switch -c $E-memory_access_hook_io_only_tests origin/master
git am $P/18-tests-memory-access-hook-io-only/*.patch
git push fork HEAD
sed -i '' "s/PR_16/<16's number>/g; s/PR_17/<17's number>/g" $P/18-*/PR.md
gh pr create --repo renode/renode --base master --head "<you>:$E-memory_access_hook_io_only_tests" \
    --title "$(head -1 $P/18-*/PR.md)" --body-file <(tail -n +3 $P/18-*/PR.md)
```

Then fill 16's body with 17's and 18's numbers.

The `sed -i ''` edits the copies in this checkout; don't commit them back
(or do, on a branch, once the numbers are known).

Optional, upstream style: prefix the commit subjects with the issue
(`[#123] ClockSource: ...`), after `git am` on each branch:

```sh
git rebase origin/master --exec \
  'git commit --amend --no-edit -m "$(git log -1 --format=%B | sed "1s/^\(\[FIX\] \)\{0,1\}/[#'$B'] /")"'
```

(01 with `$A`, 10-12 with `$C`, 13 with `$D`, 16-18 with `$E`; on 02's branch rebase onto
`$A-clock_entry_stale_index` instead, so only 02's two commits change.)
Recent external PRs have landed both with and without it
(renode-infrastructure#217 `[#941] NVIC: ...`, #224 `[#953] ...`; #220 and
#225 without; Antmicro added `[GH#995]` to tlib#45 themselves), so it is a
nicety.

Upstream's PR template also asks for a reproduction repository from
<https://github.com/renode/renode-issue-reproduction-template>. 01 has NUnit
tests that fail on master instead, 10-12 and 16-18
Robot tests; 02-08 and 15 change no behaviour, and the benchmark scripts quoted in
issue B are their reproduction. Each `PR.md` says so; build the repository
only if a reviewer asks.

### 4. When upstream asks for changes

- Make the change on the PR's branch in the clone, keeping one logical
  change per commit (`git commit --fixup <sha>`, then
  `git rebase -i --autosquash origin/master`), and
  `git push --force-with-lease fork HEAD`.
- Re-run the checks before pushing (Docker only, [`tooling/`](tooling/)):
  `tooling/build.sh <name> <branch> $(grep -v '^#' tooling/tests.txt)`
  builds the branch and runs the suites (`RENODE_REF` for a renode branch,
  e.g. 12's tests; a tlib change is a tlib branch plus a submodule-moving
  commit on the infrastructure branch); `dotnet format whitespace --folder
  --verify-no-changes --include <files>` from renode's root (its
  `.editorconfig`) in the builder image, and `clang-format` 19.1.0 for tlib,
  check the formatting. For an optimisation, re-run the fingerprint
  (`tooling/vhil-ab.sh fingerprint <name>`) and compare with master's.
- A PR is done when its commit (possibly rewritten) is on upstream master,
  even if GitHub shows it "Closed". Check the landed commit against the
  local patch it replaces before dropping that patch (below).
- If they reject one, nothing breaks: that part stays in the local queue.
- Update this directory's patches and `PR.md` if the PRs change, so the
  record matches what was submitted.

### 5. Dropping the local patches

[`docker/renode/README.md`](../../../docker/renode/README.md) ("Upstreaming")
says which upstream PRs replace which local patch. A local patch goes only
when the Renode release the vHIL moves to contains every PR that replaces
it; until then the queue keeps carrying it, unchanged, on 1.17.0.

## Re-running the measurements

Docker only (Colima here: bind mounts must be under the home directory; the
builds run in Docker volumes, as macOS's case-insensitive file system breaks
Renode's packaging). The scripts take one Renode emulation at a time:
each run first waits until no other container is running
([`tooling/idle.sh`](tooling/idle.sh); `IDLE_IGNORE` is a regex of
container names to ignore, default the vHIL's own long-lived stack), and
marks a run that ended beside another container as contended
(`summarize_bench.py` drops those).

```sh
T=docs/proposals/renode-upstream/tooling
export RENODE_UPSTREAM=$PWD/.claude/worktrees/renode-upstream   # renode master clone
export WORK=$PWD/.claude/worktrees/renode-bench                 # fw/{ECU08,AMS,CAN_BL}.elf, results/
git clone https://github.com/renode/renode $RENODE_UPSTREAM
git -C $RENODE_UPSTREAM submodule update --init --recursive
# Branches in the clone (each directory's patches `git am`'d on master):
#   src/Infrastructure: up/master = origin/master; up/01-..., up/02-... (on 01),
#     up/03-... to up/08-...; up/11-... = a commit moving the tlib submodule to
#     tlib's up/10-..., then 11's patch; up/all = 01-08 + 11
#   src/Infrastructure/src/Emulator/Cores/tlib: up/master, up/10-...
#   renode itself: master, up/12-... (12's patch), up/all = up/12-...
#   PR 13 (src/Infrastructure): up/13-slave-on-master-thread (13's patches),
#     up/13-first-two (its first two commits), up/13-forced (+ tooling/test-slaves.patch),
#     up/master-forced (master + tooling/test-slaves-master.patch)
docker build -t renode-up-builder -f $T/builder.Dockerfile $T
docker build -t renode-up-runtime -f $T/runtime.Dockerfile $T          # ifs-vhil + perf
$T/build.sh master up/master $(grep -v '^#' $T/tests.txt)              # build + Renode's suites
RENODE_REF=up/all $T/build.sh all up/all $(grep -v '^#' $T/tests.txt)
$T/build.sh pr03 up/03-sysbus-lookups                                   # one PR alone (no tests)
python3 $T/summarize_tests.py <renode-up-pkgs:/master.tests> <.../all.tests>   # in a container
$T/bench.sh $WORK/results/loops.jsonl 5 "master pr02 pr03 pr04 pr05 pr06 pr07 pr08 pr11 all" \
    "bus-trivial register-read register-write stm32-timer"
python3 $T/summarize_perpr.py $WORK/results/loops.jsonl master
$T/bench.sh $WORK/results/hooks.jsonl 5 "master all" "hook-none hook-always hook-exclude"
$T/bench.sh $WORK/results/demos.jsonl 5 "master all" "demo-stm32f4_discovery demo-nucleo_h753zi_hubris demo-nrf52840"
$T/vhil-ab.sh speed ab 5 "master all"                                  # the vHIL's speed.py
$T/vhil-ab.sh speed perpr 3 "master pr02 pr03 pr04 pr05 pr06 pr07 pr08 pr11 all" --paths sim
python3 $T/summarize_vhil.py $WORK/results/ab master
$T/vhil-ab.sh fingerprint master; $T/vhil-ab.sh fingerprint all        # then compare the two files
$T/profile.sh master stock-ecu-ams ecu-ams sim                         # perf; ../renode-patches/tooling/stacks.py
# PR 13
$T/build.sh pr13 up/13-slave-on-master-thread $(grep -v '^#' $T/tests.txt)
$T/build.sh master13 up/master src/Infrastructure/src/Emulator/Main/Tests/UnitTests/UnitTests.csproj \
    $(grep -v '^#' $T/tests-time.txt)
$T/build.sh pr13-c2 up/13-first-two src/Infrastructure/src/Emulator/Main/Tests/UnitTests/UnitTests.csproj
for m in "masterx-threads up/master-forced threads" "pr13x-threads up/13-forced threads" "pr13x-inline up/13-forced inline"; do
    set -- $m; RENODE_TEST_SLAVES=$3 $T/build.sh $1 $2 --test-timeout 120 $(grep -v '^#' $T/tests-slaves.txt)
done
# ./renode-test also runs from a package (renode-up-pkgs:/<name>, no rebuild): pr13 on
#   $(grep -v '^#' tests-time.txt) against master13; -n 3 tests/unit-tests/cpu-pause-halt.robot
#   in masterx-threads / pr13x-threads / pr13x-inline with RENODE_TEST_SLAVES (the flaky cases)
# multi-*-inline fail on master (no RunsOnMasterThread): bench.sh prints FAILED for them
$T/bench.sh $WORK/results/multi.jsonl 7 "master pr13" \
    "multi-spin-shared multi-spin-slaves multi-spin-inline multi-ble-shared multi-ble-slaves multi-ble-inline bus-trivial"
python3 $T/summarize_bench.py $WORK/results/multi.jsonl master
# The vHIL's models no longer compile on upstream master (#104075 moved Monitor.SanitizePathSeparator):
# for these runs, edit models/renode/VhilMonitor.cs to Antmicro.Renode.Utilities.PathHelpers.SanitizePathSeparator
# (locally; revert after, the vHIL's 1.17.0 build has no PathHelpers)
FP_SYSTEMS="ecu ams ecu-ams ecu-ams-ecu" $T/vhil-ab.sh fingerprint master 10000 r13-master   # and pr13;
FP_SYSTEMS="ecu ams ecu-ams ecu-ams-ecu" VHIL_INLINE_SLAVES=0 $T/vhil-ab.sh fingerprint pr13 10000 r13-pr13-threads
$T/vhil-ab.sh speed r13-ab 5 "master pr13" --systems ecu ams ecu-ams ecu-ams-ecu --paths sim bench
python3 $T/summarize_vhil.py $WORK/results/r13-ab master
# PRs 15-18 (2026-10-16, a fresh clone: renode master d0c70ee3). Branches in the clone:
#   src/Infrastructure: up/master, up/15-packet-field-attributes, up/17-cpu-memory-access-io-only
#     (= "tlib: Bump" to tlib's up/16-memory-access-io-only, then 17's patch), up/all15 (17's + 15),
#     up/17-without-16 (17's patch on master's tlib), up/15-test-only (15's test alone)
#   tlib: up/master, up/16-memory-access-io-only; renode: up/18-tests-memory-access-hook-io-only = up/all15
# WORK=.claude/worktrees/renode-bench/r15: fw/ holds ECU08, AMS, uDV and CAN_BL from vhil-data (dev,
# 2026-10-15); VHIL_REPO = a worktree at IFS_vHIL dev (udv systems, the 6-system fingerprint), with
# VhilMonitor.cs's PathHelpers edit (above)
$T/build.sh master15 up/master $(grep -v '^#' $T/tests-15.txt | grep -v memory-access-hook)
$T/build.sh pr15 up/15-packet-field-attributes $(grep -v '^#' $T/tests-15.txt | grep -v memory-access-hook)
RENODE_REF=up/18-tests-memory-access-hook-io-only $T/build.sh pr17 up/17-cpu-memory-access-io-only \
    $(grep -v '^#' $T/tests-15.txt | grep -v csproj)
RENODE_REF=up/all15 $T/build.sh all15 up/all15 $(grep -v '^#' $T/tests-15.txt)
$T/build.sh tests15 up/15-test-only src/Infrastructure/src/Emulator/Main/Tests/UnitTests/UnitTests.csproj
RENODE_REF=up/18-tests-memory-access-hook-io-only $T/build.sh pr17-no16 up/17-without-16 \
    tests/unit-tests/memory-access-hook.robot
$T/bench.sh $WORK/results/generic.jsonl 5 "master15 pr15 pr17 all15" "mcan-loopback memhook-none memhook-all memhook-io"
python3 $T/summarize_bench.py $WORK/results/generic.jsonl master15
$T/vhil-ab.sh fingerprint master15 10000; $T/vhil-ab.sh fingerprint all15 10000   # 6 systems; compare
$T/vhil-ab.sh speed r15-ab 5 "master15 pr15 pr17 all15" --systems ecu ams udv ecu-ams-udv --paths sim bench
python3 $T/summarize_vhil.py $WORK/results/r15-ab master15
```

The whole round (builds, both test runs, every benchmark) took about five
hours on this host, most of it the vHIL's `speed.py` runs.

