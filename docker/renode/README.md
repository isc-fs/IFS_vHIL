# Renode 1.17.0 with the vHIL's patch queue

The vHIL runs Renode **1.17.0** built from source with six patches to
renode-infrastructure and one to tlib that make every system faster (the first three
13-58 %) and change nothing a run computes. They are a patch queue on the
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
| 0001 | P2 | `BaseClockSource`: a dictionary from handler to clock-entry index beside the list (no linear delegate search per timer reload, `GetClockEntry` or `ScheduleAction`), plain fields for two `ThreadLocal<bool>` flags only touched under the source's lock, one reused list per update; `TryRemoveClockEntry` looks the entry up again after `UpdateLimits` (stock Renode can remove the wrong entry if a handler removed one below it) | Not submitted (awaiting the CLA) |
| 0002 | P3 | `SystemBus`: a per-thread initiator stack with one reusable disposer instead of a `Tuple`, closure and wrapper per access; locked-range look-ups skipped until a range is first locked; the last context look-up cached until the peripheral collections change; tags binary-searched without a closure | Not submitted |
| 0003 | P5 | Register framework (`PeripheralRegister`): lists only when a field changes, a reused scratch list, `Distinct` only with two or more fields, a plain loop for tag checks, unhandled-write messages built only when logged; `CpuThreadPauseGuard`'s per-access `new object()` becomes a static marker; `HookDescriptorBase.ActivateNewHooks` a plain loop; noisy `$"..."` logs (opcode hooks, scheduled actions) built only when the level lets them through | Not submitted |
| 0004 | — | `STM32_Timer`'s update event leaves a compare channel with neither its interrupt nor its output enabled, whose timer is disabled and counts the parent's way, as it is (it re-applied all four every period: two clock-entry exchanges and CPU return requests each) (#260). The file is upstream PR 08's patch. (Until 1.17.0-vhil.4 it also did `BaseClockSource`'s tick arithmetic in integers, upstream PR 09: exact, but no measurable gain, so dropped there and here) | PR 08 (prepared in `docs/proposals/renode-upstream/`, not submitted) |
| 0005 + tlib/0001 | — | A pre-opcode execution hook can be excluded at an address: `TranslationCPU.ExcludePreOpcodeExecutionHookAt(action, pc)` and `ClearPreOpcodeExecutionHookExclusions()`, over tlib's `tlib_exclude_pre_opcode_execution_hook` / `tlib_clear_pre_opcode_execution_hook_exclusions` (an open-addressed set of (pc, hook id) pairs, 4,096 slots at most three quarters full, that `generate_pre_opcode_execution_hook` consults at translation; tlib/0001 is upstream PR 10's patch, whose pairs keep pc and hook id apart: the earlier key `(pc << 3) | hook id` collided for hook ids of 8 or more); the blocks holding the address are queued for invalidation. `VhilFastForward` excludes its CMP.W hook at every CMP.W that is not a delay loop's, so the hook reaches C# from the delay loops only (#260) | tlib/0001: PR 10 (prepared, not submitted); 0005: not submitted |
| 0006 | — | A `SlaveTimeSource` can run on its master's thread (`RunsOnMasterThread`, set before it is attached): `TimeSourceBase.InnerExecute` split into `InnerExecuteBegin` (grant or unblock every handle) and `InnerExecuteEnd` (wait for them, the sync phase); the master begins a slave's iteration when it grants it time and ends it when it waits for it, so the slave's sinks run beside the master's others without the slave's dispatcher thread between them. `TimeHandle.RequestTimeInterval(noWait)` for a sink that cannot wait for its own source; `TimeDomainsManager.SwapCurrentThread` gives the slave's sync phase the slave's time stamp. Off by default; Renode's slave time-source unit tests run both ways. `VhilMachine` turns it on for every board of a lock-step run (invariant 11 holds: each board keeps a time source of its own), one hand-off a quantum per board and N + 1 threads instead of 2N + 1 (#260) | Not submitted |

The study's P1 (blocking instead of spinning at the lock-step barrier) is
slower on 4 cores and P4 (aarch64 inline TLB) not worth it; neither is here.
When upstream takes a patch, the next release carries it: drop it from the
queue on that bump.

**What a run computes is unchanged**: bus timelines, GPIO edges with the
instruction count at each write, radio payloads, each CPU's executed
instructions, PC, state and RAM are byte-identical against the release
package (the study's fingerprint, and the identity check of this queue's
PR). `tests/sim/test_fast_forward.py` and
`test_two_board_runs_give_the_same_bus_timeline` hold it in CI.

## Which build ran

`build.sh` writes `VHIL_RENODE_BUILD` beside the launcher, one line:

```
renode 1.17.0 (ab721d88e135) + vhil patches 1.17.0-vhil.5 [0001,0002,0003,0004,0005,0006,tlib/0001] queue <sha256 of the patches, 12 hex>
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
   tlib's counters at run time) and that a run is byte-identical to the new
   release package's (the study's `fingerprint.py`); run the full CI
   (`full-ci`) and the `emulation speed` job.
5. Bump `1.17.0` everywhere else in the same PR (CLAUDE.md, `.github/`,
   `docs/`): a Renode bump is its own PR.

Changing a patch on the same release: edit it, bump `VHIL_PATCH_SET`'s
suffix and update `RENODE_INFRA_PATCHED_TREE` or `RENODE_TLIB_PATCHED_TREE`
(the build prints the tree it got when they differ).
