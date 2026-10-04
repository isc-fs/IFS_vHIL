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
scripts/vhil-docker.sh editor      # system editor on http://localhost:5050
scripts/vhil-docker.sh shell
```

On macOS it uses a Colima VM rather than Docker Desktop: Docker Desktop's
LinuxKit kernel has no vcan, so IFS_HIL's suites could not reach the buses.
Colima's Ubuntu image lacks `linux-modules-extra` for its shipped kernel, so
`vm` moves it to the current generic kernel once and restarts. On a Linux
host, load vcan and set `VHIL_DOCKER_CONTEXT=default`.

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

### Firmware coverage

`--vhil-coverage DIR` records which code of each image ran
([`vhil/coverage.py`](../../vhil/coverage.py)): Renode logs every block it
translates, and a block is translated the first time it runs. The cost is
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

Linux, or WSL2 on Windows. The Renode SocketCAN bridge used from Phase 1 is
Linux-only.

| Tool | Version | Where |
|---|---|---|
| Renode | 1.17.0 portable | `renode-1.17.0.linux-portable.tar.gz` from the [Renode releases](https://github.com/renode/renode/releases) |
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
(Apache-2.0) in server mode, talking to `python -m vhil.editor serve`.

In Docker: `scripts/vhil-docker.sh editor`, then open http://localhost:5050
(not 5000: macOS's AirPlay Receiver holds that port). The image
([`docker/editor.Dockerfile`](../../docker/editor.Dockerfile)) pins Pipeline
Manager to a release whose format matches `vhil/editor.py`'s
`FORMAT_VERSION`; Run uses the images from the last `vhil-docker.sh fw`.
"Load file" imports a system YAML through the backend; "Save file" writes
it back onto the original text, comments kept. Natively:

1. Node.js ≥ 20.18 (the Node 22 LTS `linux-x64` tarball from nodejs.org,
   unpacked to `~/vhil-tools/node`; no sudo needed).
2. Pipeline Manager in its own venv (it pins old dependency versions):
   `git clone https://github.com/antmicro/kenning-pipeline-manager ~/vhil-tools/kenning-pipeline-manager`,
   `python3 -m venv ~/vhil-tools/pm-venv`, then in that venv
   `pip install -e ~/vhil-tools/kenning-pipeline-manager` and
   `pip install git+https://github.com/antmicro/kenning-pipeline-manager-backend-communication.git`,
   and `PATH=~/vhil-tools/node/bin:$PATH ./build server-app` in the checkout.
3. The same backend library, and `ruamel.yaml==0.18.*`, in this repo's venv.
4. `scripts/editor.sh`, then open http://localhost:5000. Load a system with
   `python -m vhil.editor to-graph systems/ams.yaml -o ams.json` and drop the
   file on the canvas. Run needs `VHIL_<FIRMWARE>_ELF` (e.g. `VHIL_AMS_ELF`).

Pipeline Manager's `./validate <spec> <dataflow>` checks generated files
against its own schema: `python -m vhil.editor spec -o spec.json`.

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

## Release process

`dev` → `main` via a merge PR once a phase's exit criterion is met. Tag the
merge commit (`v0.1.0` = Phase 1, …).
