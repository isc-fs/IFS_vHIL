# Claude operating model — IFS_vHIL

You are working on **IFS_vHIL**, the virtual hardware-in-the-loop bench for
ISC Racing Team's Formula Student firmware. It runs emulated ISC boards on the
same images [IFS_HIL](https://github.com/isc-fs/IFS_HIL) flashes to the real
ones, so HIL suites can run in CI without physical hardware. This file is the
fast path for an assistant entering a new session: operational, not
architectural. For where the project is going, read
[`docs/vision.md`](docs/vision.md); for the Phase 0/1 design and findings,
[`docs/proposal.md`](docs/proposal.md).

Author: Raul Moran (ISC Racing Team). Repo:
[`isc-fs/IFS_vHIL`](https://github.com/isc-fs/IFS_vHIL), working branch
`dev`. Work is tracked in issues: one per roadmap milestone (M1 #10, M2 #2,
M3 #11 … M7 #15, plus #3 and #4), and bugs as they come.

**Current backend and platforms.** More MCUs, SoCs and emulation backends
are planned; today there is one of each:

| Layer | Current | Where |
|---|---|---|
| Emulation backend | [Renode](https://github.com/renode/renode) 1.17.0 | `.repl` platforms, generated `.resc` scripts, C# models in `models/renode/` |
| Platform | `stm32h733` | [`platforms/cpus/stm32h733.repl`](platforms/cpus/stm32h733.repl) |
| Board | MainLite (`role:` ecu, ams or udv) | [`catalog/boards/mainlite.yaml`](catalog/boards/mainlite.yaml) |

Where this file names Renode, the STM32H733 or the MainLite, the rule is
specific to that backend, platform or board.

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
builds each one's firmware (at the catalogue's ref, or the board's
`firmware_ref` / `bootloader_ref`), and wires them together. Every MainLite
carries the CAN bootloader and is placed in a `role:` (ecu, ams or udv), whose
node ID, flash bus and firmware come from the board's `roles` table
(`catalog/boards/mainlite.yaml`); such a board names no `firmware:`, and its
role's pin labels and routing are display and `validate` warnings only, never
endpoint aliases. Emulator scripts (Renode `.resc` today) and
the virtual broker's wiring are **generated** from it by `vhil/system.py`.
Never hand-edit a generated script, and never put a car-specific name in the
catalogue (`ecu`, not `ifs08-ecu`). Endpoints name the MainLite's own
connectors and pins (`ams.PB9`, `ecu.FDCAN2`); which car signal each carries
on its backplane is traced in [`docs/backplanes/`](docs/backplanes/), cited in
a trailing comment on the endpoint and copied into the roles table as a
label. Devices (`catalog/models/`) attach to a
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
        emulator (Renode): platforms/cpus/stm32h733.repl  +  scripts/<dut>.resc
        FDCANn ─▶ CAN hub per bus ─▶ (Phase 1) SocketCAN vcan
        tests: tests/*.robot (Renode-native) · IFS_HIL pytest suites (Phase 1+)
```

## Hard invariants

All of these are hard rules. The general ones hold for every board, platform
and backend; the platform- and backend-specific ones hold wherever that
platform or backend is used. The numbers are stable IDs (code and docs cite
them, e.g. "invariant 8"), so they are not in order.

### General

- (1) **Never build an emulator-only firmware.** The point is to test the
  image the bench flashes. Build with IFS_HIL's recipe
  (`configs/firmware/<dut>.yaml`), and model the hardware instead of
  patching the firmware.
- (10) **Every address and IRQ in a platform comes from the vendor's device
  header.** Cite it; don't guess.
- (7) **Unmodelled hardware fails the run unless it is explained.** The emulator
  drops writes to unmodelled registers and returns 0 for reads. The plugin's
  peripheral guard (`vhil/peripheral_guard.py`) fails a session on any such
  access not in [`configs/peripherals.yaml`](configs/peripherals.yaml). Add an
  entry only with a reason it can't change what a test sees; otherwise model
  the hardware. A gap that is tracked but not yet modelled may be listed as
  "KNOWN GAP #<issue>", and its entry goes when the issue closes.
- (9) **Neither bench is ground truth; the car is.** Model what the car's
  hardware does (schematics, datasheets) and what the firmware intends,
  not what the physical bench happens to do: it has its own artifacts
  (stand-in DACs, the Pico LTC emulator, missing peers, state left by the
  last run). When the two benches disagree, find out which one departs
  from the car, a model gap or a bench artifact, and file an issue either
  way. Don't tune a test or a model to agree with the other bench.

### MainLite board / `stm32h733` platform

- (2) **HSE is 24 MHz** on the MainLite (`rcc.hseFrequency` in the
  platform). With Renode's 8 MHz default every FreeRTOS period runs 3× slow.
- (3) **TIM23 is the HAL timebase.** Remove it and every HAL timeout spins
  forever.
- (4) **Every address and IRQ in `stm32h733.repl` comes from ST's
  `stm32h733xx.h`.** Cite it; don't guess.
- (5) **Every MainLite boots through its CAN bootloader**, as on the car: the
  generated script loads the provisioned flash (bootloader in sector 0, app
  at `0x08020000`, metadata, node-ID seed; `vhil/flash_image.py`) and the
  bootloader sets VTOR when it jumps to the app (the ECU never sets it
  itself). Never shortcut it: a power-on spends the bootloader's 2 s
  auto-jump window first, and tests wait for the app with
  `Sim.wait_for_app()` instead of assuming it starts at t = 0.
- (8) **SDMMC1 times out commands no card answers** (`Stm32H7Sdmmc.cs`, after
  RM0468 60.5.4): CMDSENT without a response, CTIMEOUT with one, DTIMEOUT
  for a read's data. Renode's own STM32 SDMMC never set CMDSENT with no card,
  and the HAL's no-response command wait spun for seconds, starving every
  other task (the AMS dropped AMS_OK). Keep that behaviour; a board may now go
  without an `sd-card`, and `dead: true` / `Respond false` gives a dead one.

### Renode backend

- (6) **Every reset writes `BASEPRI` = 0.** Renode 1.17's reset zeroes the
  register as read but not its masking (renode/renode#1021), so a reset
  taken with BASEPRI raised hangs the next boot: a power cut inside a
  FreeRTOS critical section, or the IWDG firing while the AMS spins in its
  stack-overflow hook (called from PendSV with BASEPRI raised). The stale
  mask survives into the bootloader, which never writes BASEPRI: its HAL
  tick never runs and it never auto-jumps. The platform's `renode.reset`
  commands (`catalog/platforms/stm32h733.yaml`) run in each board's reset
  macro, which Renode runs on every reset, the CPU's own included; the
  broker's power-on writes it too (`vhil/broker.py`). Don't remove either
  until upstream Renode fixes this.

## Run things (Linux / WSL2)

The emulator commands below are the Renode backend's.

```sh
python -m vhil.system validate systems/ecu.yaml                 # schema + catalogue
python -m vhil.system build systems/ecu.yaml --workdir build/fw  # firmware from its declared source
python -m vhil.system render systems/ecu.yaml --firmware ecu=<elf> --firmware ecu.bootloader=<bl-elf> \
    -o build/ecu.resc                                             # generated script + flash image
export VHIL_CAN_BOOTLOADER_ELF=<bl-elf>                            # every MainLite boots through it
RENODE=<renode> scripts/explore.sh systems/ecu.yaml <elf> 5       # boot + CAN log
RENODE=<renode> scripts/probe.sh systems/ecu.yaml <elf> 3 "nvic Frequency"
<renode-dir>/renode-test tests/ecu_smoke.robot --variable ELF:<elf> --variable RESC:build/ecu.resc
python -m pytest tests/unit                                       # host-only, no Renode
python -m pytest tests/sim --ecu-elf <elf> --can-bootloader-elf <bl-elf>  # native tests (vhil/sim.py)
python -m vhil.editor spec|to-graph|to-system|serve              # system editor backend (Pipeline Manager)
python -m vhil.editor check                                       # Pipeline Manager loads every system (editor image)
```

In Docker (any host, including macOS): `scripts/vhil-docker.sh
vm|fw|unit|smoke <s>|sim|coverage|speed|ifs-hil [suite]|shell` runs the same jobs
as CI ([`docs/development/setup.md`](docs/development/setup.md#docker-any-host-nothing-installed-natively)).

Pinned versions: Renode **1.17.0** (the current backend), Arm GNU
**14.2.Rel1**. Bump deliberately, in their own PR.

## Style

- English for technical content. Concise.
- Cite the source for any address, IRQ or firmware behaviour (`file:line`).
- Minimal diffs; no opportunistic refactors.
- Single-team internal repo: no license or marketing prose.
