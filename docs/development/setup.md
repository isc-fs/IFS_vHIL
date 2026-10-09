# Development setup and workflow

## Docker (any host, nothing installed natively)

[`docker/Dockerfile`](../../docker/Dockerfile) holds everything below at the
pinned versions, for amd64 and arm64. [`scripts/vhil-docker.sh`](../../scripts/vhil-docker.sh)
runs each CI job in it; firmware and the IFS_HIL checkout live in the
`vhil-data` volume, results land in `results/`.

```sh
scripts/vhil-docker.sh vm          # macOS: Colima VM `vhil` with vcan (once; needs colima)
scripts/vhil-docker.sh fw          # build ECU + AMS from their systems
scripts/vhil-docker.sh unit
scripts/vhil-docker.sh smoke ecu   # or ams
scripts/vhil-docker.sh sim
scripts/vhil-docker.sh coverage   # firmware coverage of tests/sim (below)
scripts/vhil-docker.sh ifs-hil     # IFS_HIL's ECU smoke suite over vcan can0..can2
scripts/vhil-docker.sh editor      # system editor on http://localhost:8080/editor/
scripts/vhil-docker.sh shell
```

On macOS it uses a Colima VM rather than Docker Desktop: Docker Desktop's
LinuxKit kernel has no vcan, so IFS_HIL's suites could not reach the buses.
Colima's Ubuntu image lacks `linux-modules-extra` for its shipped kernel, so
`vm` moves it to the current generic kernel once and restarts. On a Linux
host, load vcan and set `VHIL_DOCKER_CONTEXT=default`. Only `ifs-hil` runs
its container `--privileged --network host` (it creates `can0..can2`); every
other job runs unprivileged on Docker's default network (`VHIL_DOCKER_VCAN=1`
gives `shell`/`run` vcan too), and `server`/`editor` publish on 127.0.0.1.

### Emulator monitor, card images and what a system file may say

These notes are for the current emulation backend, Renode 1.17. Its
`-P <port>` monitor listens on every interface with no
authentication, and has no option to bind one address. `vhil.sim` and
`vhil.bench` start Renode with `-P -1` instead and serve the monitor
themselves on `127.0.0.1` only ([`models/renode/VhilMonitor.cs`](../../models/renode/VhilMonitor.cs),
`vhil.renode.launch`). `scripts/explore.sh` and `probe.sh` use the console
monitor (`--console -P -1`). `renode-test`'s Robot server listens on every
interface too; `vhil-docker.sh smoke` runs it on the default Docker network
with no published port, so it is not reachable from the host.

An `sd-card` `image` is opened read-write, so it must lie inside a card-image
directory: `$VHIL_CARD_DIR` (`:`-separated; default `build/cards`). A relative
image is looked up in the first; `..`, absolute paths elsewhere and symlinks
out are refused. A test passes its own temp directory with
`Sim(..., card_dirs=[tmp])`.

Everything a system file or a run scenario names reaches the emulator only
through `vhil.renode`'s encoders, behind the schema and `System`'s checks: names are
identifiers, device params have their model's type (a string param its
`param_formats` format, else a plain word), and a value that could end a
string, a line or a command is refused.

### When a native test fails

A failing test in `tests/sim` gets a **snapshot** of every Sim it touched,
per board, in the pytest report (section `vhil snapshot`): virtual time, PC,
LR and SP with their symbols, the core registers, the active exception and
SCB fault registers, and the FreeRTOS view (running task, every task's state,
priority and stack high-water mark), read from RAM through the ELF's symbols
([`vhil/snapshot.py`](../../vhil/snapshot.py)). With `--sim-log-dir` (CI's
`sim-logs` artifact) the full snapshot goes to
`<sim-log-dir>/failures/<test>/<n>-<system>-<board>.txt` and the report keeps a
few lines. It costs nothing until a test fails, and it only reads.

To see how the firmware got there, re-run the test with `--vhil-trace`: each
CPU keeps its last 4096 translation blocks (`--vhil-trace 20000` for more),
and the snapshot adds them, symbolised, and collapsed into a call-ish trace
(`[NRF24_BitBangTransfer -> HAL_GPIO_WritePin -> ...] x 3`). The hook runs
on every block, so the emulation is 5-8x slower (`test_ecu_boot.py`: 17 s
to 89 s); it is opt-in for that reason.

```sh
scripts/vhil-docker.sh sim -k heartbeat_only --vhil-trace     # results/sim-logs/failures/
```

### CPU fault injection

A native test can make the CPU fail where the firmware's own fault paths
start, without patching the image (#196, `vhil/sim.py`). Each call arms a
one-shot execution hook that changes a register when the CPU reaches a
function; its counter outlives resets, so the next boot runs clean.

| Call | What the firmware sees |
|---|---|
| `sim.fail_malloc(board, call=1)` | its `call`-th `pvPortMalloc` from now asks for more than the heap holds: heap_4 returns NULL and calls `vApplicationMallocFailedHook` |
| `sim.bus_fault_at(board, function, register=0, call=1)` | the pointer in `r<register>` at `function`'s entry points at the platform's reserved range (`BUS_ERROR_ADDRESS`): its first load is a precise BusFault (`models/renode/VhilBusError.cs`) |
| `sim.return_to(board, function, address, call=1)` | `function`'s return address (LR) at entry becomes `address`, as a stack overwrite gives: from `XN_ADDRESS` (peripheral space) its return is a MemManage with CFSR.IACCVIOL (`models/renode/VhilExecuteNever.cs`) |
| `sim.fault_status(board)` | SHCSR, CFSR, HFSR, MMFAR and BFAR |
| `sim.function_at(board)` | the function the PC is in, e.g. `HardFault_Handler` |

The CPU model routes a fault as ARMv7-M does: with SHCSR's
MEMFAULTENA/BUSFAULTENA/USGFAULTENA clear (as the AMS and ECU leave them) a
MemManage, BusFault or UsageFault escalates to HardFault with HFSR.FORCED;
set, each takes its own handler. Renode models the PMSAv7 MPU, so a test can
program a region to get a MemManage; with the MPU off, an instruction fetch
from an XN region of the default memory map (a jump into peripheral space) is
a MemManage with IACCVIOL too (#239). Not modelled: a fetch from a reserved
range, a BusFault (IBUSERR) on the chip, aborts the Renode machine. Any
machine abort fails the run at once: the monitor command waiting on it raises
`vhil.renode.MachineAborted` (`models/renode/VhilMonitor.cs`), where a
`RunFor` would otherwise never return.

### Firmware coverage

`--vhil-coverage DIR` records which code of each image ran
([`vhil/coverage.py`](../../vhil/coverage.py)): the emulator (Renode) logs
every block it translates, and a block is translated the first time it runs. The cost is
5% on a plain ECU boot and about 25% over a mix of suites with power cycles
(a reset flushes the translation cache, so the code is logged again), which
is why CI doesn't turn it on. At the end of the session DIR holds, per image, `<image>.info`
(lcov, files with DWARF line info), `<image>.functions.tsv` (every function,
hit or not) and `summary.txt` / `summary.md` (functions and lines hit per
source file). Hits are 0/1, not counts; an image without line info (the CAN
bootloader's Release build) gets function coverage only.

```sh
scripts/vhil-docker.sh coverage 'test_ecu_*'                  # results/coverage/
python -m vhil.coverage cov-ams/ cov-ecu/ -o cov/            # merge runs (their raw/ logs)
genhtml results/coverage/ECU08.info -o results/coverage/html  # lcov's HTML, if installed
```

## Environment

Linux, or WSL2 on Windows. The SocketCAN bridge used from Phase 1 (Renode's,
for the current backend) is Linux-only. The table pins the current emulation
backend and toolchain.

| Tool | Version | Where |
|---|---|---|
| Renode (emulation backend) | 1.17.0 portable | `renode-1.17.0.linux-portable.tar.gz` from the [Renode releases](https://github.com/renode/renode/releases) |
| Arm GNU Toolchain | 14.2.Rel1 | developer.arm.com (pinned by IFS_HIL's recipes) |
| Python | ≥ 3.10 | venv with `robotframework==6.1 robotframework-retryfailed==0.2.0 psutil==5.9.4 "pyyaml==6.0.*" "telnetlib3==2.0.*"` (Renode 1.17's own `tests/requirements.txt`) |
| CMake | ≥ 3.22 | distro package |

### vcan on WSL2

Microsoft's WSL2 kernel has the CAN core but not `vcan.ko`.
[`scripts/wsl-vcan.sh`](../../scripts/wsl-vcan.sh) builds it from Microsoft's
source for exactly the running kernel (the first run takes ~10–15 min). With
`--load`, it also loads the module and creates `can0`–`can2`, asking for your
sudo password. Build dependencies: `sudo apt-get install -y build-essential
flex bison bc libelf-dev libssl-dev dwarves`. Re-run it after `wsl --update` changes
`uname -r`, and after every WSL restart with `--load`.

CI ([`.github/workflows/smoke.yml`](../../.github/workflows/smoke.yml))
installs exactly these on `ubuntu-latest`. It is the reference setup.

### System editor (optional)

The editor UI is Antmicro's [Pipeline Manager](https://github.com/antmicro/kenning-pipeline-manager)
(Apache-2.0) in server mode, talking to `python -m vhil.editor serve`. It is
vendored in [`editor/pipeline-manager/`](../../editor/pipeline-manager/README-VHIL.md)
(upstream v0.5.2), with our changes made in place and listed in its
`CHANGELOG-VHIL.md`.

In Docker: `scripts/vhil-docker.sh editor`, then open
http://localhost:8080/editor/ (`/` redirects there, and so do old
`#/editor/<system>`, `#/systems[/<id>]`, `#/runs[/<id>[/<tab>]]` and
`#/classic/…` links; `?system=<id>&branch=<b>` opens a system, `?run=<id>`
replays a run, `&tab=<dock tab>` on it; the shell's own pages are retired).
It runs `docker/compose.yaml`'s `editor` and `proxy` (and the `api`, for the
login check): one origin, as on a host, the editor under `/editor/` and no
port of its own. The image
([`docker/editor.Dockerfile`](../../docker/editor.Dockerfile)) builds it
from that directory (the frontend's dependencies from its lockfile, `npm
ci`); its release's format matches `vhil/editor.py`'s `FORMAT_VERSION`. Run
uses the images from the last `vhil-docker.sh fw`.
"Load file" imports a system YAML through the backend; "Save file" writes
it back onto the original text, comments kept. Natively:

1. Node.js ≥ 20.18 (the Node 22 LTS `linux-x64` tarball from nodejs.org,
   unpacked to `~/vhil-tools/node`; no sudo needed).
2. Pipeline Manager in its own venv (it pins old dependency versions):
   `python3 -m venv ~/vhil-tools/pm-venv`, then in that venv
   `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PIPELINE_MANAGER=0.5.2 PIPELINE_MANAGER_SKIP_FRONTEND_BUILD=1 pip install -e editor/pipeline-manager`
   and `pip install git+https://github.com/antmicro/kenning-pipeline-manager-backend-communication.git`;
   then `npm ci` in `editor/pipeline-manager/pipeline_manager/frontend`, the
   shell's tokens, fonts and run request copied in (the image build does the same:
   `mkdir -p editor/pipeline-manager/pipeline_manager/frontend/src/vhil/shell && cp -r vhil/server/static/tokens.css vhil/server/static/editor-run.js vhil/server/static/fonts editor/pipeline-manager/pipeline_manager/frontend/src/vhil/shell/`,
   git ignores the copy; redo it when they change), and
   `PATH=~/vhil-tools/node/bin:$PATH ./build server-app --skip-install-deps`
   in `editor/pipeline-manager`, and `export PM_DIR=$PWD/editor/pipeline-manager`.
3. The same backend library, and `ruamel.yaml==0.18.*`, in this repo's venv.
4. `scripts/editor.sh`, then open http://localhost:5000. Load a system with
   `python -m vhil.editor to-graph systems/ams.yaml -o ams.json` and drop the
   file on the canvas. The workspace's Systems, Run, Commit and PR use the
   web app's API on the same origin, under `/editor/`, which only the proxy
   provides (`deploy/Caddyfile`; `docker/compose.yaml` runs it): for those,
   use Docker. `PM_CSP_REPORT_ONLY=1` sends the editor's CSP as Report-Only
   (violations in the browser console, nothing blocked).

Changes to Pipeline Manager are commits to `editor/pipeline-manager/`, each
listed in its `CHANGELOG-VHIL.md`. The first, bus-per-instance, fixes graphs
with more than one CAN bus: v0.5.2 shares one `bus` object between every node
of a type, so on load each bus takes the stubs of the last and the
connections to the others dangle ("Missing dst s:can_inv:0").

`python -m vhil.editor check [systems...]` runs Pipeline Manager's
`./validate <spec> <dataflow>...` on the specification and each system's
dataflow (default: `systems/*.yaml`). It loads them through the frontend's
own code, so it catches what a schema check misses. In Docker:
`scripts/vhil-docker.sh editor-check`; `tests/unit` runs it too when
`$PM_DIR` points at a checkout (the editor image).

**The workspace** (`/editor/`, M5.4; laid out as in step 7 of the
[workspace plan](../architecture/editor-workspace.md)): a 40 px top bar
(the system and branch with a dot for unsaved edits, a firmware chip per
board, the run's duration, Run (F5) and Stop (Shift+F5), the mode, Commit…
and Open PR, the theme), an activity rail and sidebar (Ctrl+B: Palette,
Systems, Runs), the canvas, an inspector (the selected node's properties;
a board's role and firmware refs live there, not on its node), a bottom dock
(Ctrl+J: Log and Problems, more tabs to come) and a status strip; `?` lists
the shortcuts. Systems opens a system onto the canvas (from the checked-out
tree or a branch) or starts a new one. Commit writes `systems/<id>.yaml` on
the branch you name, in the API's workspace, with git plumbing: no checkout
moves, `dev`/`main` are never written, a branch checked out in the workspace
is refused (`vhil/server/gitstore.py`). The file is validated first as
`python -m vhil.system validate` would. A MainLite node has a `role` select
(ECU, AMS or uDV), each choice shown with the node id it gives the bootloader
(`ecu (node 0x1)`); changing it relabels the node's pins live (the backend
answers Pipeline Manager's `properties_on_change`), saving writes `role: ecu`,
and the bootloader it carries is a read-only `bootloader` property. A board's firmware chip (or its
ref in the inspector) opens a picker that sets its
`firmware_ref` (a branch or tag of the catalogue repo, listed with `git
ls-remote`). Open PR pushes the branch and opens a PR to `dev`; it needs
`VHIL_GITHUB_TOKEN` (or `VHIL_GITHUB_TOKEN_FILE`; contents + pull requests write) on the API until the
GitHub App (M5.5) replaces it. Without it the button is off and the branch
stays local. Check lists the file's errors and warnings in Problems; a click
selects the node one is about. Run starts a normal run
(`POST /api/runs`, `vhil/server/static/editor-run.js`): the system as
saved, at the commit it was opened from or last saved to (the checked-out
tree if it was opened from there), with each board's `firmware_ref` /
`bootloader_ref` from the graph, for the virtual ms given (default 3000:
each MainLite spends its bootloader's 2 s window first). With unsaved edits
it refuses and says to commit first; it never runs the graph in the browser.
Its state goes to the Log, the status strip and a notification, with a link
to its replay; the Runs view lists the system's runs. Tests… in the top bar
runs the native tests (`tests/sim`, a pytest run) on the open system; its
JUnit results and output open in the Artifacts tab. Known gap: Pipeline Manager 0.5.2 rejects the graphs of the
systems with more than one CAN bus ("Missing dst s:can_inv:0": the earlier
buses' stubs are not found), so the ECU systems don't load in the editor yet;
single-bus systems such as `ams` do.

### Web app (M5)

`scripts/vhil-docker.sh server` serves the shared web app on
http://localhost:8080 with no login (`VHIL_AUTH=dev`). GitHub login, the
GitHub App and their environment: [`web-app.md`](web-app.md).

## Branching

`main` is the release branch. Feature work lands on `dev` first, and `dev`
merges to `main` when a release is cut. Same model as IFS_HIL.

Branch names are `<type>/<kebab-slug>`: `feat/`, `fix/`, `docs/`, `chore/`,
`test/`. Always branch **from `dev`**; PRs target `dev`.

## Commits

- One commit per concern.
- First line ≤ 72 chars, imperative mood, area prefix:
  `feat(platform): …`, `fix(ecu): …`, `docs: …`.
- Body wrapped at 72, explaining the *why*.
- No AI co-author lines.

## Issues

Work is planned and tracked in issues. Each roadmap milestone in
[`vision.md`](../vision.md#5-roadmap) has one issue with a checklist and an
exit criterion. Bugs and model gaps get their own issue, labelled
`bug` or `enhancement`.

- A PR says which issue it advances: `Part of #1`.
- PRs merge into `dev`, so GitHub's `Closes #N` does not fire. Close the
  issue by hand once its exit criterion is met, linking the PRs.
- When the virtual and physical benches disagree, neither is assumed
  right: check both against the car's hardware and the firmware's intent.
  Open an issue for the side that departs from the car (a model gap or a
  bench artifact); don't skip the test to make it green.

## Pull requests

- **Summary** (1–3 bullets) and **Test plan** (`- [ ]` / `- [x]`).
- Small and thematic. Draft PRs are fine for blocked work.
- Green CI before merge; merge commit, not squash; delete the branch
  after merge.
- **CI runs the heavy jobs only in special cases.** These are the sim shards,
  smoke and the IFS_HIL ECU/ECU+AMS/AMS suites, and they run when:
  - the PR is labelled `full-ci`;
  - the PR is a release PR into `main`;
  - the nightly run on `dev` fires;
  - someone dispatches a workflow by hand.

  Every PR runs `unit`, a minute or so, which also validates every system.
  Otherwise the heavy jobs are skipped, which counts as passing for the
  required checks. Label a PR `full-ci` when it can change what the emulated
  firmware does: models, platforms, the catalogue, systems, `vhil/`'s
  simulation code, tests or the workflows. Docs, web app and editor-only
  changes don't need it.

## Release process

`dev` → `main` via a merge PR once a phase's exit criterion is met. Tag the
merge commit (`v0.1.0` = Phase 1, …).
