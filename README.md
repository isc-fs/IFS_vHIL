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
platforms/cpus/stm32h733.repl   STM32H733 platform (Renode ships only H743/H753/H747)
scripts/ecu.resc                Boot the ECU app image as the CAN bootloader leaves it
scripts/build_fw.sh             Build a DUT image with IFS_HIL's recipe
scripts/explore.sh, probe.sh    Headless boot + log / monitor-command helpers
tests/ecu_smoke.robot           CAN-side smoke checks (heartbeat, buses, 0x704 health)
CLAUDE.md                       Operating model: branch/commit/PR policy, invariants
docs/proposal.md                Design, spike results, coverage, phases, risks
docs/development/setup.md       Toolchain, branching, issues, PRs, releases
.github/workflows/ecu-smoke.yml  CI: build the ECU from IFS_HIL's recipe, run the smoke suite
```

## Try it (Linux or WSL2)

Requirements: Renode 1.17.0 (portable Linux build) and Arm GNU Toolchain
14.2.Rel1, the toolchain pinned by IFS_HIL's recipes.

```sh
git clone -b dev https://github.com/isc-fs/IFS08-CE-ECU ~/vhil/ECU
scripts/build_fw.sh ~/vhil/ECU ~/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin

# 5 virtual seconds, logging every CAN frame the ECU queues
RENODE=~/renode_1.17.0-portable/renode scripts/explore.sh scripts/ecu.resc ~/vhil/ECU/build/ECU08.elf 5

# automated smoke checks
~/renode_1.17.0-portable/renode-test tests/ecu_smoke.robot --variable ELF:$HOME/vhil/ECU/build/ECU08.elf
```

`renode-test` needs Renode's Python test requirements (`robotframework==6.1`,
`psutil`, `pyyaml`, `telnetlib3`, `robotframework-retryfailed`).
