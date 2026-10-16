# Renode 1.17.0 with the vHIL's patch queue

The vHIL runs Renode **1.17.0** built from source with nine patches to
renode-infrastructure and two to tlib. Eight of them and tlib's make every
system faster (the first three 13-58 %) and change nothing a run computes;
0007 is a fidelity fix, which does (#292, below). They are a patch queue on the
release tag, not a fork: `build.sh` fetches the tag, checks every input
against a pin, applies `patches/` in order and builds the portable headless
package, the release tarball's layout. `docker/Dockerfile`'s `renode-build`
stage runs it; the vHIL image copies the package to `/opt/renode`, and CI
exports the same stage (`.github/actions/setup-renode`). Why and how they
were measured:
[`docs/proposals/renode-fork-study.md`](../../docs/proposals/renode-fork-study.md)
(#260).

| File | What |
|---|---|
| `pins.env` | Release, its commit, renode-resources' commit, the patch set's id, and the patched renode-infrastructure and tlib trees |
| `submodules.txt` | Every submodule's commit (recursive) as the tag records it |
| `patches/` | The queue, applied in order to `src/Infrastructure` (renode-infrastructure); `patches/tlib/` to its tlib submodule |
| `build.sh` | Fetch, check, patch, build, package; writes `VHIL_RENODE_BUILD` |

## The patches

| # | Study name | What it does | Upstream |
|---|---|---|---|
| 0001 | P2 | `BaseClockSource`: a dictionary from handler to clock-entry index beside the list (no linear delegate search per timer reload, `GetClockEntry` or `ScheduleAction`), plain fields for two `ThreadLocal<bool>` flags only touched under the source's lock, one reused list per update; `TryRemoveClockEntry` looks the entry up again after `UpdateLimits` (stock Renode can remove the wrong entry if a handler removed one below it) | Prepared as PRs 01 + 02, not submitted (CLA) |
| 0002 | P3 | `SystemBus`: a per-thread initiator stack with one reusable disposer instead of a `Tuple`, closure and wrapper per access; locked-range look-ups skipped until a range is first locked; the last context look-up cached until the peripheral collections change; tags binary-searched without a closure | Prepared as PRs 03 + 04 + 05, not submitted |
| 0003 | P5 | Register framework (`PeripheralRegister`): lists only when a field changes, a reused scratch list, `Distinct` only with two or more fields, a plain loop for tag checks, unhandled-write messages built only when logged; `CpuThreadPauseGuard`'s per-access `new object()` becomes a static marker; `HookDescriptorBase.ActivateNewHooks` a plain loop; noisy `$"..."` logs (opcode hooks, scheduled actions) built only when the level lets them through | Prepared as PRs 06 + 07, not submitted |
| 0004 | — | `STM32_Timer`'s update event leaves a compare channel with neither its interrupt nor its output enabled, whose timer is disabled and counts the parent's way, as it is (it re-applied all four every period: two clock-entry exchanges and CPU return requests each) (#260). The file is upstream PR 08's patch. (Until 1.17.0-vhil.4 it also did `BaseClockSource`'s tick arithmetic in integers, upstream PR 09: exact, but no measurable gain, so dropped there and here) | Prepared as PR 08, not submitted |
| 0005 + tlib/0001 | — | A pre-opcode execution hook can be excluded at an address: `TranslationCPU.ExcludePreOpcodeExecutionHookAt(action, pc)` and `ClearPreOpcodeExecutionHookExclusions()`, over tlib's `tlib_exclude_pre_opcode_execution_hook` / `tlib_clear_pre_opcode_execution_hook_exclusions` (an open-addressed set of (pc, hook id) pairs, 4,096 slots at most three quarters full, that `generate_pre_opcode_execution_hook` consults at translation; tlib/0001 is upstream PR 10's patch, whose pairs keep pc and hook id apart: the earlier key `(pc << 3) | hook id` collided for hook ids of 8 or more); the blocks holding the address are queued for invalidation. `VhilFastForward` excludes its CMP.W hook at every CMP.W that is not a delay loop's, so the hook reaches C# from the delay loops only (#260) | Prepared as PRs 10 (tlib) + 11 + 12 (tests), not submitted |
| tlib/0002 | — | The memory-access flag (`tlib_is_on_memory_access_enabled`, CPU_COMMON) set to 2 reports only peripheral (IO) and slow (unaligned, page-spanning) accesses to `tlib_on_memory_access`: a load or store that hits RAM in the same page is not reported. 1 reports every access as before; `tlib_on_memory_access_event_enabled` still sets 0 or 1. `VhilFastForward` arms its detector with 2: it watches MMIO only, and on a host whose TCG backend calls the softmmu helpers for every access (aarch64, no inline TLB fast path) every RAM load and store reached C# while it was armed (the bootloader's loop, which the detector re-proves every chunk, clears a struct byte by byte): on an M-series Mac boots 25-45 % faster, steady state up to a third. x86 hits RAM inline, so there only TLB misses go through the helpers | Not submitted |
| 0006 | — | A `SlaveTimeSource` can run on its master's thread (`RunsOnMasterThread`, set before it is attached): `TimeSourceBase.InnerExecute` split into `InnerExecuteBegin` (grant or unblock every handle) and `InnerExecuteEnd` (wait for them, the sync phase); the master begins a slave's iteration when it grants it time and ends it when it waits for it, so the slave's sinks run beside the master's others without the slave's dispatcher thread between them. `TimeHandle.RequestTimeInterval(noWait)` for a sink that cannot wait for its own source; `TimeDomainsManager.SwapCurrentThread` gives the slave's sync phase the slave's time stamp. Off by default; Renode's slave time-source unit tests run both ways. `VhilMachine` turns it on for every board of a lock-step run (invariant 11 holds: each board keeps a time source of its own), one hand-off a quantum per board and N + 1 threads instead of 2N + 1 (#260) | Prepared as PR 13, not submitted |
| 0007 | — | **Fidelity fix.** `STM32_Timer` counts ARR + 1 values per update period, as the chip does (RM0468 44.3.2: up from 0 to ARR, then an overflow to 0; down from ARR to 0, then an underflow to ARR), so its update event comes every (ARR + 1) x (PSC + 1) timer clocks; stock Renode's clock entry had ARR as its limit, one count short. Every HAL timebase (ARR = 999 at 1 MHz: TIM23 on the ECU, TIM6 on the AMS, TIM1 on the uDV) ticked every 999 us, 0.1 % fast, and the uDV's TIM2 (ARR = 2499) every 2.499 ms. The entry's limit is ARR + 1; counting down its value is CNT + 1 (a descending entry fires on reaching 0). `TIMx_CNT` now reads 0..ARR (it never read ARR); PWM mode 1 has a period of ARR + 1 counts and CCRx high (#292, `tests/sim/test_stm32_timer.py`) | Prepared as PR 14, not submitted |
| 0008 | — | `Packet`'s field wrappers (`FieldPropertyInfoWrapper`, already cached per type) keep each kind of attribute they looked up and the declaring type's byte order, instead of calling `GetCustomAttributes` for every attribute read of every field of every value decoded or encoded. MCAN decodes each filter element from the message RAM for every frame it receives (`FilterMessage`, `DecodeFilterElement`) and encodes every frame it stores (`StoreInRxFIFO`): on ecu-ams-udv (x86) the reflection was 6 % of the host's samples and the RX path 8-12 % | Not submitted |
| 0009 | — | `NVIC`'s ICSR.RETTOBASE value provider built the `SystemException` numbers by reflection (`Enum.GetValues`) and intersected them with the active exceptions through LINQ on every ICSR read; FreeRTOS's Cortex-M ports read ICSR in every outermost critical section (`vPortEnterCritical`'s `configASSERT` on VECTACTIVE). The numbers are built once, and with no exception active (Thread mode) the answer is immediate: the same value | Not submitted |

The study's P1 (blocking instead of spinning at the lock-step barrier) is
slower on 4 cores and P4 (aarch64 inline TLB) not worth it; neither is here.
When upstream takes a patch, the next release carries it: drop it from the
queue on that bump (plan below, "Upstreaming").

**What a run computes is unchanged by 0001-0006, 0008, 0009, tlib/0001 and tlib/0002**: bus
timelines, GPIO edges with the instruction count at each write, radio
payloads, each CPU's executed instructions, PC, state and RAM are
byte-identical against the release package (the study's fingerprint, and the
identity check of this queue's PR). **0007 changes it on purpose** (#292):
every STM32 timer's update event comes one count later, so the fingerprint
taken with 1.17.0-vhil.6 is the new reference. Against 1.17.0-vhil.5 (10 s
from power-on of ecu, ams, ecu-ams, udv and ecu-ams-udv; firmware at dev,
2026-10-15) only timing moved, by what the timers predict: every bus carries
the same frames (ids, data, senders) in the same order, and every watched pin
the same edges in the same order; the HAL tick counts 1 per 1000 us instead of
999 us (uwTick 7984 → 7976 on the ECU at 10 s, 7999 → 7991 on the AMS, 7960 →
7952 on the uDV: the app's 7.98 s at 1000/999); the uDV's 0x512 is 8 us later
per frame (8 TIM2 periods of one count each: 722 us of drift over 91 frames
gone, now on a 20 ms grid within 1 us); a frame the ECU sends at its n-th HAL
tick is n us later (0x510-0x515 at tick 11: 11 us); FreeRTOS-paced frames
move by at most 1 us (an ISR at another instant); the ECU's nRF24 bit-banged
SPI edges (PA5, PA7) and PB0, PC5 by at most 0.5 ms, where its status poll
waits for HAL_GetTick to advance (nrf24.c:726-773); the AMS's PB9 edges not
at all alone, by 1 or 9 us beside the ECU and the uDV (their frames moved).
Digests (the fingerprint script with uwTick added):
ecu `8781ed1d…` → `76394279…`, ams `0778b2ef…` → `74e362eb…`, ecu-ams `b839edac…` →
`6a8427e0…`, udv `b09e608e…` → `3b280945…`, ecu-ams-udv `3b9faadc…` →
`ee8cef99…`.
`tests/sim/test_fast_forward.py`, `tests/sim/test_lazy_dma.py` and
`test_two_board_runs_give_the_same_bus_timeline` hold runs identical to each
other (with and without fast-forward, lazy DMA; run against run) in CI.

## Upstreaming

The six patches and the tlib one are prepared as twelve pull requests to
three repositories (renode/renode-infrastructure, antmicro/tlib and
renode/renode for the tests; numbered 01-08 and 10-13, 09 was dropped),
rebased on upstream master, tested with Renode's own suites and measured
there:
[`docs/proposals/renode-upstream/`](../../docs/proposals/renode-upstream/README.md)
(submission order, commands, the CLA). Plan for the queue:

| Local patch | Replaced by upstream PRs | Drop it when |
|---|---|---|
| 0001 | 01 (bug fix) + 02 | the Renode release the vHIL moves to contains both |
| 0002 | 03 + 04 + 05 | the release contains all three |
| 0003 | 06 + 07 | the release contains both |
| 0004 | 08 (STM32_Timer; the clock source arithmetic, prepared as 09, was dropped here and there) | the release contains it |
| 0005 + tlib/0001 | 10 (antmicro/tlib) + 11 (TranslationCPU) + 12 (renode's tests) | the release's tlib has 10 and its renode-infrastructure 11; this is a new API, proposed through an issue first, and may come back in another shape (then `VhilFastForward` follows it) or be declined (then the two patches stay here) |
| 0006 | 13 (three commits: the `InnerExecute` split, `RequestTimeInterval(noWait)`, `SlaveTimeSource.RunsOnMasterThread`) | the release contains it; an opt-in mode of a path upstream's own machines no longer take (`mach create` shares the master time source), proposed through issue D first: upstream may make it a default or a `Machine` argument (then `VhilMachine` follows) or decline it (then 0006 stays here) |

Upstream lands a PR through Antmicro's tree (the PR shows "Closed" when its
commit is on master), possibly rewritten, and a release follows. Until the
vHIL bumps to a release with every PR that replaces a patch, that patch
stays here unchanged on 1.17.0; on the bump, a patch whose PRs all landed is
deleted instead of rebased (step 2 of "Moving to a new Renode release"),
after checking the landed commits do what the patch did (the fingerprint
identity check covers the result). A patch whose PRs are partly in is
rebased down to what is still missing. If upstream rejects a PR, its part of
the patch stays in the queue.

The PRs differ from the local patches in details reviewed for upstream (a
separate bug fix and its tests, an exact first-match index, a look-up cache
owned by the bus and held weakly by threads, no reused scratch list,
(pc, hook) keys that don't collide for hook ids of 8 or more, 0006 rebased
onto upstream's reworked `InnerExecute` and split in three; table in the
upstream README), not in what a run computes: the vHIL's fingerprint is
byte-identical between upstream master and master with every PR (with 13:
ecu, ams, ecu-ams and ecu-ams-ecu, 10 s from power-on).

## Which build ran

`build.sh` writes `VHIL_RENODE_BUILD` beside the launcher, one line:

```
renode 1.17.0 (ab721d88e135) + vhil patches 1.17.0-vhil.9 [0001,0002,0003,0004,0005,0006,0007,0008,0009,tlib/0001,tlib/0002] queue <sha256 of the patches, 12 hex>
```

`vhil.renode.build_id()` reads it: `launch()` writes it as the first line
of every Renode log (`vhil: renode 1.17.0 ...`), pytest prints it in the
session header, `scripts/speed.py` records it under `host.renode`, and a
worker run's summary carries it as `renode`. A Renode without the file (a
release package, a native install) reports itself as a release package.
`renode --version` prints the tag's commit and the build date
(`1.17.0+<date>git<commit>`), not the patches.

## Building

Inside the image build (`docker build -t ifs-vhil docker/`, `scripts/vhil-docker.sh
image`), or just the package:

```sh
docker buildx build --target renode-package --output type=local,dest=build/renode docker/
build/renode/renode --version; cat build/renode/VHIL_RENODE_BUILD
```

About 6-8 minutes on 4 cores from nothing, the network included (Renode's
submodules, renode-resources and NuGet packages); BuildKit caches the stage,
so it builds again only when `docker/renode/` or the SDK image changes. The
build needs a case-sensitive file system (the portable packaging renames
`Renode` to `renode`): any Linux Docker host, Colima's VM included, not a
bind mount from macOS. It builds natively on amd64 and arm64; a
multi-platform `buildx --platform linux/amd64,linux/arm64` build compiles
the foreign one under emulation, which is slow: build each platform on a
native node where you can.

A source build's .NET runtime is the SDK image's (8.0.x), not the one the
release packaged.

The package's assemblies are compiled ReadyToRun (`-p:PublishReadyToRun=true`,
crossgen2): a Renode starts on native code instead of JIT-compiling every
method it runs, and tiering still re-JITs the hot ones. Same IL, so what a
run computes is unchanged ([`docs/performance.md`](../../docs/performance.md)).

## Moving to a new Renode release

1. Clone the new tag with its submodules, and record them:
   ```sh
   git clone --recurse-submodules --branch v<new> https://github.com/renode/renode
   cd renode
   git submodule foreach --recursive --quiet 'echo "$(git rev-parse HEAD) $displaypath"' \
       > <vhil>/docker/renode/submodules.txt
   ```
2. Re-apply the queue in renode-infrastructure, dropping what upstream took:
   ```sh
   cd src/Infrastructure
   git am -3 <vhil>/docker/renode/patches/*.patch     # resolve, `git am --continue`
   git format-patch -N -o <vhil>/docker/renode/patches/ <tag's infrastructure commit>
   git write-tree                                       # RENODE_INFRA_PATCHED_TREE
   ```
   and `patches/tlib/` the same way in `src/Emulator/Cores/tlib`
   (`RENODE_TLIB_PATCHED_TREE`). Keep the numbering contiguous (`0001-...`),
   one upstream-sized change per patch.
3. In `pins.env`: `RENODE_VERSION`, `RENODE_COMMIT` (`git ls-remote
   https://github.com/renode/renode "refs/tags/v<new>*"`), the
   renode-resources commit the release's build uses, a new `VHIL_PATCH_SET`
   (`<new>-vhil.1`), `RENODE_INFRA_PATCHED_TREE`, `RENODE_TLIB_PATCHED_TREE`.
4. Build the package; check `VhilFastForward` still calibrates (it finds
   tlib's counters at run time) and that its `CMP.W` hook still fires
   (`scripts/speed.py`'s `fast_forward` reports CMP.W hooks > 0 on the ECU):
   upstream master (tlib `ff1bb507`, renode-infrastructure `c47c11a6`,
   after 1.17.0) passes a 32-bit Thumb-2 instruction to opcode hooks as the
   whole word, first halfword in the upper 16 bits, so the hook's mask
   (`0xFBF0` / `0xF1B0`) must become `0xFBF00000` / `0xF1B00000` on the
   first release with that change, or the counter-loop skips silently stop and that a run is byte-identical to the new
   release package's (the study's `fingerprint.py`); run the full CI
   (`full-ci`) and the `emulation speed` job. Upstream master also moved
   `Monitor.SanitizePathSeparator` to `Antmicro.Renode.Utilities.PathHelpers`
   (renode-infrastructure `24b3a005`, #104075): `VhilMonitor.cs` (#284)
   uses it, and the models don't compile until it follows.
5. Bump `1.17.0` everywhere else in the same PR (CLAUDE.md, `.github/`,
   `docs/`): a Renode bump is its own PR.

Changing a patch on the same release: edit it, bump `VHIL_PATCH_SET`'s
suffix and update `RENODE_INFRA_PATCHED_TREE` or `RENODE_TLIB_PATCHED_TREE`
(the build prints the tree it got when they differ).
