# Renode fork study: what patching Renode 1.17.0 buys the vHIL, and at what cost

Part of #260. A feasibility study: prototype patches to Renode's own C#
(renode-infrastructure) and tlib, measured with the vHIL's
`scripts/speed.py`, checked for exact equivalence, and weighed against the
cost of carrying them. Nothing here changes the vHIL; the patches are under
[`renode-patches/`](renode-patches/) for review.

## Recommendation

**Upstream the three patches that pay (P2, P3, P5) as pull requests to
renode/renode-infrastructure, and carry them meanwhile as a small patch queue
on the 1.17.0 tag, built from source in the image. Do not maintain a fork.**

- **There is nothing newer to upgrade to.** 1.17.0 (2026-09-07) is the latest
  release. master is 239 commits ahead, none of them performance work in the
  time framework, the bus or tlib's aarch64 backend (renode
  `v1.17.0...master`, tlib `167decf9...master`, 2026-10-13). An upgrade later
  is still worth it for the BASEPRI fix (renode/renode#1021, fixed upstream in
  8f5c6f5 five days after it was filed), not for speed.
- **The three patches are small, generic and change nothing a run computes**
  (byte-identical bus timelines, GPIO edges, CPU state and RAM against stock
  Renode, below), so they are good upstream candidates: none touches an
  interface or a model's behaviour, and each removes an allocation, a closure
  or a linear search from a hot path every Renode user has.
- **Together they make every system 13-58 % faster in steady state** on the
  same host (4 cores, median of 5, interleaved A/B, table below); the two-board
  bench path, the slowest the wall-clock bench runs, goes from 0.88x to 1.28x.
- **A patch queue, not a fork**: three commits (about 360 lines) applied to
  the release tag in the image build. Rebasing on a new release is re-applying
  three commits to files that changed little in a year; once upstream takes
  them the queue is empty and the image goes back to the release package.
  Building Renode from source adds about 5 minutes to an image build on 8
  arm64 cores (no cache) and a builder stage with the .NET 8 SDK; the runtime
  image does not grow (178 MB package against the release's 189 MB).
- **Not worth doing**: P1 (blocking instead of spinning at the barrier: slower
  on 4 cores), a semantics-preserving run-ahead past sync points (impossible
  without lookahead, below), and the aarch64 inline TLB (at most a few percent,
  Apple silicon only; CI is x86).

## Measured gains

`scripts/speed.py` (boot = first 3 s of virtual time from power-on, steady =
the 5 s after) on an Apple M-series Mac through Colima, the container limited
to 4 cores (`--cpus 4 --cpuset-cpus 4-7`, as `VHIL_DOCKER_CPUS=4`), Renode
1.17.0 built from source at the tag ("stock") against the same tree with the
patches, each package interleaved run by run with the other, median of 5,
2026-10-13. The firmware is pinned (ECU dev 44610a5, AMS dev 91d49c9, CAN
bootloader v1.7.0); fast-forward on, as the bench runs.

| System | Path | Phase | stock | patched (P2+P3+P5) | release package |
|---|---|---|---|---|---|
| ams | bench | boot | 1.33x | **1.54x (+15 %)** | 1.31x |
| ams | bench | steady | 2.09x | **2.69x (+28 %)** | 1.99x |
| ams | sim | boot | 0.91x | **1.31x (+44 %)** | 1.07x |
| ams | sim | steady | 1.94x | **3.06x (+58 %)** | 2.23x |
| ecu | bench | boot | 1.34x | **1.53x (+14 %)** | 1.15x |
| ecu | bench | steady | 1.69x | **2.29x (+36 %)** | 1.60x |
| ecu | sim | boot | 1.34x | **1.54x (+15 %)** | 1.35x |
| ecu | sim | steady | 2.73x | **3.85x (+41 %)** | 2.70x |
| ecu-ams | bench | boot | 0.81x | **1.11x (+38 %)** | 0.96x |
| ecu-ams | bench | steady | 0.88x | **1.28x (+47 %)** | 1.03x |
| ecu-ams | sim | boot | 0.78x | 0.75x (-4 %) | 0.83x |
| ecu-ams | sim | steady | 1.08x | **1.23x (+13 %)** | 1.20x |

The release package (the image's `/opt/renode`) and the source build of the
same tag differ only by noise: their spread (-14 % to +20 %) is this host's
run-to-run variation (docs/performance.md: ±20 % under Colima), and the
patched build's gains are beyond it everywhere but the two-board boot under
lock-step, which the barrier dominates (below). CI (x86, GitHub's 4-vCPU
runners) was not measured: the study could not push. The `emulation speed`
job on a branch whose image builds Renode with the patches would give it.

Each patch alone (same setup, median of 3; one run of each in turn):

| System | Path | Phase | stock | P1 no-spin | P2 clock | P3 bus | P5 alloc |
|---|---|---|---|---|---|---|---|
| ams | bench | steady | 1.88x | -8 % | +7 % | +6 % | +4 % |
| ams | sim | steady | 1.66x | +8 % | +22 % | +51 % | +25 % |
| ecu | bench | steady | 1.42x | +10 % | +42 % | +18 % | +18 % |
| ecu | sim | steady | 2.47x | -16 % | +22 % | +5 % | +13 % |
| ecu-ams | bench | steady | 0.89x | -8 % | +12 % | +4 % | +5 % |
| ecu-ams | sim | steady | 1.00x | -11 % | +6 % | +5 % | +14 % |

(Boot columns and every run in [`renode-patches/results.md`](renode-patches/results.md).) The gains add up
roughly multiplicatively: P2 and P3 each take a different cost off every timer
event and every bus access, and P5 most of the garbage both made.

## Candidates

What the host's time went to, with host `perf` on Renode (ecu-ams, sim path,
steady state, JIT symbols from .NET's perf map, coreclr's from Microsoft's
symbol server; `renode-patches/tooling/profile-variant.sh`), as a share of
all samples of the Renode process, stock → patched:

| | stock | patched |
|---|---|---|
| tlib executing (translated code, helpers, and every call back into C#) | 33.5 % | 34.5 % |
| bus accesses (`SystemBus` read/write, models included) | 17.9 % | 13.6 % |
| time framework (`BaseClockSource.Update`, handlers included) | 16.8 % | 13.4 % |
| clock-entry look-ups (`Misc.IndexOf`, `Delegate.Equals`) | 5.5 % | 0.1 % |
| allocation (`JIT_New*`) | 6.3 % | 3.4 % |
| garbage collection | 4.8 % | 1.9 % |
| waiting at the barrier (`SemaphoreSlim.Wait`, spin and block) | 34.0 % | 36.4 % |

### 1. The lock-step barrier: P1 (dropped)

Each board's quantum is relayed by three threads: the master grants it to
the board's `SlaveTimeSource` dispatcher thread (one per board since #209),
which grants it to the CPU thread, and back. Every wait is
`MonitorCondition.Wait` on a `SemaphoreSlim`, which spins (`SpinWait`, with
`sched_yield`) before it blocks: the dispatcher threads and the master spend
most of their samples there.

**P1** (`01-p1-nospin.patch`, 20 lines) replaces the waiter with a plain
`Monitor.Wait`, no spin. With five threads that spin on four cores it was
expected to give the CPU threads their cores back; measured, it is slower in
most configurations (-8 to -16 %), a little faster in a few: the spin buys a
cheap hand-off, and blocking pays a futex wake and, in a VM, a vCPU wake, at
every one of the 2000 sync points per virtual second. Not recommended. A
cheaper hand-off would need fewer hops, not a different wait: running a
board's dispatcher inline on the master's thread (one hop instead of two each
way) is a redesign of `SlaveTimeSource`, not a patch.

**Run-ahead is not possible without changing what a run computes.** A board
can run past a sync point only if nothing the other board does in the current
quantum can reach it before then. The vHIL delivers whatever crosses between
boards (frames, through the bus model) at the next sync point, so board A's
quantum k+1 depends on board B's quantum k, which is only known once B has
run it: the lookahead is exactly one quantum, which is what the barrier
already uses. Running ahead further needs either a lookahead longer than the
quantum (a longer quantum: what the firmware sees of another board would lag
more, a semantic change, docs/can-bus.md) or optimistic execution with
rollback (snapshots per quantum: far costlier than the barrier).

How much the barrier costs, as a measurement only: the same build with a 2 ms
quantum instead of 500 µs runs ecu-ams 1.32x → 2.00x (sim, steady) and 1.34x
→ 1.91x (bench), median of 3. About a third of a two-board run's wall time
goes with the sync points: hand-offs, and the boards' busy stretches falling
in different quanta (the "sum, not max" of docs/performance.md). Only the
quantum, a decision about the model (#260 item 1), moves that.

### 2. The time framework per event: P2 (recommended, upstream)

`BaseClockSource` finds a clock entry by its handler with a linear search
comparing delegates (`FindIndex(x => x.Handler == handler)`, a closure each
time) on every timer reload, every `GetClockEntry` (each STM32 timer limit
event reads its entry back) and every `Machine.ScheduleAction`, which adds a
one-shot entry and removes it when it fires (the CAN bus model's wakes, ADC3's
conversions). It also keeps two flags in `ThreadLocal<bool>`, read several
times per update although they are only ever touched with the source's lock
held, and allocates a list per update.

**P2** (`02-p2-clock.patch`, +102/-41 in `BaseClockSource.cs`) keeps a
dictionary from handler to index beside the entry list (same delegate
equality, rebuilt from the removal point on a removal, as the list shifts),
makes the two flags plain fields (they are only read and written under
`sync`, which a thread in `Update` holds throughout, so they cannot be seen by
another thread), and reuses one list for the handlers already run.
`TryRemoveClockEntry` looks the entry up again after its `UpdateLimits`,
which can run handlers: stock Renode removes the entry at the index it found
before, which is a different entry if a handler removed one below it in the
meantime. That is the only behavioural difference, in a case stock Renode gets
wrong; the runs below never hit it.

Gain: +6 to +42 % steady state alone; `IndexOf`/`Delegate.Equals` go from
5.5 % of the process's samples to 0.1 %.

### 3. Managed transitions per bus access: P3 (recommended, upstream)

The reverse P/Invoke from tlib into C# itself is cheap (`JIT_ReversePInvoke*`
under 0.5 %); what follows it is not. For every CPU read or write of a
peripheral, `SystemBus` (stock):

- allocates a `Tuple`, a closure and a `DisposableWrapper` to set and restore
  the access's initiator in a `ThreadLocal` (`SetLocalContext`);
- looks up locked address ranges twice (two locks, three dictionary
  look-ups), although nothing in the vHIL ever locks one;
- looks up the initiator's peripheral collection again (a lock and two
  dictionary look-ups);
- searches the tags with a closure (`Misc.BinarySearch(r => ...)`).

**P3** (`03-p3-mmio.patch`, +138/-12 in `SystemBus.cs`) keeps a per-thread
stack of initiators with one reusable disposer (a thread-static one-entry
cache in front of the `ThreadLocal`), skips the locked-range look-ups until a
range is first locked, caches the last successful context look-up until the
collections change (`DropCaches` bumps a version), and binary-searches the
tags without a closure. Same peripheral, same offset, same order of
side effects.

A cache of the peripheral per address range (the brief's suggestion) was not
needed: `PeripheralCollection.FindAccessMethods` is 0.2 % of the samples; the
cost was around it, not in it. A function-pointer entry from tlib straight to
a peripheral would bypass hooks, watchpoints and the per-access context, all
of which the vHIL uses (`VhilBusHooks`, `VhilFastForward`).

Gain: +4 to +51 % steady state alone (most on the AMS, whose LTC6811 chain
and SD card are register traffic).

### 4. aarch64 hosts: inline TLB (not prototyped; not worth it)

tlib's aarch64 backend (`tcg/aarch64/tcg-target.c`, MIT, Antmicro, 2024)
calls the softmmu helper for every guest load and store; the i386 backend
inlines the TLB compare and only calls it on a miss. Porting the i386 fast
path is tractable (about 150 lines of aarch64 encodings: index from the
address, compare with the entry's `addr_read`/`addr_write`, add `addend`,
load or store, keep the store's barrier) and has no licence problem if
written from tlib's own i386 backend (MIT, Fabrice Bellard). QEMU's aarch64
backend, which has the fast path, is GPL-2.0-or-later and should not be
copied into tlib (MIT/LGPL) for a component meant to go upstream.

It is not worth it here: on the CPU threads, all of tlib (translated code
3.9 %, helpers and the execution loop about 9 %) is about 13 % of the time,
of which the load/store helpers are a part; an inline fast path saves at most
a few percent, on Apple silicon only. CI and the server are x86, where the
fast path exists. Worth an upstream issue, not our effort.

### 5. Allocation and logging overhead: P5 (recommended, upstream)

Garbage collection was 4.8 % of the samples and allocation 6.3 %. Besides P3's
bus context, the per-access allocations were:

- the register framework (`PeripheralRegister.ReadInner`/`WriteInner`): a
  `List` per read and per write, `Distinct()` (a `HashSet`) on every write,
  a LINQ `Where` over the tags on every write (`InvalidTagValues`), and
  `LogUnhandledWrites` formatting its message with LINQ and string formatting
  before the logger drops it at the default level;
- the CPU's pause guard: a `new object()` on every bus access
  (`CpuThreadPauseGuard.Initialize`; only its being non-null matters);
- `HookDescriptorBase.ActivateNewHooks`: a LINQ iterator before every
  execution chunk;
- `$"..."` noisy logs built on every call before the level check: every
  opcode hook (the fast-forward's CMP.W hook) and every scheduled action.

**P5** (`04-p5-alloc.patch`, +113/-42 in five files) removes each: lists only
when a field changes, a reused scratch list for writes (a fresh one if a write
handler re-enters), `Distinct` only when there are two or more fields, a
plain loop for the tags, messages built only when `Logger.MinimumLogLevel`
lets them through (same output: the logger dropped them anyway), a static
marker for the guard, a plain loop for the hooks.

Gain: +4 to +25 % steady state alone. Allocation 6.3 → 3.4 %, GC 4.8 → 1.9 %
with all three patches.

Other things looked at, none worth a patch: the translation block size (tlib's
maximum already), the chunk length (set by the nearest clock entry: the
events themselves), and the "Logging thread" (3 % of samples, all of it its
`BlockingCollection` wait spinning; it logs little).

## Correctness

- **Same run, byte for byte.** `renode-patches/tooling/fingerprint.py` runs
  ecu, ams and ecu-ams from power-on for 3.5 s on the lock-step path and
  records what `tests/sim/test_fast_forward.py` compares: every bus's
  timeline in ns, GPIO edges with the executed-instruction count at each
  write, the radio's payloads, and each CPU's executed instructions, PC,
  architectural state and RAM (`vhil_ff_<board> StateDigest`). Its SHA-256
  is identical for the release package, the stock source build, the patched
  build (P2+P3+P5) and the build with all four patches (P1 included):
  ecu `843924f0…`, ams `5a662ca9…`, ecu-ams `479f9487…` (790, 163 and 953
  frames; 28 411, 1 125 and 27 862 edges).
- **The vHIL's own tests, against the patched build**
  (`RENODE=<patched>/renode python -m pytest`): `test_fast_forward.py` (ecu,
  ams, ecu-ams), `test_can_bus.py::test_two_board_runs_give_the_same_bus_timeline`,
  `test_ecu_boot.py`, `test_ams_boot.py`, `test_ams_can.py`,
  `test_ecu_ams_gate.py`, `test_cortex_m7_core.py`: 47 passed, 2 xfailed (the
  AMS files run a second time: on the first, a concurrent container on the
  same 8 GB VM got Renode OOM-killed). One failure and two errors need the
  firmware's source tree
  (`test_firmware_identity` reads `VERSION`, `test_ams_can`'s contract tests
  read `Core/Inc/can/messages`), which the study's pinned ELFs did not have;
  they fail identically against stock Renode. The full native suite and
  IFS_HIL's suites were not run (the brief: a quick subset).

## Patch size and upstreamability

| Patch | Files | Lines | Upstream? |
|---|---|---|---|
| P1 MonitorCondition no-spin | 1 | +20/-8 | No (slower here) |
| P2 BaseClockSource index | 1 | +102/-41 | Yes: generic, no API change; the `TryRemoveClockEntry` re-lookup is a bug fix worth its own commit |
| P3 SystemBus access path | 1 | +138/-12 | Yes: no API change (the public `SetLocalContext` keeps its signature) |
| P5 allocations and logs | 5 | +113/-42 | Yes: split per file for review |
| aarch64 inline TLB | – | ~150 (est.) | An issue, not our patch |

All in renode-infrastructure; none in tlib. Upstream takes GitHub pull
requests (after a CLA) and lands them through its own tree, so a PR shows
"closed" when its commit lands (e.g. renode-infrastructure #217, 2026-10-07);
#1021 shows the team answers within days. Each PR should carry the profile
evidence and a Robot or unit test where one applies (P2: clock entries added,
removed and exchanged from handlers).

## Cost of carrying them

- **Rebasing**: re-apply three commits per Renode release. Upstream touched
  `BaseClockSource`, `SystemBus` and `PeripheralRegister` since 1.17.0 (clock
  disabling, tag options, a `FieldMode` rename: 228 commits on
  renode-infrastructure master, 2026-10-13), and all three patches still
  cherry-pick onto master without conflicts.
- **Image build**: a builder stage (`mcr.microsoft.com/dotnet/sdk:8.0`,
  cmake, gcc) running `./build.sh -t --no-gui`: 4.5 min from scratch on 8
  arm64 cores here (3 min of it tlib for every architecture, which a
  `--external-lib-arch arm` style build of just the Cortex-M core would cut),
  about 1 min incremental. Must build on a case-sensitive file system (the
  portable packaging renames `Renode` to `renode`). CI would build it per
  image change, cached by the patch queue's hash, for both amd64 and arm64.
- **Image size**: unchanged (portable headless package 178 MB, release 189
  MB).
- **Supply chain**: the Dockerfile's SHA-256 pin on the release tarball
  becomes a pin on the tag's commit (and its submodules', fixed by the tag)
  plus the patch files in this repository.
- **Fast-forward calibration**: `VhilFastForward` finds tlib's counters by
  value at run time; it calibrated and skipped as usual on every build here
  (tlib unchanged).

A fork repository (renode-infrastructure with our branch) would add a second
repository to keep in step, submodule bumps and its own CI, for the same
three commits. Not needed while the change set is this small.

## What is left after the patches

The remaining Renode-internal cost is the barrier (36 % of the samples, by
design) and tlib executing (34 %, including every peripheral model's work).
Two of the remaining hot spots are the vHIL's own models, for the other
work in #260: `STM32H7_ADC3.CatchUp` from a scheduled action (3.6 % of all
samples) and `VhilCanBus.Decide` (2.6 %).

## Reproducing

Layout under `.claude/worktrees/renode-fork-study/`: `renode/` (clone of
renode at v1.17.0 with submodules), `vhil/` (this repository), `fw/` (the
pinned ELFs), `scripts/` (a copy of `renode-patches/tooling/`; the scripts find the
others relative to it):

```sh
docker build -t renode-fork-builder -f tooling/builder.Dockerfile tooling
docker build -t renode-fork-runtime -f tooling/runtime.Dockerfile tooling   # ifs-vhil + perf
git -C renode/src/Infrastructure am $PWD/vhil/docs/proposals/renode-patches/0[234]-*.patch
tooling/build-variant.sh patched                # portable package into a Docker volume
tooling/ab.sh m 5 "stock patched"               # interleaved speed.py runs
python3 tooling/summarize.py m                  # medians
tooling/fingerprint-variant.sh patched          # byte-for-byte observation
tooling/fork.Dockerfile                         # ifs-vhil with the patched package
```
