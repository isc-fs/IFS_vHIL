# Handover — 2026-10-02

Where IFS_vHIL stands, so work can resume on another computer (or in a new
Claude Code session). Lives on the `docs/handover` branch only; delete the
branch once resumed. For the repo rules read [`CLAUDE.md`](../CLAUDE.md), for
direction [`docs/vision.md`](vision.md), for the test plan
[`docs/analysis/ifs-hil-tests.md`](analysis/ifs-hil-tests.md).

## Resume prompt

Paste into a new Claude Code session opened in an IFS_vHIL checkout:

> Read `docs/HANDOVER.md` on the `docs/handover` branch of isc-fs/IFS_vHIL,
> then `CLAUDE.md`. Set up the environment it describes if missing, check the
> state of PR #55 and dev's CI, and continue with the next steps it lists.
> Ask me before merging anything.

## State on 2026-10-02

dev = `fda6dc0` (after #54). Branch protection on `dev`: required checks
`unit`, `smoke (ecu)`, `smoke (ams)`, `sim`, `ecu`; branch must be up to
date; admins included; no force push. No review required.

Merged today, in order:

| PR | What |
|---|---|
| #51 | M4 test API: `vhil/sim.py`, `models/renode/VhilProbe.cs` (CAN probe per bus, GPIO probe per board, virtual timestamps), `tests/sim/`; platform at 528 MIPS |
| #52 | Generator: `$elf_<board>` set after `mach create` (multi-board systems failed) |
| #18 | M2: LTC6820/LTC6811 isoSPI chain (`models/renode/IsoSpi.cs`), SD card model, `tests/ams_smoke.robot` |
| #53 | `models/renode/VhilPacer.cs`: wall-clock pacing without Renode's catch-up sprints (bench only) |
| #54 | `models/renode/Stm32H7Adc3.cs`: H72x/H73x ADC3 (single/continuous, differential INN3=PF8, calibration); AMS current and ECU pedal native tests |

Closed issues today: #2 (M2), #10 (M1), #12 (M4), #19 (ADC3).

Open PR: **#55 (draft)** `feat/editor-spike` — system editor backend for
Antmicro's Pipeline Manager (`vhil/editor.py`, `scripts/editor.sh`,
`tests/unit/test_editor.py`). Tried in the real UI: palette from the
catalogue, AMS graph renders, Validate OK, Run writes
`can_acu: 222 frames …` to the editor terminal.

Open issues: M3 #11, M5 #13, M6 #14, M7 #15, depth #4, enablers #21–#24
(SDMMC IDMA, FDCAN fault hooks, RCC reset flags/backup domain, LTC open wire),
native suites #25–#50 (#34 ams-current and #40 ecu-boot have first tests).

## Next steps

1. **#55 rough edges**, then mark ready:
   - a connection to the CAN bus node's BUS interface draws back to the
     board header; check how Pipeline Manager expects bus connections
     (interface `bus` field / sides) and fix `specification()`/`to_dataflow`;
   - YAML import from the UI (`dataflow_import`) not yet exercised: the
     "import via backend" file input did not fire from a scripted change event;
   - written system files lose YAML comments: apply edits onto the original
     file with a comment-preserving YAML library before M5.
2. **M3 #11**: ECU and AMS on one ACU bus. `systems/` needs a two-board
   system; exit criterion is IFS_HIL's `test_block_f_real_ams.py` passing
   against the emulated AMS (remove its entry in `configs/gaps.yaml`).
   The editor can already compose it (see `test_an_edit_in_the_graph_is_a_valid_system`).
3. Enablers in the analysis' order: #21 SDMMC IDMA, #22 FDCAN faults, #23 reset
   flags, #24 open wire, bootloader #4, plants #14; then the native suites.

## Findings worth keeping

- **Speed.** The firmwares never sleep (no WFI in the idle task), so host cost
  scales with core MIPS. Platform = 528 MIPS (faithful; busy-waits time
  correctly). The wall-clock bench (`vhil/bench.py`, `WALL_CLOCK_MIPS=100`)
  lowers it so IFS_HIL's suites keep real time. CI runner: 1.16x real time at
  100 MIPS, 0.60x at 528 (`scripts/speed.py`, printed in the `sim` job summary).
  Boards run on parallel host threads; the slowest sets the pace (ECU+AMS
  together ≈ 0.76x at 100 MIPS, 0.5 ms quantum). A WFI idle hook in the
  firmware would be the big win — a firmware decision, not ours.
- **Renode quirks.** NVIC.Reset keeps BASEPRI (renode/renode#1021, broker writes
  0); pacing repays deficits (hence VhilPacer); a repl can't redeclare an
  inherited peripheral (ADC3 is `adc3: @ none` + `adc3_h73x`); variables set
  while a machine is selected are machine-local; `CANTester.SendFrame` starts
  a paused emulation (probe `Send` schedules instead); peripherals `@ none`
  aren't reachable from the monitor (probes are emulation externals).
- **AMS** without an SD card spins in `SDMMC_GetCmdError` (0.03–0.15x real time);
  `systems/ams.yaml` fits one. AMS still latches Error later on `VcuStale`
  (no VCU on its bus) until M3.
- **Pipeline Manager** (Apache-2.0): spec/dataflow format `20250623.14`;
  replies must match `pipeline_manager/resources/api_specification/` exactly
  (`frontend_on_connect` → `{}`, `app_capabilities_get` → bare list,
  `dataflow_export` → base64 for non-JSON). npm reports 68 audit warnings in
  its frontend deps: review before hosting. Antmicro's trace viewer
  (Zephelin) is Zephyr/flamegraph-oriented — not for CAN/GPIO inspection;
  Perfetto UI on exported Chrome-trace JSON is the cheap option there.

## Environment (WSL2 Ubuntu 22.04 on the old machine)

Tools in `~/vhil-tools`:
- `renode_1.17.0-portable/` (`RENODE` env var or the default path in `vhil/bench.py`)
- `arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/`
- `node` → Node 22.23.3
- `kenning-pipeline-manager/` (built `server-app`) + `pm-venv/`
- `wsl-kernel/` (vcan build)

Working files in `~/vhil`:
- `venv/` (PyYAML 6.0.3, jsonschema 4.26, pytest, robotframework 6.1,
  robotframework-retryfailed 0.2.0, psutil 5.9.4, telnetlib3 2.0.8, and
  pipeline_manager_backend_communication from git)
- `IFS_HIL/` checkout (for `scripts/run-ifs-hil.sh`)
- `ECU08.elf` and `m2fw/ams@main/build/AMS.elf`. Rebuild both with
  `python -m vhil.system build systems/<ecu|ams>.yaml --workdir ~/vhil/fw`.

Setup from scratch: [`docs/development/setup.md`](development/setup.md)
(Renode, toolchain, venv, vcan via `scripts/wsl-vcan.sh` — needs sudo, which
only you can type — and, on #55's branch, the system editor).

Everyday commands:

```sh
python -m pytest tests --ecu-elf ~/vhil/ECU08.elf --ams-elf ~/vhil/m2fw/ams@main/build/AMS.elf
scripts/run-ifs-hil.sh ~/vhil/IFS_HIL ~/vhil/ECU08.elf smoke          # needs can0..2
~/vhil-tools/wsl-vcan.sh --load                                       # after a WSL restart
scripts/editor.sh                                                     # #55: editor on :5000
```

## Working agreements

- Branch off `dev`, PR into `dev`, merge commits; never push to `dev`/`main`.
- No AI co-author or "Generated with" trailers (CLAUDE.md overrides the default).
- Merge only when Raul says so; the Claude Code auto-mode classifier also
  blocks `gh pr merge` unless the request is explicit.
- Downloads and installs need Raul's OK first.
- Don't port IFS_HIL tests: write native ones (vision, analysis doc).
- Close issues by hand with a comment linking the PRs that met the exit criterion.
- From Windows Git Bash into WSL: `MSYS_NO_PATHCONV=1 wsl -d Ubuntu-22.04 -- bash script.sh`;
  put anything with `$` in a script file. Don't wrap Renode runs in `timeout`
  from that shell (job control stops them).
