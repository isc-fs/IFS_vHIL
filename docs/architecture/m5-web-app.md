# M5: the shared web app — architecture

Decision record for [#13](https://github.com/isc-fs/IFS_vHIL/issues/13).
Exit criterion: an engineer composes, runs and inspects a system from the
browser, and the system file they produced runs identically in CI.

## Constraints that decide most of it

- **The system file is the source of truth** (vision principle 1). The app
  edits files in a git repository; it never holds a system only in its
  database. A saved system is a commit on a branch, and sharing it is a PR.
- **Virtual time is the contract** (principle 2). Live views and history are
  indexed by virtual time, so a run's trace is the same whether the worker ran
  fast or slow, and a replay shows exactly what CI would see.
- **Workers are Linux** (vision §4): the `ifs-vhil` image CI already uses,
  with Renode, the firmware toolchain and vcan. A run in the app and a CI job
  are the same code path (`vhil.sim.Sim` / pytest), not a parallel one.
- **Backend-agnostic above the platform layer** (principle 5): the API speaks
  systems, boards, buses, frames and signals, not Renode or STM32.

## Decision

| Concern | Choice | Why |
|---|---|---|
| System editor | **Antmicro Pipeline Manager** (already integrated, `vhil/editor.py`, `docker/editor.Dockerfile`) | Proven both-ways translation to the system file; no second editor to maintain |
| App shell + views | Plain HTML + ES modules served by the API (no build step); uPlot (vendored) for signal plots | No frontend build chain in a firmware team's repo; the editor is embedded, the rest is tables and plots |
| API service | **FastAPI** (Python) | Same language as `vhil.*`: systems, catalogue, Sim and the generator are imported, not re-implemented |
| Job queue | A `runs` table in **SQLite** (WAL), workers claim rows atomically | One dependency fewer than Redis for a single-host team tool; the table *is* the run history. Swap for Postgres when there is more than one host |
| Workers | `python -m vhil.worker` in the `ifs-vhil` image, N replicas | Same image and code path as CI; vcan and pacing as in CI |
| Live views | WebSocket per run; the worker appends frames/samples to the run's trace file, the API tails it | Live and history are the same data: a finished run replays from the file it streamed |
| Traces | One JSON-lines file per run (frames, GPIO edges, signals, FSM/state samples), plus pytest JUnit, failure snapshots (#104) and coverage | Already what the sim produces; no new format |
| Auth | **GitHub OAuth** login restricted to the isc-fs org; a **GitHub App** installation token for firmware clones and for pushing system-file branches / opening PRs | Vision §4: no personal tokens. A dev mode with auth off for local use, only when asked for and on loopback ([`web-app.md`](../development/web-app.md)) |
| Deployment | `docker compose`: `api`, `worker` (×N), `editor`; on a host, behind Caddy with its own git clone and SQLite backups ([`docs/deploy.md`](../deploy.md)) | Runs on any Linux host or a Mac with Colima, like the rest of the repo. Workers need no privileges: runs use Renode's in-process CAN hubs, not vcan. Every service runs unprivileged (uid 10001, no capabilities, read-only root); workers have no secrets and reach only GitHub, through an egress proxy ([hardening](../deploy.md#hardening)) |

## Components

```
browser ── app shell (/)  ─┬─ /editor  → Pipeline Manager (iframe) ⇄ vhil.editor backend
                           ├─ /systems  list · open · save (git branch) · PR
                           ├─ /runs     start · live (WS) · history · inspect
                           │
                     FastAPI (vhil.server) ── SQLite (runs, users) ── workspace (git clone)
                                 │ claims                           └─ results/<run>/…
                           workers (vhil.worker, ifs-vhil image) ── Renode, vcan
```

## API contract (v1)

All JSON; times in microseconds of virtual time.

- `GET /api/health` → `{status, version}`
- `GET /api/catalog` → boards, backplanes, models, platforms, firmware (from `catalog/`)
- `GET /api/systems` → `[{id, path, ref}]` in the workspace
- `GET /api/systems/{id}` → `{id, yaml, doc, ref}`;
  `PUT /api/systems/{id}` `{yaml, message, branch}` → validates
  (`vhil.system validate`), commits on `branch`, returns `{ref}`
- `POST /api/systems/{id}/pr` `{branch, title}` → PR URL (GitHub App)
- `POST /api/runs` `{system, ref?, firmware: {board: ref?}, scenario}` →
  `{run_id}`. `ref` is any branch, tag or commit of the workspace (the
  editor's saved branches; `GET /api/workspace/refs` lists them); the run
  records its commit and runs `systems/<id>.yaml` as it is there, copied
  into the run's directory, so the shared checkout never moves. The rest
  (catalogue, models, platforms, code) is the worker's tree, so a ref that
  differs from the workspace's HEAD in any of those is refused rather than
  run differently from CI; a saved branch only ever changes a system file.
  A pytest scenario reads the checked-out tests and systems: HEAD only.
  Board `firmware_ref`s of the system as saved pick the images. A scenario is either
  `{"kind": "run", "virtual_ms": N, "stimuli": [...]}` (stimuli: CAN
  send / periodic, GPIO set, analog set at virtual times) or
  `{"kind": "pytest", "select": "tests/sim/test_x.py::test_y"}`.
  Limits ([`docs/deploy.md`](../deploy.md#limits)): a run over the
  server's virtual time, stimuli or watch limit is 422; with too many
  active (queued + running) runs, the user's or everyone's, 429
- `GET /api/tests` → `{root, files, tests, error?}`: the test files and
  node ids under `tests/sim` a pytest scenario can select (`pytest
  --collect-only -q`, cached until a file there changes)
- `GET /api/runs` (history, newest first) · `GET /api/runs/{id}` →
  `{id, state: queued|running|passed|failed|error, system, ref, created,
  started, finished, virtual_us, summary, worker, heartbeat, attempts, owner,
  can_cancel}` (`owner`: who started it; `can_cancel`: whether the requester
  may, as its owner or an admin).
  A worker beats `heartbeat` while it holds a run; a run whose worker died
  is reclaimed by the next worker's poll: back to `queued`, or `error` after
  its second attempt (`vhil/server/runs.py`)
- `GET /api/runs/{id}/trace?since_us=&kinds=&limit=&cursor=` → a page of
  trace records; the `X-Trace-Cursor` response header is the next page's
  `cursor` (opaque: a byte offset into the trace file), so a page costs
  what it returns;
  `WS /api/runs/{id}/live` → the same records as they are written, then
  `{"kind": "end", "state": …}`
- `GET /api/runs/{id}/artifacts/{name}` → JUnit, snapshots, coverage, logs,
  never rendered: `text/plain` or an attachment, `CSP: sandbox`
- `POST /api/runs/{id}/cancel` (owner or admin, else 403)

Trace record kinds: `frame {t_us, bus, id, ext, data, src?}` (`src:
"stimulus"` on the frames the scenario itself sent, stamped when the probe
sent them; counted in the summary's `sent`, not `frames`),
`edge {t_us, board, pin, level}`, `sample {t_us, board, name, value}`
(read_symbol / analog values the scenario asks to watch), `log {t_us, text}`.

## Delivery (sub-issues)

1. **Skeleton**: `vhil.server` (health, catalogue, systems read), compose file,
   dev-mode config, CI job running its unit tests.
2. **Runs**: queue table, `vhil.worker`, `run`/`pytest` scenarios, trace
   writer, live WebSocket, cancel.
3. **Inspect**: run history and run page: frame table with filters and
   decoding against the boards' `.def`/DBC, signal plots, snapshots,
   coverage, JUnit.
4. **Editor + git**: the shell embeds Pipeline Manager; save writes the system
   file to a branch in the workspace; firmware picker (repo/ref) from the
   boards' firmware sources; PR creation.
5. **Auth**: GitHub OAuth + org check; GitHub App tokens for firmware and
   pushes; dev mode.
6. **Deploy**: production compose, volumes, backups of the SQLite file,
   a host.

Each lands with tests; the exit criterion is checked end to end in the last.

## Open decisions (owner: Raul)

- **Host.** Where the app runs (a team server, a cloud VM). Needs Linux with
  Docker and vcan (or `linux-modules-extra`).
- **GitHub App.** Creating it in the isc-fs org needs an org admin; its
  permissions would be contents read (firmware repos), contents write + pull
  requests (this repo, for system-file branches/PRs), members read (login
  check). Until then, dev mode and a local deploy key cover development.
