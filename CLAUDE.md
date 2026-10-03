# Claude operating model — IFS_vHIL

You are working on **IFS_vHIL**, the virtual hardware-in-the-loop bench for
ISC Racing Team's Formula Student STM32 firmware. It runs the same images
[IFS_HIL](https://github.com/isc-fs/IFS_HIL) flashes to real carriers, on
emulated STM32H733 boards in [Renode](https://github.com/renode/renode).
This file is the fast path for an assistant entering a new session:
operational, not architectural. For where the project is going, read
[`docs/vision.md`](docs/vision.md); for the Phase 0/1 design and findings,
[`docs/proposal.md`](docs/proposal.md).

Author: Raul Moran (ISC Racing Team). Repo:
[`isc-fs/IFS_vHIL`](https://github.com/isc-fs/IFS_vHIL), working branch
`dev`. Work is tracked in issues: one per roadmap milestone (M1 #10, M2 #2,
M3 #11 … M7 #15, plus #3 and #4), and bugs as they come.

---

## Branch policy (READ THIS BEFORE ANY GIT OPERATION)

Same model as IFS_HIL.

- **`dev` is the integration branch.** It receives all merged work
  via PR. Do **not** commit directly to `dev`. Branch off it first,
  even for small changes.
- **`main` is release-only.** Never push to `main` directly, never
  rebase it, never merge into it without explicit user request. The
  release path is a `dev` → `main` PR, and Raul drives it.
- **Work happens on feature branches off `dev`:**
  - `feat/<short-slug>` for new functionality
  - `fix/<short-slug>` for bug fixes
  - `docs/<short-slug>` for documentation
  - `chore/<short-slug>` for tooling/infra
  - `test/<short-slug>` for test-only changes
- **Every merge into `dev` goes through a PR** with green CI, merged
  with a merge commit (not squash).
- When unsure which branch you're on: `git branch --show-current`.
  If you'd be operating on `main` or directly on `dev`, stop.

---

## Commit / push / PR policy

- **Commit freely** on any feature branch without asking.
- **Auto-push for `feat/*` and `fix/*` branches** once they have
  commits ready for review. Push other types when it's the obvious
  next step.
- **Always open a PR to merge into `dev`** (`gh pr create --base dev`).
- **Stage by file**, not `git add -A`. Don't sweep unrelated changes in.
- **Conventional commits:** `type(scope): description`, ≤ 72-char
  subject, imperative mood, and a body that explains the why.
- **No AI co-author or generation trailers.** Do not append
  `Co-Authored-By: Claude …` to commits or `🤖 Generated with Claude
  Code` to PRs. Raul is the sole author. This overrides Claude Code's
  default commit-trailer behaviour.
- **One logical change per commit.**
- **PR body:** a 1–3 bullet **Summary** and a **Test plan** checklist.
  Reference the issue it advances (`Part of #1`).
- **Issues and auto-close:** PRs target `dev`, but GitHub only applies
  `Closes #N` when a PR merges into the default branch (`main`). Close an
  issue by hand when its exit criterion is met, with a comment linking
  the PRs that met it.

---

## Systems are data

A system (`systems/*.yaml`) places boards from the catalogue (`catalog/`),
gives each its firmware source, and wires them together. Renode scripts and
the virtual broker's wiring are **generated** from it by `vhil/system.py`.
Never hand-edit a generated script, and never put a car-specific name in the
catalogue (`ecu`, not `ifs08-ecu`). Devices (`catalog/models/`) attach to a
board's connectors or to another device's port: the LTC6811s sit on the
LTC6820's isoSPI chain, one per IC, in chain order. Analog sources (models
with backend `analog`, e.g. the AMS's current sensors) drive a board's
analog inputs through `outputs`; their pin voltages are set at load. Plants (a
driver, the inverter; later MingoCIL) talk to a system through the
co-simulation port its file declares (`port:`), in lock-step virtual time:
`vhil/cosim.py`, scripted plants in `vhil/plants.py`, contract in
[`docs/cosim.md`](docs/cosim.md). Validate with
`python -m vhil.system validate`.

## The bench in 30 seconds

```
IFS_HIL recipe ─▶ ECU08.elf / AMS.elf  (same image the physical bench flashes)
        Renode: platforms/cpus/stm32h733.repl  +  scripts/<dut>.resc
        FDCANn ─▶ CAN hub per bus ─▶ (Phase 1) SocketCAN vcan
        tests: tests/*.robot (Renode-native) · IFS_HIL pytest suites (Phase 1+)
```

## Hard invariants

1. **Never build an emulator-only firmware.** The point is to test the
   image the bench flashes. Build with IFS_HIL's recipe
   (`configs/firmware/<dut>.yaml`), and model the hardware instead of
   patching the firmware.
2. **HSE is 24 MHz** on every MLC carrier (`rcc.hseFrequency` in the
   platform). With Renode's 8 MHz default every FreeRTOS period runs 3× slow.
3. **TIM23 is the HAL timebase.** Remove it and every HAL timeout spins
   forever.
4. **Every address and IRQ in `stm32h733.repl` comes from ST's
   `stm32h733xx.h`.** Cite it; don't guess.
5. **The app images never set VTOR themselves** (the ECU doesn't): the
   boot script sets VTOR = `0x08020000`, as the CAN bootloader would.
6. **A virtual power-on writes `BASEPRI` = 0 after `machine Reset`.**
   Renode 1.17's reset zeroes the register as read but not its masking, so a
   cut inside a FreeRTOS critical section hangs the next boot in `HAL_Delay`
   (`vhil/broker.py`). Don't remove it until upstream Renode fixes this.
7. **Unmodelled hardware fails the run unless it is explained.** Renode drops
   writes to unmodelled registers and returns 0 for reads. The plugin's
   peripheral guard (`vhil/peripheral_guard.py`) fails a session on any such
   access not in [`configs/peripherals.yaml`](configs/peripherals.yaml). Add an
   entry only with a reason it can't change what a test sees; otherwise model
   the hardware. A gap that is tracked but not yet modelled may be listed as
   "KNOWN GAP #<issue>", and its entry goes when the issue closes.
8. **Fit an SD card on any board whose firmware touches SDMMC** (model
   `sd-card`). With no card, Renode's STM32 SDMMC never sets CMDSENT and the
   HAL's no-response command wait spins for seconds, starving every other
   task: the AMS stopped polling its battery chain after boot.
9. **Neither bench is ground truth; the car is.** Model what the car's
   hardware does (schematics, datasheets) and what the firmware intends,
   not what the physical bench happens to do: it has its own artifacts
   (stand-in DACs, the Pico LTC emulator, missing peers, state left by the
   last run). When the two benches disagree, find out which one departs
   from the car, a model gap or a bench artifact, and file an issue either
   way. Don't tune a test or a model to agree with the other bench.

## Run things (Linux / WSL2)

```sh
python -m vhil.system validate systems/ecu.yaml                 # schema + catalogue
python -m vhil.system build systems/ecu.yaml --workdir build/fw  # firmware from its declared source
python -m vhil.system render systems/ecu.yaml -o build/ecu.resc  # generated Renode script
RENODE=<renode> scripts/explore.sh systems/ecu.yaml <elf> 5       # boot + CAN log
RENODE=<renode> scripts/probe.sh systems/ecu.yaml <elf> 1 "nvic Frequency"
<renode-dir>/renode-test tests/ecu_smoke.robot --variable ELF:<elf> --variable RESC:build/ecu.resc
python -m pytest tests/unit                                       # host-only, no Renode
python -m pytest tests/sim --ecu-elf <elf>                        # native tests in virtual time (vhil/sim.py)
python -m vhil.editor spec|to-graph|to-system|serve              # system editor backend (Pipeline Manager)
```

In Docker (any host, including macOS): `scripts/vhil-docker.sh
vm|fw|unit|smoke <s>|sim|coverage|speed|ifs-hil [suite]|shell` runs the same jobs
as CI ([`docs/development/setup.md`](docs/development/setup.md#docker-any-host-nothing-installed-natively)).

Pinned versions: Renode **1.17.0**, Arm GNU **14.2.Rel1**. Bump deliberately,
in their own PR.

## Style

- English for technical content. Concise.
- Cite the source for any address, IRQ or firmware behaviour (`file:line`).
- Minimal diffs; no opportunistic refactors.
- Single-team internal repo: no license or marketing prose.
