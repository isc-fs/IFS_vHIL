# CI policy and test-impact selection

The heavy CI (the native tests, the smoke suites, the scenarios, IFS_HIL's
suites on the virtual bench) costs runner-hours, so it runs only in special
cases (owner decision, 2026-10-06), and a `full-ci` pull request runs only
what its changes can affect, plus a guard.

## When what runs

| Event | Unit (`unit`) | Heavy CI |
|---|---|---|
| Pull request, no label | yes | nothing (the jobs are skipped, which counts as passing) |
| Pull request labelled `full-ci` | yes | the **guard** + what the changed paths select (`ci/impact.yaml`) |
| Pull request labelled `full-ci-all` | yes | everything |
| Release pull request into `main` | yes | everything |
| Nightly on `dev` (schedule) | (on every push to `dev`) | everything, plus the impact map |
| Manual dispatch | - | everything |

The editor check (`editor.yml`) is separate: it runs on any pull request, and
any push to `dev`, whose paths `ci/impact.yaml` maps to `editor: true`,
labelled or not.

Label a PR `full-ci` when it can change what the emulated firmware does
(models, platform, catalogue, systems, `vhil/`'s simulation code, tests, the
workflows); `full-ci-all` when you want the whole set anyway (a refactor
across the run path, before a release). Docs, web app and editor-only changes
need neither.

## The guard

Every heavy run (`full-ci` whatever it selects, `full-ci-all`, nightly, main)
runs, besides the unit tests every PR runs:

- **The identity fingerprint of all six systems** (ecu, ams, ecu-ams, udv,
  ecu-ams-udv and the benchmark ecu-ams-ecu): everything a lock-step run
  observes in its first 5 s from power-on (every bus's timeline, watched GPIO
  edges with instruction counts, radio payloads, each CPU's instructions, PC,
  state and RAM digest, uwTick), hashed per system and compared with
  [`ci/fingerprints.json`](../ci/fingerprints.json) (`scripts/fingerprint.py`,
  the study's fingerprint of [`docker/renode/`](../docker/renode/README.md)).
  The firmware is pinned to the commits that file names, so a firmware branch
  moving never moves it: only the vHIL can.
- **The two-board determinism test**
  (`test_two_board_runs_give_the_same_bus_timeline`, invariant 11).

Both run in `sim.yml`'s `impact guard` job, which the required `sim` check
needs. The guard is what makes a narrow selection safe: whatever a change does
to any system's boot and first seconds shows up there even when no selected
test looks at it.

**When the fingerprint changes on purpose** (a model fix that changes what
the firmware sees, a Renode patch that changes timing), the guard fails with
the systems that moved. Download the `fingerprint` artifact of the failed run
and commit its `fingerprints.json` as `ci/fingerprints.json`, saying in the
commit why it moved (as `docker/renode/README.md` does for patch 0007). That
is a `ci/**` change, so the PR then runs everything. To move the pinned
firmware, edit the commits in the same file (and re-record).

## Required checks stay present

`dev` requires `unit`, `smoke (ecu)`, `smoke (ams)`, `sim`, `ecu` and
`scenarios`. Each heavy workflow first calls the reusable
[`select.yml`](../.github/workflows/select.yml) (`scripts/ci-select.py`),
and its jobs are gated on its outputs so that every required check always
reports:

- A job that is not selected is **skipped**, which GitHub counts as passing:
  `ecu`, `scenarios`. A failed selection does not skip them: they run and
  fail on their first step (`if: !cancelled() && (needs.select.result !=
  'success' || <selected>)`).
- The `smoke` legs always run (a skipped matrix job would report as `smoke`,
  not `smoke (ecu)`) and skip their steps when not selected.
- `sim` is an aggregate that always runs: it passes only when the selection
  succeeded and each of the shards and the guard succeeded when selected or
  was skipped when not.
- The shard matrix comes from the selection: 8 shards for everything, fewer
  for a small selection (as many as keep each lane within a full run's,
  `scripts/sim-balance.py --files`).

GitHub reports no check at all for a workflow whose expressions don't parse
(zero jobs, and `gh pr checks` shows the rest green): run
`actionlint` (`docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7
-shellcheck= .github/workflows/*.yml`) on any workflow change.

## The impact map: `ci/impact.yaml`

Paths to test groups. The format is documented at the top of
[`ci/impact.yaml`](../ci/impact.yaml); in short:

- `everything:` globs select everything: the emulator (`docker/renode/**`),
  the platform, the board and pin model, the CAN bootloader's entry, the
  schemas, the run path (`vhil/sim.py`, `system.py`, `worker.py`,
  `renode.py`, `broker.py`, `bench*.py`, `pytest_plugin.py` and the modules
  every session goes through), the models every run uses (the CAN path, the
  machines, the monitor, fast-forward, probes), and CI itself
  (`.github/workflows/**`, `ci/**`, the selection and sharding scripts).
- Every other path must match a rule, or it selects everything (fail safe:
  a new directory runs the whole CI until a rule names it). The rules it
  matches add up.
- A rule selects tests/sim files by **pytest marker** (`pytest.ini`: boards
  `ecu`/`ams`/`udv`, systems `system_<id>`, subsystems `can`, `sd`, `adc`,
  `isospi`, `plant`, ...), by path, or by **importers** (the test files that
  import the changed module); and **systems** (their tests, scenarios, smoke
  suite and IFS_HIL job), **runs** (the same without the tests/sim files),
  **boards**, **firmware** (catalogue entries -> boards -> systems),
  **devices** (catalogue models -> the systems that have one), or scenarios,
  smoke, IFS_HIL jobs, the speed benchmark, the editor check directly.
- `select: {}` maps a path to nothing heavy (docs; code only unit tests
  cover).

Examples (`full-ci`):

| Change | Runs |
|---|---|
| `docs/**` | guard only |
| `models/renode/Stm32H7Sdmmc.cs` | guard; tests/sim `sd` files; scenarios of ams, ecu-ams; smoke (ams); IFS_HIL ams, ecu-ams |
| `vhil/plant_ts.py` | guard; tests/sim `plant` files and importers; scenarios of ecu-ams |
| `catalog/firmware/udv.yaml` | guard; tests/sim `udv` files |
| `systems/ecu-ams.yaml` | guard; tests/sim `system_ecu_ams`; scenarios, IFS_HIL ecu-ams |
| `vhil/sim.py`, a workflow, an unmapped path | everything |

Every tests/sim file carries a module-level `pytestmark` with its boards, its
systems and at least one subsystem; `tests/unit/test_ci_select.py` checks
it, the board markers against the systems' roles, every rule's targets, and
the selection's fail-safes. Scenarios are grouped by system already
(`systems/<system>.scenarios/`); the scenario job runs the selected systems'
(`VHIL_SCENARIO_SYSTEMS`, `tests/scenarios/conftest.py`).

### Extending it

- **A new test file**: give it `pytestmark` (boards, systems, subsystem).
  Markers are strict (`--strict-markers`): a new one goes in `pytest.ini`.
- **A new model, module or directory**: add a rule naming the smallest group
  that catches a regression, or list it under `everything` if every run goes
  through it. Until then it selects everything.
- Check what a change selects:

  ```sh
  python scripts/ci-select.py --explain --changed models/renode/Stm32H7Sdmmc.cs
  python scripts/ci-select.py --explain --diff origin/dev HEAD     # this branch, as full-ci
  ```

  (in Docker: `scripts/vhil-docker.sh run python scripts/ci-select.py ...`).
  A `full-ci` run's `select` job prints the same table in its summary.

## The safety net: the nightly impact audit

[`impact-audit.yml`](../.github/workflows/impact-audit.yml) runs at 05:30
UTC, after the nightlies (`scripts/ci-audit.py`), and reports; it never
fails:

1. For each PR merged into `dev` since the previous audit: its label and the
   selection `full-ci` gives its merge commit's changes.
2. What failed in the nightlies: a tests/sim file (each shard's
   `results.tsv` in its `sim-logs-<k>` artifact), a scenario
   (`scenarios.xml`), else the job.
3. A nightly failure outside a `full-ci` PR's selection is a **selection
   gap**: a warning, a row in the summary, and a comment on the PR. For an
   unlabelled PR it is noted (it ran no heavy CI at all).
4. The impact map: the nightly sim shards run with `VHIL_IMPACT_MAP=1`, so
   each file records the systems its Sims booted (`tests/conftest.py`) and
   the `vhil/` Python it ran (coverage.py) into `sim-logs/impact/`. Through
   the systems come the firmware entries and the device models (catalogue
   entry and C# source) each test loaded. For every such path the audit asks
   whether a change to it alone would select that test, and lists the ones
   that would not: those tests rely on the guard for that path. Widen a rule
   when a listed pairing should be selected.

Platform models (ADC3, DMA, SPI, ...) are loaded by every board, so "loaded"
says little about what a test exercises; the map leaves them to the subsystem
markers and the guard.

## Measuring

Runner-minutes of a run: the sum of its jobs' durations,

```sh
gh api repos/isc-fs/IFS_vHIL/actions/runs/<id>/jobs --paginate \
  --jq '[.jobs[] | select(.started_at and .completed_at) |
         ((.completed_at|fromdate) - (.started_at|fromdate))] | add / 60'
```
