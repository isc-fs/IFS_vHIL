# Upstreaming the Renode patch queue

The vHIL builds Renode 1.17.0 with five local patches to renode-infrastructure
and one to tlib ([`docker/renode/patches/`](../../../docker/renode/patches/),
study: [`../renode-fork-study.md`](../renode-fork-study.md)). This directory
holds them rewritten as pull requests to the three upstream repositories,
rebased on upstream master, split the way upstream reviews, tested with
Renode's own suites and measured on upstream's own workloads. **Nothing here
has been posted**: Raul submits them after signing the CLA.

Which repository each change belongs in:

- [renode/renode-infrastructure](https://github.com/renode/renode-infrastructure),
  the emulator's C# core: PRs 01-08 and 11;
- [antmicro/tlib](https://github.com/antmicro/tlib), the translation library
  (C, a submodule of renode-infrastructure at `src/Emulator/Cores/tlib`):
  PR 10, which PR 11 needs;
- [renode/renode](https://github.com/renode/renode), which carries the
  submodule pointer (Antmicro bumps it when a change lands), the Robot
  tests and the issues: PR 12 (tests for 10 + 11) and issues A, B and C.

## The pull requests

Base: renode master `9b5195e6`, renode-infrastructure master `3e4301cf`
(the commit renode master points at) and tlib master `0dd9a30c` (the commit
renode-infrastructure points at), the heads on 2026-10-14. PRs 01-07 were
first prepared on `59f8f5d0` / `5474222a`; their patches apply unchanged to
the new base (`git am`, same trees), so only 05's (reworked, below) is new.

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

Each directory has the `git format-patch` output against its repository's
master (`*.patch`, author and committer `Raul Moran <rmg@raulmoranguerra.com>`,
no trailers, ready to `git am`; 02's patches apply on top of 01's; 11's
patch is the C# change alone, without the commit that moves the tlib
submodule, see "Submitting") and `PR.md`: the title on its first line, then
the body, in the sections of upstream's PR template (Related issue,
Description, Usage example, Additional information). [`ISSUES.md`](ISSUES.md)
has the three issues to open first in renode/renode. The measurements:
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

How they map to the local queue:

| Local patch | Upstream PRs | Differences from the local patch |
|---|---|---|
| 0001 (P2, clock source) | 01 + 02 | The `TryRemoveClockEntry` re-lookup is its own bug-fix PR (01), extended to the same stale index in `GetClockEntry` (after the entry's own handler, and after a full update), with tests that fail on master. 02 keeps the first entry per handler (what the linear search found, also with duplicates), searches null handlers linearly, updates the index incrementally on removal, and has a test |
| 0002 (P3, system bus) | 03 + 04 + 05 | The tag search mirrors `Misc.BinarySearch` exactly. The access context restores the depth from before each push (stock's "put the previous context back", also out of order) and drops the thread-static cache. The context look-up cache is per thread, never allocates on a hit, is also invalidated when a context key is (re-)added, which stock code can do without dropping its caches, and is owned by the dictionary with threads referring to it weakly (no machine kept alive by a thread that outlives it) |
| 0003 (P5, allocations) | 06 + 07 | No reused scratch list for writes (it needed a re-entrancy flag): the changed-fields list is allocated when the first field changes; no enumerator boxing; log checks use `Logger.MinimumLogLevel <= level`, as `PL330_DMA` already does |
| 0004 (timers) | 08 | Only the timer half is submitted. The clock source's integer arithmetic (prepared as PR 09) was dropped: exact, but no measurable host-time gain |
| 0005 + tlib/0001 (hook exclusion) | 10 + 11 + 12 | tlib keys the set by (pc, hook id) pairs; the local patch packs them as `(pc << 3) \| id`, which collides for hook ids of 8 or more (`CPU_HOOKS_MASKS_LIMIT` is 256; harmless for the vHIL, whose only pre-opcode hook is id 0). Same API names and semantics, so `VhilFastForward` uses the upstream version as is; tests added |

The differences are review-driven, not behavioural: the vHIL's fingerprint
is byte-identical between master and master with every PR, as it was
between 1.17.0 and 1.17.0 with the local queue.

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
`src/Infrastructure`. Suggested order, smallest risk first:

1. 01 (bug fix, with failing-on-master tests)
2. 03, 07 and 08 (a few lines each)
3. 06, 04, 05
4. 02 (once 01 is in, or at least reviewed)
5. issue C; once Antmicro agrees on the shape: 10, then 11 (after tlib's
   master has 10 and renode-infrastructure's submodule points there), then 12

Opening 03-08 at once is fine too; each says it is one of a series from
issue B. Upstream merges through its own tree (often rewriting: they
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
issue B (feature request template, the performance series) and issue C
(feature request template, the hook exclusion) at
<https://github.com/renode/renode/issues/new/choose>. Note their numbers:
`A`, `B` and `C` below. For C, wait for a maintainer's answer before opening
10-12 if you prefer not to spend review time on a shape they would change.

### 3. Fork, apply, push, open

renode-infrastructure, PRs 01-08:

```sh
gh repo fork renode/renode-infrastructure --clone=false     # once, under your account
git clone https://github.com/renode/renode-infrastructure && cd renode-infrastructure
git remote add fork https://github.com/<you>/renode-infrastructure
git fetch origin master
P=<path to IFS_vHIL>/docs/proposals/renode-upstream
A=<issue A number>; B=<issue B number>; C=<issue C number>

# Fill the placeholders in the PR bodies (PR_01 once 01 is open, for 02;
# PR_10, PR_11, PR_12 once those are open)
sed -i '' "s/ISSUE_A/$A/g; s/ISSUE_B/$B/g; s/ISSUE_C/$C/g" $P/*/PR.md

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

The `sed -i ''` edits the copies in this checkout; don't commit them back
(or do, on a branch, once the numbers are known).

Optional, upstream style: prefix the commit subjects with the issue
(`[#123] ClockSource: ...`), after `git am` on each branch:

```sh
git rebase origin/master --exec \
  'git commit --amend --no-edit -m "$(git log -1 --format=%B | sed "1s/^\(\[FIX\] \)\{0,1\}/[#'$B'] /")"'
```

(01 with `$A`, 10-12 with `$C`; on 02's branch rebase onto
`$A-clock_entry_stale_index` instead, so only 02's two commits change.)
Recent external PRs have landed both with and without it
(renode-infrastructure#217 `[#941] NVIC: ...`, #224 `[#953] ...`; #220 and
#225 without; Antmicro added `[GH#995]` to tlib#45 themselves), so it is a
nicety.

Upstream's PR template also asks for a reproduction repository from
<https://github.com/renode/renode-issue-reproduction-template>. 01 has NUnit
tests that fail on master instead, 10-12
Robot tests; 02-08 change no behaviour, and the benchmark scripts quoted in
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
```

The whole round (builds, both test runs, every benchmark) took about five
hours on this host, most of it the vHIL's `speed.py` runs.

