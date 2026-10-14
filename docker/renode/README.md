# Renode 1.17.0 with the vHIL's patch queue

The vHIL runs Renode **1.17.0** built from source with three patches to
renode-infrastructure that make every system 13-58 % faster and change
nothing a run computes. They are a patch queue on the release tag, not a
fork: `build.sh` fetches the tag, checks every input against a pin, applies
`patches/` in order and builds the portable headless package, the release
tarball's layout. `docker/Dockerfile`'s `renode-build` stage runs it; the
vHIL image copies the package to `/opt/renode`, and CI exports the same
stage (`.github/actions/setup-renode`). Why and how they were measured:
[`docs/proposals/renode-fork-study.md`](../../docs/proposals/renode-fork-study.md) (#260).

| File | What |
|---|---|
| `pins.env` | Release, its commit, renode-resources' commit, the patch set's id, and the patched renode-infrastructure tree |
| `submodules.txt` | Every submodule's commit (recursive) as the tag records it |
| `patches/` | The queue, applied in order to `src/Infrastructure` (renode-infrastructure) |
| `build.sh` | Fetch, check, patch, build, package; writes `VHIL_RENODE_BUILD` |

## The patches

| # | Study name | What it does | Upstream |
|---|---|---|---|
| 0001 | P2 | `BaseClockSource`: a dictionary from handler to clock-entry index beside the list (no linear delegate search per timer reload, `GetClockEntry` or `ScheduleAction`), plain fields for two `ThreadLocal<bool>` flags only touched under the source's lock, one reused list per update; `TryRemoveClockEntry` looks the entry up again after `UpdateLimits` (stock Renode can remove the wrong entry if a handler removed one below it) | Prepared as PRs 01 + 02, not submitted (CLA) |
| 0002 | P3 | `SystemBus`: a per-thread initiator stack with one reusable disposer instead of a `Tuple`, closure and wrapper per access; locked-range look-ups skipped until a range is first locked; the last context look-up cached until the peripheral collections change; tags binary-searched without a closure | Prepared as PRs 03 + 04 + 05, not submitted |
| 0003 | P5 | Register framework (`PeripheralRegister`): lists only when a field changes, a reused scratch list, `Distinct` only with two or more fields, a plain loop for tag checks, unhandled-write messages built only when logged; `CpuThreadPauseGuard`'s per-access `new object()` becomes a static marker; `HookDescriptorBase.ActivateNewHooks` a plain loop; noisy `$"..."` logs (opcode hooks, scheduled actions) built only when the level lets them through | Prepared as PRs 06 + 07, not submitted |

The study's P1 (blocking instead of spinning at the lock-step barrier) is
slower on 4 cores and P4 (aarch64 inline TLB) not worth it; neither is here.
When upstream takes a patch, the next release carries it: drop it from the
queue on that bump (plan below, "Upstreaming").

**What a run computes is unchanged**: bus timelines, GPIO edges with the
instruction count at each write, radio payloads, each CPU's executed
instructions, PC, state and RAM are byte-identical against the release
package (the study's fingerprint, and the identity check of this queue's
PR). `tests/sim/test_fast_forward.py` and
`test_two_board_runs_give_the_same_bus_timeline` hold it in CI.

## Upstreaming

The three patches are prepared as seven pull requests to
renode/renode-infrastructure, rebased on upstream master, tested with
Renode's own suites and measured there:
[`docs/proposals/renode-upstream/`](../../docs/proposals/renode-upstream/README.md)
(submission order, commands, the CLA). Plan for the queue:

| Local patch | Replaced by upstream PRs | Drop it when |
|---|---|---|
| 0001 | 01 (bug fix) + 02 | the Renode release the vHIL moves to contains both |
| 0002 | 03 + 04 + 05 | the release contains all three |
| 0003 | 06 + 07 | the release contains both |

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
separate bug fix and its tests, an exact first-match index, per-thread
look-up cache, no reused scratch list; table in the upstream README), not in
what a run computes: the vHIL's fingerprint is byte-identical between
upstream master and master with the seven PRs.

## Which build ran

`build.sh` writes `VHIL_RENODE_BUILD` beside the launcher, one line:

```
renode 1.17.0 (ab721d88e135) + vhil patches 1.17.0-vhil.1 [0001,0002,0003] queue <sha256 of the patches, 12 hex>
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
   Keep the numbering contiguous (`0001-...`), one upstream-sized change per
   patch.
3. In `pins.env`: `RENODE_VERSION`, `RENODE_COMMIT` (`git ls-remote
   https://github.com/renode/renode "refs/tags/v<new>*"`), the
   renode-resources commit the release's build uses, a new `VHIL_PATCH_SET`
   (`<new>-vhil.1`), `RENODE_INFRA_PATCHED_TREE`.
4. Build the package; check `VhilFastForward` still calibrates (it finds
   tlib's counters at run time) and that a run is byte-identical to the new
   release package's (the study's `fingerprint.py`); run the full CI
   (`full-ci`) and the `emulation speed` job.
5. Bump `1.17.0` everywhere else in the same PR (CLAUDE.md, `.github/`,
   `docs/`): a Renode bump is its own PR.

Changing a patch on the same release: edit it, bump `VHIL_PATCH_SET`'s
suffix and update `RENODE_INFRA_PATCHED_TREE` (the build prints the tree
it got when they differ).
