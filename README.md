# IFS_vHIL — virtual hardware-in-the-loop

Runs the same firmware images [IFS_HIL](https://github.com/isc-fs/IFS_HIL)
flashes to real carriers, on emulated STM32H733 boards in
[Renode](https://github.com/renode/renode). The goal is HIL-style testing on
every push, in parallel, with no hardware attached. Physical-only work stays
on the physical bench: analog accuracy, real power, flash wear and bus physics.

**Status: Phase 0 — feasibility spike done.** The unmodified ECU firmware
boots in Renode. Its three CAN buses run at the firmware's configured rates.
Read [`docs/proposal.md`](docs/proposal.md) for the design, what the spike
proved, coverage against IFS_HIL's suites, and the plan. Work is tracked
in issues, one per phase: [#1](https://github.com/isc-fs/IFS_vHIL/issues/1)–[#4](https://github.com/isc-fs/IFS_vHIL/issues/4).

**Contributing:** `dev` is the trunk and `main` is release-only. Branch
`feat/` `fix/` `docs/` `chore/` `test/` off `dev` and open a PR back into it.
See [`docs/development/setup.md`](docs/development/setup.md), and
[`CLAUDE.md`](CLAUDE.md) for the operating model.

## Layout

```
systems/                        Systems as data: boards, their firmware, the buses between them
catalog/platforms/              Emulatable MCUs (stm32h733)
catalog/boards/                 Boards: a platform plus named connectors and pins (mlc-carrier)
catalog/firmware/               Firmware sources: repo, ref, build recipe, load address (ecu, ams)
catalog/models/                 Device models: ltc6820 isoSPI bridge, ltc6811 battery monitor, sd-card
models/renode/IsoSpi.cs         The LTC6820 + LTC6811 isoSPI models (C#, compiled by Renode at load)
schemas/vhil.schema.json        Schema every catalogue entry and system is validated against
platforms/cpus/stm32h733.repl   STM32H733 Renode platform (Renode ships only H743/H753/H747)
vhil/system.py                  Generator: validate / render / bench / build a system
scripts/explore.sh, probe.sh    Headless boot + log / monitor-command helpers
scripts/run-ifs-hil.sh          Run an IFS_HIL suite against the virtual bench (CI and local)
vhil/                           Virtual broker, Renode monitor client, pytest plugin
configs/gaps.yaml               IFS_HIL tests the virtual bench can't pass yet, and why
configs/peripherals.yaml        Unmodelled hardware the firmware may touch, and why (peripheral guard)
tests/ecu_smoke.robot           CAN-side smoke checks (heartbeat, buses, 0x704 health)
tests/unit/                     Host-only checks of the catalogue, systems and generator
CLAUDE.md                       Operating model: branch/commit/PR policy, invariants
docs/proposal.md                Design, spike results, coverage, phases, risks
docs/development/setup.md       Toolchain, branching, issues, PRs, releases
.github/workflows/              CI: unit, ECU smoke (Robot), IFS_HIL ECU suite
```

## Try it (Linux or WSL2)

Requirements: Renode 1.17.0 (portable Linux build) and Arm GNU Toolchain
14.2.Rel1, the toolchain pinned by IFS_HIL's recipes.

```sh
export RENODE=~/renode_1.17.0-portable/renode     # and the Arm toolchain on PATH

# build every board's firmware from the source its system declares
python -m vhil.system build systems/ecu.yaml --workdir build/fw   # prints ecu=<elf>

# 5 virtual seconds, logging every CAN frame the ECU queues
scripts/explore.sh systems/ecu.yaml build/fw/ecu@dev/build/ECU08.elf 5

# automated smoke checks (the Renode script is generated from the system)
python -m vhil.system render systems/ecu.yaml -o build/ecu.resc
$RENODE-test tests/ecu_smoke.robot --variable ELF:$PWD/build/fw/ecu@dev/build/ECU08.elf --variable RESC:$PWD/build/ecu.resc

# IFS_HIL's own ECU suite, unmodified, against the virtual ECU (needs vcan
# can0..can2; on WSL2 run scripts/wsl-vcan.sh --load first)
scripts/run-ifs-hil.sh ~/IFS_HIL build/fw/ecu@dev/build/ECU08.elf smoke
```

A system file is the source of truth: edit `systems/*.yaml`, never a
generated script. `python -m vhil.system validate <system>` checks one against
the schema and the catalogue.

Tests the virtual bench can't pass yet are listed in
[`configs/gaps.yaml`](configs/gaps.yaml) with the reason and issue; they are
reported as skips, never silently dropped. If the firmware touches hardware
the bench doesn't model and that isn't explained in
[`configs/peripherals.yaml`](configs/peripherals.yaml), the run fails even
when every test passed.

`renode-test` needs Renode's Python test requirements (`robotframework==6.1`,
`psutil`, `pyyaml`, `telnetlib3`, `robotframework-retryfailed`).
