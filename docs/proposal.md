# IFS_vHIL — proposal

> **Direction and roadmap now live in [`vision.md`](vision.md).** This
> document keeps the Phase 0/1 design, spike results and findings.

**Status:** Phase 0 (feasibility spike) done — see [§3](#3-what-the-spike-proved).
**Date:** 2026-10-01

## 1. Why

[IFS_HIL](https://github.com/isc-fs/IFS_HIL) runs the real firmware on real
boards, and nothing replaces that. But it is one bench, behind one runner,
on one person's desk:

- A run needs `bench-01` powered, online and unwedged. When it isn't, every
  firmware PR waits — or, as happened twice in September 2026, the chain
  fails silently and PRs look green.
- One run at a time (the bench lock), up to 90 minutes each.
- Nobody can reproduce a failure without the hardware in front of them.

A **virtual HIL** runs the *same firmware image* on an emulated STM32H733 in
[Renode](https://github.com/renode/renode) (MIT, Antmicro). It needs no
hardware, so it can run on every push, in parallel, on GitHub-hosted runners,
deterministically, with a debugger attached when something fails.

It does not replace the physical bench: it **filters** what reaches it. Logic,
state machines, CAN contracts and timing at the millisecond level move to the
virtual bench; analog accuracy, real power, flash wear, bus physics and the
bench's own hardware stay physical.

### Goals

- Boot the unmodified IFS_HIL firmware images (same recipe, same ELF) — no
  emulator-only build flags.
- Drive them over CAN the way the physical bench does, so the existing
  pytest suites and simulators (`tools/firmware_test/vcu/*`, `ams/*`) can run
  against either bench.
- Plug into IFS_HIL's capability routing, so `/hil-test` can land on a
  virtual bench when no physical capability is needed.

### Non-goals

- Cycle accuracy, CAN bit timing, analog noise, power behaviour.
- Replacing the physical bench, or the per-repo host SIL tests.

---

## 2. Architecture

```
IFS_HIL recipe ─▶ ECU08.elf / AMS.elf  (identical to what the bench flashes)
                        │
        ┌───────────────▼────────────────────────────┐
        │ Renode  ·  one machine per board          │
        │  platforms/cpus/stm32h733.repl              │
        │   ├─ FDCAN1/2/3 ─▶ CAN hubs (inv/acu/dash)  │
        │   ├─ ADC3       ◀─ SetVoltage (pedals, I)   │
        │   ├─ GPIO       ◀▶ SDC, relays, buttons     │
        │   └─ SPI1       ◀▶ LTC6820+6811 model (AMS) │
        └───────────────┬────────────────────────────┘
                        │ SocketCANBridge (Linux)
                     vcan0..2
                        │
     IFS_HIL pytest suites + simulators (python-can), unchanged
```

- **Platform.** Renode has no H72x/H73x part, so this repo defines one
  ([`platforms/cpus/stm32h733.repl`](../platforms/cpus/stm32h733.repl)): it
  reuses Renode's common `stm32h7.repl` and adds what the H733 has and the
  H743 lacks — FDCAN3, TIM23 (the HAL timebase), USART10, a real SPI1 — plus
  the H733 memory map and the MainLite's 24 MHz HSE. Every address and IRQ is
  from ST's `stm32h733xx.h`.
- **Boot.** The app images are linked at `0x08020000` behind the CAN
  bootloader. The ECU never sets VTOR itself, so the boot script does what
  the bootloader would: load the ELF, set VTOR, start. (Emulating the
  bootloader too is Phase 4.) *Since #4, every system boots through the real
  CAN bootloader instead (CLAUDE.md invariant 5).*
- **CAN.** One Renode CAN hub per physical bus. On Linux, `SocketCANBridge`
  exposes each hub as a `vcan` interface, which is exactly what the physical
  bench's tests already talk to — the main reason the suites can be reused.
- **Stimulus.** Monitor commands (`adc3 SetVoltage`, GPIO `OnGPIO`) behind a
  small adapter shaped like IFS_HIL's `hil_client` (DAC / TCA / relay calls),
  so fixtures need a backend switch, not a rewrite.
- **AMS cell chain.** A C# Renode peripheral on SPI1 modelling the LTC6820
  bridge and a 10× LTC6811-1 chain (CFGA/CV/AUX/STAT/COMM, PEC15, ADG731 mux
  via WRCOMM/STCOMM). The protocol logic already exists in IFS_HIL's Pico
  emulator (`tools/pico_ltc_emulator/`) and is ported, not reinvented.

---

## 3. What the spike proved

Run on 2026-10-01: Renode 1.17.0, ECU `dev` @ `544b651` built with IFS_HIL's
`ecu.yaml` recipe (Arm GNU 14.2.Rel1), 5 s of virtual time.

| Check | Result |
|---|---|
| Boots to FreeRTOS, no HardFault / `Error_Handler` | ✅ |
| FDCAN1 (inverter): 0x360, 0x362 | ✅ 100 Hz |
| FDCAN2 (ACU): 0x100 heartbeat, 0x506 | ✅ 100 Hz |
| FDCAN2: 0x504 / 0x505 / 0x511, GPS 0x508 / 0x509 | ✅ 10 Hz / 5 Hz |
| FDCAN2: 0x704 health (ungated, DiagTask) | ✅ 1 Hz |
| FDCAN3 (dash, H733-only): 0x510–0x521 | ✅ 5 Hz |
| Virtual time matches firmware periods | ✅ once HSE = 24 MHz |
| Wall-clock cost | ≈ 1.25 s host per 1 s virtual |
| `tests/ecu_smoke.robot` (6 checks) | ✅ 6/6 in ≈ 16 s; a deliberately wrong check fails as expected |

Findings worth keeping:

- **The HSE frequency matters.** With Renode's default 8 MHz crystal the RCC
  model computes a 176 MHz core clock and every FreeRTOS period ran 3× slow.
  The platform now sets `rcc.hseFrequency = 24 MHz`.
- **TIM23 is load-bearing.** CubeMX uses it as the HAL tick; without it every
  HAL timeout loop would spin forever.
- **0x704 decodes cleanly:** task mask `0x1F` (all five tasks), reset cause
  PIN, uptime counting. CanRxTask is live on a quiet bus because it bumps its
  liveness on every 100 ms queue timeout — IFS_HIL's I-003 docstring ("only
  runs when frames arrive") predates that and is stale.
- **Noise, not failures:** USB OTG (initialised, unused), `CAN_CCU`
  (calibration unit, unused) and ADC channel preselection log warnings.

---

## 4. Coverage — what can move off the physical bench

Against IFS_HIL's suites (`configs/suites.yaml`).

**ECU (`tests/hil/vcu/`)**

| Block | Virtual? | Needs |
|---|---|---|
| A boot | mostly | power cycle → `machine Reset`; A-003 reflash needs the bootloader (Phase 4) |
| B FDCAN / heartbeat | yes | CAN I/O (Phase 1) |
| C FSM, F AMS, L DV | yes | CAN I/O + simulated AMS/inverter (IFS_HIL's sims) |
| E inverter, fault recovery | yes | `inverter_sim.py` over vcan — **better than bench-01, which has no inverter** |
| G plausibility | yes | ADC3 injection for APPS / brake |
| H pit-diag, I health, J telemetry | yes | CAN I/O |
| J soak | yes | no speed-up: the ECU already emulates at ≈1.25× real time; the win is parallel runs |

**AMS (`tests/hil/ams/`)**

| Block | Virtual? | Needs |
|---|---|---|
| A boot, B safety, C FSM, BAL, R relays | yes | LTC chain model + GPIO stimulus (Phase 2) |
| E LTC, K encoders, N disconnect | yes | LTC model incl. fault injection (open wire, silent module) |
| I/M current | logic only | ADC injection; **gain/offset accuracy stays physical** |
| J bus-off | **no** | Renode does not model CAN error counters / bus-off |
| F flash endurance, D bootloader | **no / Phase 4** | real flash wear; bootloader emulation |
| S/T/U microSD | partly | SDMMC1 model + card image; physical card pulls stay physical |

**Stays physical:** analog accuracy, bus physics and bus-off, power rails and
brown-out, flash endurance, the bench self-tests (`tests/hil/test_*.py`), and
the Pico emulator's own fidelity.

---

## 5. Plan

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **0 — spike** ✅ | H733 platform, ECU boot script, Robot smoke test | ECU boots, all three buses at the right rates |
| **1 — CAN I/O + CI** ([#1](https://github.com/isc-fs/IFS_vHIL/issues/1)) | SocketCAN bridge to `vcan`, a pytest fixture that starts Renode and yields the buses, CI workflow building the ECU from IFS_HIL's recipe | IFS_HIL's ECU `smoke` suite (A, B, F, I) passes unmodified against the virtual ECU in GitHub Actions |
| **2 — AMS** ([#2](https://github.com/isc-fs/IFS_vHIL/issues/2)) | LTC6820/LTC6811 chain model (C#), AMS boot script, GPIO/ADC stimulus adapter | AMS boots out of Error with seeded cells; AMS `smoke` passes |
| **3 — fleet integration** ([#3](https://github.com/isc-fs/IFS_vHIL/issues/3)) | A `kind: virtual` bench descriptor in IFS_HIL, routed to GitHub-hosted runners; `/hil-test` picks virtual when no physical capability is needed | A firmware PR gets a virtual verdict without bench-01 |
| **4 — depth** ([#4](https://github.com/isc-fs/IFS_vHIL/issues/4)) | CAN bootloader emulation (A-003, Block D), fault injection, coverage reports, multi-node (ECU ↔ AMS on one virtual bus) | ECU and AMS talking to each other, no simulators |

Phase 1 is the decisive one: if the existing suites run unmodified, the rest
is incremental.

---

## 6. Risks

- **Model fidelity.** Renode is functional, not cycle-accurate. Tests that
  assert on sub-millisecond timing need loose tolerances or stay physical.
- **Silent no-ops.** Renode *tags* unmodelled peripherals: writes are dropped
  and reads return 0. A firmware path that depends on one can pass for the
  wrong reason. Mitigation (in place): the peripheral guard fails any run
  whose firmware touches unmodelled hardware not explained in
  `configs/peripherals.yaml`.
- **Linux-only CAN bridge.** `SocketCANBridge` needs Linux (or WSL2 with
  `vcan`). CI is Linux, so this only affects local runs on Windows/macOS.
- **Upstream drift.** Pin the Renode version; bump deliberately.
- **Two benches, two truths.** A test that passes virtually and fails
  physically is a model gap until proven otherwise — track it, don't hide it.
