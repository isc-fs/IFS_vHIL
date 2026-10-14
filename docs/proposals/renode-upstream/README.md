# Upstreaming the Renode patch queue

The vHIL builds Renode 1.17.0 with three local patches
([`docker/renode/patches/`](../../../docker/renode/patches/), study:
[`../renode-fork-study.md`](../renode-fork-study.md)). This directory holds
them rewritten as pull requests to
[renode/renode-infrastructure](https://github.com/renode/renode-infrastructure),
rebased on upstream master, split the way upstream reviews, tested with
Renode's own suites and measured on upstream's own workloads. **Nothing here
has been posted**: Raul submits them after signing the CLA.

All the changes are in renode-infrastructure (the emulator's C# core). None
belongs in renode/renode (it only carries the submodule pointer, which
Antmicro bumps when a change lands; the issues go there, below) or tlib (no
patch touches the translation library).

## The pull requests

Base: renode master `59f8f5d0` (2026-10-12) and renode-infrastructure master
`5474222a` (the commit renode master points at; still both heads on
2026-10-14).

| # | Directory | Title | Commits | Size | Depends on |
|---|---|---|---|---|---|
| 01 | [`01-clock-entry-stale-index/`](01-clock-entry-stale-index/) | [FIX] ClockSource: Look the entry up again after running handlers | 1 | +82/-7, 2 files (56 lines are tests) | - |
| 02 | [`02-clock-entry-index/`](02-clock-entry-index/) | ClockSource: Find clock entries by handler with a dictionary | 2 | +149/-47 over 01, 2 files (34 lines are a test) | 01 |
| 03 | [`03-sysbus-locked-ranges-and-tags/`](03-sysbus-locked-ranges-and-tags/) | SystemBus: Skip locked range look-ups and the tag search's closure | 2 | +37/-1, 1 file | - |
| 04 | [`04-sysbus-access-context/`](04-sysbus-access-context/) | SystemBus: Set the access context without allocating | 1 | +83/-11, 2 files (27 lines are a test) | - |
| 05 | [`05-sysbus-context-lookup-cache/`](05-sysbus-context-lookup-cache/) | SystemBus: Keep each thread's last context look-up | 1 | +53/-0, 1 file | - |
| 06 | [`06-register-framework-allocations/`](06-register-framework-allocations/) | PeripheralRegister: Avoid allocations on every register access | 2 | +60/-30, 1 file | - |
| 07 | [`07-cpu-hot-path-allocations/`](07-cpu-hot-path-allocations/) | TranslationCPU, Machine: Avoid allocations and noisy log formatting on hot paths | 2 | +25/-6, 4 files | - |

Each directory has the `git format-patch` output against renode-infrastructure
master (`*.patch`, author and committer `Raul Moran <rmg@raulmoranguerra.com>`,
no trailers, ready to `git am`; 02's patches apply on top of 01's) and
`PR.md`: the title on its first line, then the body, in the sections of
upstream's PR template (Related issue, Description, Usage example,
Additional information). [`ISSUES.md`](ISSUES.md) has the two issues to open
first in renode/renode. The measurements: [`results.md`](results.md). The
scripts: [`tooling/`](tooling/).

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
  version.
- **06**: register framework: changed-fields list only when a field
  changes, no `Distinct`/LINQ on the common path, unhandled-write messages
  built only when they can be logged.
- **07**: pause guard's `new object()` per bus access, LINQ before every
  execution chunk, noisy messages formatted before being dropped.

How they map to the local queue:

| Local patch | Upstream PRs | Differences from the local patch |
|---|---|---|
| 0001 (P2, clock source) | 01 + 02 | The `TryRemoveClockEntry` re-lookup is its own bug-fix PR (01), extended to the same stale index in `GetClockEntry` (after the entry's own handler, and after a full update), with tests that fail on master. 02 keeps the first entry per handler (what the linear search found, also with duplicates), searches null handlers linearly, updates the index incrementally on removal, and has a test |
| 0002 (P3, system bus) | 03 + 04 + 05 | The tag search mirrors `Misc.BinarySearch` exactly. The access context restores the depth from before each push (stock's "put the previous context back", also out of order) and drops the thread-static cache. The context look-up cache is per thread, never allocates, and is also invalidated when a context key is (re-)added, which stock code can do without dropping its caches |
| 0003 (P5, allocations) | 06 + 07 | No reused scratch list for writes (it needed a re-entrancy flag): the changed-fields list is allocated when the first field changes; no enumerator boxing; log checks use `Logger.MinimumLogLevel <= level`, as `PL330_DMA` already does |

The differences are review-driven, not behavioural: the vHIL's fingerprint
is byte-identical between master and master with the seven PRs, as it was
between 1.17.0 and 1.17.0 with the local queue.

## Results in short

Details and every number: [`results.md`](results.md).

- **Renode's tests**, master vs master + all seven: NUnit 1,095 → 1,099
  passed (the four new tests), 18 skipped, 0 failed; 38 Robot suites 330
  passed, 1 skipped, 0 failed on both. 01's two tests fail on master. Each PR
  builds alone and passes `dotnet format whitespace --verify-no-changes`.
- **Identity**: the vHIL's fingerprint (bus timelines, GPIO edges with
  instruction counts, CPU state and RAM digests, 3.5 s of ecu, ams and
  ecu-ams) is the same file on master and on all.
- **Speed, all seven**: the vHIL's systems 9-53 % faster (steady state
  13-53 %); upstream's bus-performance loop 2.38x, a register read 1.69x, a
  register write 2.23x as fast; allocation per bus access 656 → 92 B, per
  register write 1.36 kB → 280 B.
- **Not measured cleanly**: host time on Renode's demos and per PR (another
  job shared the host; the demos barely touch the changed paths anyway:
  they allocate 600-3,800 times less than the loops). Per-PR allocation is
  measured (deterministic). Commands to re-run on a quiet host: results.md.

## Order and dependencies

01 first; 02 after it (02's commits sit on 01's: the index must be right
after handlers run, which is 01's fix). 03 to 07 are independent of each
other and of 01/02: each applies to master alone, and all seven apply
together in any order (03, 04 and 05 touch different parts of
`SystemBus.cs`). Suggested order, smallest risk first, so the first reviews
are easy:

1. 01 (bug fix, with failing-on-master tests)
2. 03 and 07 (a few lines each)
3. 06, 04, 05
4. 02 (once 01 is in, or at least reviewed)

Opening 03-07 at once is fine too; each says it is one of a series from
issue B. Upstream merges through its own tree (often rewriting: they
simplified renode-infrastructure#217 and "modified and merged" #207), so
expect each PR to be closed rather than merged, with its commit on master.

## Submitting (Raul)

### 1. The CLA

Sign Antmicro's Contributor License Agreement: renode-infrastructure's at
<https://cla-assistant.io/renode/renode-infrastructure> (the CLA-assistant
bot also posts this link on the first PR) and, as upstream's PR template
asks, <https://cla-assistant.io/renode/renode>. Sign with the GitHub
account that opens the PRs; the commits' author email
(`rmg@raulmoranguerra.com`) must be one of that account's verified emails,
or CLA-assistant reports the commits as unsigned. To author them with
another address, rewrite them after `git am` (step 3) and before pushing:
`git -c user.email=<address> rebase --exec 'git commit --amend --no-edit --reset-author' origin/master`.

### 2. The issues

Upstream's CONTRIBUTING.md asks for an issue even when you send the fix
yourself. Open [`ISSUES.md`](ISSUES.md)'s issue A (bug report template) and
issue B (feature request template) at
<https://github.com/renode/renode/issues/new/choose>. Note their numbers:
`A` and `B` below.

### 3. Fork, apply, push, open

```sh
gh repo fork renode/renode-infrastructure --clone=false     # once, under your account
git clone https://github.com/renode/renode-infrastructure && cd renode-infrastructure
git remote add fork https://github.com/<you>/renode-infrastructure
git fetch origin master
P=<path to IFS_vHIL>/docs/proposals/renode-upstream
A=<issue A number>; B=<issue B number>

# Fill the placeholders in the PR bodies (PR_01 once 01 is open, for 02)
sed -i '' "s/ISSUE_A/$A/g; s/ISSUE_B/$B/g" $P/*/PR.md

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
pr 06-register-framework-allocations $B-register_framework_allocations
pr 04-sysbus-access-context          $B-sysbus_access_context
pr 05-sysbus-context-lookup-cache    $B-sysbus_context_lookup_cache
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

The `sed -i ''` edits the copies in this checkout; don't commit them back
(or do, on a branch, once the numbers are known).

Optional, upstream style: prefix the commit subjects with the issue
(`[#123] ClockSource: ...`), after `git am` on each branch:

```sh
git rebase origin/master --exec \
  'git commit --amend --no-edit -m "$(git log -1 --format=%B | sed "1s/^\(\[FIX\] \)\{0,1\}/[#'$B'] /")"'
```

(01 with `$A`; on 02's branch rebase onto `$A-clock_entry_stale_index`
instead, so only 02's two commits change.) Recent external PRs have landed
both with and without it (renode-infrastructure#217 `[#941] NVIC: ...`,
#224 `[#953] ...`; #220 and #225 without), so it is a nicety.

Upstream's PR template also asks for a reproduction repository from
<https://github.com/renode/renode-issue-reproduction-template>. 01 has NUnit
tests that fail on master instead; 02-07 change no behaviour, and the
benchmark scripts quoted in issue B are their reproduction. Each `PR.md`
says so; build the repository only if a reviewer asks.

### 4. When upstream asks for changes

- Make the change on the PR's branch in the renode-infrastructure clone,
  keeping one logical change per commit (`git commit --fixup <sha>`, then
  `git rebase -i --autosquash origin/master`), and
  `git push --force-with-lease fork HEAD`.
- Re-run the checks before pushing (Docker only, [`tooling/`](tooling/)):
  `tooling/build.sh <name> <branch> $(grep -v '^#' tooling/tests.txt)`
  builds master + the branch and runs the suites; `dotnet format whitespace
  --folder --verify-no-changes --include <files>` in the builder image checks
  the formatting. For an optimisation, re-run the fingerprint
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
# in $RENODE_UPSTREAM/src/Infrastructure: branch up/master = origin/master,
# up/NN-... = `git am` of each directory's patches, up/all = all seven
docker build -t renode-up-builder -f $T/builder.Dockerfile $T
docker build -t renode-up-runtime -f $T/runtime.Dockerfile $T          # ifs-vhil + perf
$T/build.sh master up/master $(grep -v '^#' $T/tests.txt)              # build + Renode's suites
$T/build.sh all up/all $(grep -v '^#' $T/tests.txt)
$T/bench.sh $WORK/results/generic.jsonl 5 "master all"                 # generic workloads
python3 $T/summarize_bench.py $WORK/results/generic.jsonl master
$T/vhil-ab.sh speed ab 5 "master all"                                  # the vHIL's speed.py
python3 $T/summarize_vhil.py $WORK/results/ab master
$T/vhil-ab.sh fingerprint master; $T/vhil-ab.sh fingerprint all        # then compare the two files
$T/profile.sh master stock-ecu-ams ecu-ams sim                         # perf; ../renode-patches/tooling/stacks.py
```
