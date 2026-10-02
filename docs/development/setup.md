# Development setup and workflow

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

1. Node.js ≥ 20.18 (the Node 22 LTS `linux-x64` tarball from nodejs.org,
   unpacked to `~/vhil-tools/node`; no sudo needed).
2. Pipeline Manager in its own venv (it pins old dependency versions):
   `git clone https://github.com/antmicro/kenning-pipeline-manager ~/vhil-tools/kenning-pipeline-manager`,
   `python3 -m venv ~/vhil-tools/pm-venv`, then in that venv
   `pip install -e ~/vhil-tools/kenning-pipeline-manager` and
   `pip install git+https://github.com/antmicro/kenning-pipeline-manager-backend-communication.git`,
   and `PATH=~/vhil-tools/node/bin:$PATH ./build server-app` in the checkout.
3. The same backend library in this repo's venv.
4. `scripts/editor.sh`, then open http://localhost:5000. Load a system with
   `python -m vhil.editor to-graph systems/ams.yaml -o ams.json` and drop the
   file on the canvas. Run needs `VHIL_<FIRMWARE>_ELF` (e.g. `VHIL_AMS_ELF`).

Pipeline Manager's `./validate <spec> <dataflow>` checks generated files
against its own schema: `python -m vhil.editor spec -o spec.json`.

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
- A test that passes virtually but fails on the physical bench is a
  **model gap**. Open an issue for it; don't skip the test to make it
  green.

## Pull requests

- **Summary** (1–3 bullets) and **Test plan** (`- [ ]` / `- [x]`).
- Small and thematic. Draft PRs are fine for blocked work.
- Green CI before merge; merge commit, not squash; delete the branch
  after merge.

## Release process

`dev` → `main` via a merge PR once a phase's exit criterion is met. Tag the
merge commit (`v0.1.0` = Phase 1, …).
