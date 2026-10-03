# IFS_HIL test analysis → the native vHIL test set

**Issue:** [#3](https://github.com/isc-fs/IFS_vHIL/issues/3) · **Date:** 2026-10-02
**Analysed:**
- IFS_HIL `origin/dev` @ `fa11a25`
- ECU firmware `dev` @ `544b651`
- AMS firmware `main` @ `df1c961`

Every test in IFS_HIL's ECU and AMS suites was analysed for the firmware behaviour it actually verifies, stripped of bench mechanics, and classified by whether and how the vHIL can test that behaviour. The per-test tables, with firmware `file:line` references, are in the appendices:

- [Appendix A: ECU (`tests/hil/vcu/`)](ifs-hil-tests-ecu.md): 57 tests
- [Appendix B: AMS (`tests/hil/ams/`) and bench self-tests](ifs-hil-tests-ams.md): 139 tests, plus 8 self-test files

The vHIL does **not** port these tests. They were written around a bench: a Pico standing in for the battery; DACs, relays and expanders standing in for the car; wall-clock timing; retries for bench faults. The native test set (§4) re-derives each behaviour as a test against models, in virtual time, and adds the behaviours IFS_HIL never covered.

## 1. Classes and counts

| Class | Meaning | ECU | AMS | Total |
|---|---|---|---|---|
| `virtual` | Testable now through CAN, GPIO, power and the battery models | 26 | 99 | **125** |
| `virtual-needs:*` | Testable once a named model or milestone exists (see §5) | 29 | 32 | **61** |
| `physical-only` | Depends on analog accuracy, bus physics, power or real flash wear | 1 | 7 | **8** |
| `bench-artifact` | Exists only because of the bench or its workarounds | 1 | 1 | **2** |
| | | **57** | **139** | **196** |

The 8 bench self-test files (`tests/hil/test_*.py`: MCP2515s, INA226s, DACs, SPI ADCs, relays, nRF24, a placeholder) test the backplane, not firmware. They have no place in the vHIL.

Blockers among the `virtual-needs` tests:

| Needs | Tests | Notes |
|---|---|---|
| ADC3 model for the H73x ([#19](https://github.com/isc-fs/IFS_vHIL/issues/19)) | 32 | ECU pedals (21), AMS pack current (11). Biggest single unblocker |
| CAN bootloader emulation ([#4](https://github.com/isc-fs/IFS_vHIL/issues/4)) | 13 | Flash, jump, discover, jump reason |
| SDMMC IDMA model | 7 | AMS SD logger. Modelled (#21, `models/renode/Stm32H7Sdmmc.cs`): the AMS mounts a FAT32 card image, logs, and seals; `tests/sim/test_ams_sd.py` reads the card back |
| ECU + AMS in one system (M3, [#11](https://github.com/isc-fs/IFS_vHIL/issues/11)) | 3 | ECU side of the real-AMS tests |
| FDCAN bus-off injection hook | 2 | AMS recovery logic; real error physics stays physical |
| RTC backup domain across warm reset | 2 | Error-latch persistence, HIL_CLEAR build |
| RCC reset-flag model | 1 | ECU reset cause |
| FDCAN fault injection | 1 | ECU FDCAN1 failure must not silence FDCAN2 |

## 2. IFS_HIL has drifted from the firmware

Many tests no longer match the firmware they test. They fail, pass for the wrong reason, or check a rule that was removed. Report these to the IFS_HIL and firmware teams regardless of the vHIL.

**ECU** (Appendix A, D1–D9):
- **EV.2.3 was removed from the firmware.** G-002 tests a rule that no longer exists, and L-011's `ev_2_3` check is vacuous.
- **Fault recovery sends a burst of up to 3 `0x360` frames per cycle**, ending in `Off` + Flt_Clear. E-003…E-006 assert on the last frame and are race-prone or wrong.
- **The CAN start-button stub is gone.** START is a debounced GPIO, so every manual-R2D test depends on the PB5 wire.
- **Torque values in the tests are stale:**
  - `0x700.torque_cmd` now carries real Nm, which breaks E-002's final assertion.
  - `can_map.dv_torque_nm` uses the old scale: the firmware sends −89 Nm for 40 %, not −80.
  - Power and thermal caps (5500 rpm when stale, 60 % unknown temperature) make L-007, L-008 and L-010 unpassable or coincidental.
- **`0x700 ok_precharge` is raw and sticky**, so F-004 (real AMS) should fail.
- **Duplicate test IDs across files:** B-001, B-003, E-003, E-004, F-001, J-001, J-002.

**AMS** (Appendix B):
- **Balancing (BAL) is inverted.** With no or stale `0x103`, the firmware is **Off**, not Auto. BAL-01…05 fail in setup or assert the opposite.
- **Over- and under-temperature faults are disabled** (`TempFaultsTrusted = false`). B-026c, B-029-OT and K-100 expect an Error that never comes.
- **Fault reasons are stale:**
  - A silenced chain reports BmsModuleOffline (2), not BmsStale (3) (B-029).
  - A TSMS drop in Charge latches ChargerTsmsOpen (15) rather than returning to Start (C-039c).
  - A charger precharge timeout reports ChargerStale (14), not FsmError (12) (C-037c).
- **The current limit is 185 A, not the profile's 200 A.** J-102 now trips, and the I-100/N-003 sweeps latch CurrentOverLimit.
- **Profile values have drifted:** `bms_stale_ms` is missing (tests use 1500 ms; the firmware uses 350 ms); the cell debounce is ~250 ms, not 300 ms; A-011's DLC list omits `0x130` and `0x021`.

## 3. Behaviour no IFS_HIL test covers

Highlights; the full lists are in the appendices. These go straight into the native set.

**ECU**
- ⚠ **A silent inverter in Active is not detected.** `inv_present` is computed (`control_task.cpp:92`) but never used by `Controller::step`. If `0x461` stops in state 4/6, the ECU keeps commanding torque on the last held state. This looks like a firmware safety finding, and the vHIL can test it with CAN alone.
- **Torque limiter chain:** power envelope, cell-voltage derate with IR compensation, motor and pack thermal caps.
- **Driver interface:** precharge 10 s timeout and re-entry; RTDS output (2 s R2D, AS-Emergency pattern); START debounce.
- **Inverter recovery:** Active → WaitInvStandby re-drive and `inv_redrive_count`.
- **Bootloader trigger** refused in drive states.
- **Robustness:**
  - IWDG on a ControlTask stall.
  - Fault latch across reset (the deferred I-004).
  - TX FIFO overflow, which is silently dropped and is a candidate cause of IFS_HIL#128.
- **Calibration NVM session** (`0x7E2`–`0x7E5`).
- **DC-link discharge.**
- **Dash bus (FDCAN3), GPS (USART10).**

**AMS**
- **Predicate boundaries and order:** grace edge, 2799/2800 and 4200/4201 mV, exact debounces, BmsModuleOffline before BmsStale.
- **Battery-side faults:**
  - Open-wire (ADOW, reason 16).
  - Temperature-sensor disconnect (reason 13).
  - The tap-artifact guard.
- **Balancing:** BALN, the `0x104` per-module mask and its dead-man, hysteresis, max 8 per module, no adjacent cells, 50 °C lockout, DCC quiesce before ADCV.
- **Charger path:** ChargerStale and ChargerTsmsOpen.
- **Re-arm and transition guards:** the re-arm interlock (`discharge_engaged`, `dc_bus_valid`) and the Transition guard.
- **Error latch across warm reset;** boot trigger refused while energised.
- **isoSPI:** link cut and recovery, PEC retry policy (one bad attempt absorbed).
- **Pack current [ADC3]:** SOC/coulomb counting (`0x130`, sent but untested), the over-current trip-time curve.
- **SD logger content and rotation; IMU telemetry; pit-diag arm/ack; watchdog.**

## 4. The native test set

Tests are grouped by **firmware behaviour and system**, not by IFS_HIL block. Each group is one suite, and each suite will get one issue.

Conventions:
- Assertions are in virtual time: exact cadences and debounce edges, not wall-clock windows (M4, [#12](https://github.com/isc-fs/IFS_vHIL/issues/12)).
- Stimulus goes through models: cells, temperatures, GPIO, CAN and fault injection.
- Every suite checks the firmware **as it is today** and records the drift above as a finding, not as a test.

### AMS (`systems/ams.yaml`)

| Suite | Behaviour | Supersedes | Needs |
|---|---|---|---|
| **ams-boot** | Discovery, identity (`0x6C6`, fwinfo), first-frame timing, all producers alive, cockpit sentinel, pull-downs, AMS_OK low during grace | A-001, A-004…A-007, A-009, A-010, A-013, A-014, E-052, E-060, G-097, K-102, M-040, M-041, S-140, S-144, C-045, C-046, C-047 | — |
| **ams-can** | Contract DLCs, cadences (500 ms, 100 ms), heartbeat, extended-ID filter, pit-diag arm/ack, noise immunity; 30 min idle/Run soaks | A-008, A-011, A-012, B-010, K-103, M-03, J-132, F-081, E-050, E-051 | — |
| **ams-cells** | UV/OV predicates at exact boundaries and debounces, fault reason + module detail, tap-artifact guard | B-026a/b, B-028 ×2, B-029 none/UV/OV | — |
| **ams-temps** | Temperature decode; disconnect (13); OT/UT are **not** faults while `TempFaultsTrusted=false` | B-026c, B-029-OT, E-064 | — |
| **ams-chain** | Silent chip, cut isoSPI link and recovery, per-IC PEC localisation, BmsModuleOffline before BmsStale, PEC retry, open wire | B-022, B-029-stale, E-061, E-063, E-065, E-066, E-067, G-102 | open-wire model (for ADOW) |
| **ams-fsm-car** | Start → Precharge → Transition → Run; TSMS/DASH_CHG edges; precharge timeout; Run re-arm; bus collapse; sticky Error; VcuStale | C-030…C-034, C-039a/b, C-041, C-042, C-043, C-049, C-050, B-021, B-027, B-029-VCU, K-100 | — |
| **ams-fsm-charger** | Charger lock, proceed, ChargerStale (14), ChargerTsmsOpen (15), wrong magic, PRE never closes | C-037, C-037b/c, C-038, C-039c, B-030 | — |
| **ams-contactors** | AIR−/AIR+/PRE/AMS_OK as a **GPIO edge trace**, atomic patterns, ≤ 1 tick on Error | F-060…F-066, R-110…R-115, C-048 | — |
| **ams-balancing** | Off/Auto/BALN/BALM commands, DCC read straight from the LTC models, hysteresis, limits, quiesce | BAL-01…06 | — |
| **ams-current** | ADC calibration, transfer function, IIR settling, over-current trip curve, sticky latch, leg disconnect, stale, SOC | I-100, I-101, J-100…J-102, K-101, M-042, N-001…N-004 | ADC3 #19 |
| **ams-reset** | Boot trigger (Start, Error; refused when energised), wrong ID/DLC/payload ignored, error latch across warm reset | D-051, D-051b, D-045 ×4, F-076 ×2, F-080 | backup-domain retention (F-076) |
| **ams-bootloader** | Discover, flash, verify, jump, jump reason, flash under bus load | A-002, A-003, D-050, D-052, M-05, F-071, F-072, F-074, F-075, S-142 | bootloader #4 |
| **ams-canbus** | Bus-off recovery and counter | J-130, J-131 | FDCAN bus-off hook |
| **ams-sd** | Boot with no card, dead card, card; logger never disturbs MainTask; card image: columns, 4 Hz, rotation, `.CRC`, indices, reset mid-write | S-141, S-143, T-150…T-152, U-160…U-162 | SDMMC IDMA |
| **ams-telemetry** | SOC/coulomb counting, IMU rows and fault tolerance, `0x6CA`, `0x6C5` post-mortem | (new) | ADC3 #19; BMI088 model |

### ECU (`systems/ecu.yaml`)

| Suite | Behaviour | Supersedes | Needs |
|---|---|---|---|
| **ecu-boot** | Exact `0x100`/`0x704` cadence, first-frame latency, task liveness per cycle, heap trace, reset causes, bus independence | B-001 ×2, B-003 ×2, B-004, I-001, I-002, I-003, I-005 | RCC reset flags (I-002) |
| **ecu-startup** | Vdc latch, precharge gate and 10 s timeout, START debounce, R2D 2 s dwell, RTDS pulse | C-001, C-002, C-004, C-005, E-001 | ADC3 #19 (brake) |
| **ecu-ams-gate** | 200 ms freshness edges, AmsError entry/exit, stale-in-error, uDV not refreshing the AMS | F-001, F-002, F-003, L-001, L-014 | — |
| **ecu-inverter** | Ordered `0x360` burst, Flt_Clear edge, re-drive, `inv_redrive_count`, **silent inverter in Active** | E-003 ×2, E-004, E-005, E-006, E-007 | plant M6 #14 (recovery realism) |
| **ecu-torque** | APPS map and deadbands, T.11.8.9 at 100 ms, ADC faults, power/cell/thermal caps; brake + throttle does **not** cut | E-002, G-001, G-002, G-003, J-001 (soak) | ADC3 #19; plant M6 #14 |
| **ecu-dv** | uDV contract: `0x504`/`0x505`/`0x506`/`0x511`, DV entry and refusal, stale request, manual precedence, latch clearing, AmsError pre-emption | L-002, L-004…L-013 | ADC3 #19 |
| **ecu-diag** | Pit-diag enable/ack, every `0x70x` decoded against the DBC at exact cadence, inverter mirrors, dash FDCAN3, GPS | H-001…H-003, J-001/J-002 (telemetry), L-003 | UART NMEA feeder (GPS) |
| **ecu-robustness** | IWDG on a ControlTask stall, fault latch across reset, TX queue/FIFO overflow (#128), malformed frames | (new) | fault injection |
| **ecu-cal** | Calibration session FSM, `vehicle_safe` gate, commit/persist/reload, corrupt-record fallback | (new) | ADC3 #19; flash-programming model |
| **ecu-bootloader** | fwinfo, `0x002` accepted and refused, flash + verify + jump, BKP registers | A-002, A-003, A-005, A-006, J-002 | bootloader #4 |

### Systems of several boards

| Suite | Behaviour | Supersedes | Needs |
|---|---|---|---|
| **sys-ecu-ams** | The ACU-bus contract both ways: VCU heartbeat keeps the AMS out of VcuStale; ECU `ok_precharge` follows the AMS; AMS power loss makes the ECU's AMS view stale | F-005, F-001 (real AMS), F-004 (real AMS) | M3 #11 |

### Stays on the physical bench
A-001 (carrier draw), M-01, M-04 and M-06 (bit timing, error counters), F-070 (cold-boot power), F-073 (flash write integrity), F-077 (power loss mid-flash), F-078 (backup-domain decay). Analog gain and offset calibration (part of I-100/I-101), the N-block pull-down on a physically open connector, and card FTL behaviour (part of T-152) also stay physical.

## 5. Enablers, in order

1. **Test API in virtual time (M4, [#12](https://github.com/isc-fs/IFS_vHIL/issues/12)).** CAN inject/observe with virtual timestamps, GPIO input set and output edge history, model calls, assertions on exact time. Every suite above uses it, and it replaces the 500 ms frame polling the current Robot suites use.
2. **ADC3 for the H73x ([#19](https://github.com/isc-fs/IFS_vHIL/issues/19)).** Unblocks 32 tests and a healthy AMS Start.
3. **ECU + AMS (M3, [#11](https://github.com/isc-fs/IFS_vHIL/issues/11)).** The AMS leaves VcuStale; `sys-ecu-ams` becomes possible.
4. **Platform fidelity, filed as separate issues:** SDMMC IDMA; FDCAN bus-off and fault hooks; RCC reset flags and backup-domain retention across warm reset; LTC6811 open-wire.
5. **Bootloader emulation ([#4](https://github.com/isc-fs/IFS_vHIL/issues/4)).**
6. **Plants (M6, [#14](https://github.com/isc-fs/IFS_vHIL/issues/14)):** inverter, pedals; BMI088 model.

The Phase 1 compatibility run (IFS_HIL's ECU smoke over the virtual broker) stays as a regression check until `ecu-boot`, `ecu-ams-gate` and `ecu-diag` cover it.
