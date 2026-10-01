# Appendix A: IFS_HIL ECU tests (`tests/hil/vcu/`)

Part of [the IFS_HIL test analysis](ifs-hil-tests.md).

**Analysed:**
- IFS_HIL `origin/dev` @ `fa11a25`: 57 test functions in 14 files (~63 cases with parametrisation)
- ECU firmware `dev` @ `544b651`

**Paths:**
- Test locations are `file:line` in `tests/hil/vcu/test_block_*.py`.
- Firmware references are relative to `Core/Src/app/` unless shown otherwise.

## Drift between the suite and the firmware

| # | Finding | Affects |
|---|---|---|
| D1 | EV.2.3 was removed from the firmware (`control.cpp:45-57`; `pit_diag_status.def:7`, bit0 now reserved) | G-002 tests a removed rule; the `ev_2_3` check in L-011 is vacuous |
| D2 | In inverter state 10/11 the ECU sends 2–3 `0x360` per cycle, ending in `Off(0x01)` + Flt_Clear (`control.cpp:321-339`, `control_task.cpp:355-359`) | E-003…E-006 assert the last frame is `0x0D`/`0x13`: race-prone or wrong |
| D3 | The CAN start-button stub is gone; START is a GPIO with a 50 ms debounce (`can_rx_task.cpp:79-90`, `io_signals.cpp:60-62`); the profile still sets `start_btn_via_can: true` | Every manual-R2D test depends on the physical PB5 wire |
| D4 | `0x700.torque_cmd` now carries real Nm (`pit_diag.cpp:52`) | E-002's final `tc == 0` |
| D5 | `can_map.dv_torque_nm` uses /90 and a 10 % deadband; the firmware uses `pct*240/95 − 1200/95` and a 5 % deadband (`ecu_config.hpp:171,464-466`, `inverter.cpp:11-22`); 40 % → −89 Nm, not −80 | L-007, L-008, L-010, L-011 |
| D6 | Caps: stale `0x463` → the power envelope assumes 5500 rpm (~51 %); unknown temperatures → 60 % (`control_task.cpp:175-178`, `power_limit.cpp`) | L-010 can't pass; L-007/008/011 pass only by coincidence |
| D7 | `0x700 ok_precharge` is the raw, sticky `veh.ok_precharge` (`pit_diag.cpp:45`), not the freshness-gated value | F-004 (real AMS) should fail; L-014 documents this |
| D8 | Duplicate IDs: B-001, B-003, E-003, E-004, F-001, J-001, J-002 | — |
| D9 | Stale tool docstrings: `ams_sim.py`, `inverter_sim.py` and `drive_to_active.py` cite `control.c`/`can.c` and say Active is reached at `inv_state==3` (it is 4, `ecu_config.hpp:72`); the `vcu_can_sp` docstring says 37.5 % (the profile has 87.5 %) | — |

## Tests

**Classes:**
- **V**: `virtual`
- **N:x**: `virtual-needs:x`
- **P**: `physical-only`
- **B**: `bench-artifact`

### A: boot

| ID | Test · line | Behaviour verified (firmware) | Class | Native idea · notes |
|---|---|---|---|---|
| A-001 | `a001_carrier_draws_current` · a_boot:105 | Carrier powered, boot current drawn (no firmware logic) | P: supply current | Replace with "CPU reaches `main`"; bench-health check |
| A-005 | `a005_fwinfo_magic_and_product` · a_boot:148 | fwinfo at +0x400: magic `0xF14F1B00`, product `IFS08-CE-ECU` (`firmware_info.cpp:71,82`) | V | Static ELF/bin check in CI; BL-side validation after #4 |
| A-002 | `a002_bl_discover` · a_boot:167 | `0x002`/B007AD12 → BKP0R magic, reboot; BL answers discover, node 0x01 (`can_rx_task.cpp:66-75`, `bootloader.cpp:23-28`) | N:bootloader#4 | Assert reset, BKP0R = `0xB00710AD`, discover reply. Sample-point flip and retries are bench mechanics |
| A-006 | `a006_bl_round_trip` · a_boot:186 | As A-002 | N:bootloader#4 | Merge into A-002 (weaker duplicate) |
| A-003 | `a003_flash_and_jump` · a_boot:200 | BL flash, verify, jump; app streams `0x100` | N:bootloader#4 | Flash the image under test via the emulated BL. The stale-binary guard is a bench workaround |

### B: FDCAN and heartbeat

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| B-001 | `b001_heartbeat_with_inv_bus_unpeered` · b_fdcan:35 | FDCAN2 up independently of FDCAN1; `0x100` streams without an inverter (`app_init_task.cpp:80-83`, `control_task.cpp:305-323`) | V | One `0x100` per 10 ms ± 0.5 ms with the INV bus unattached |
| B-002 | `b002_inv_degraded_keeps_acu_tx` · b_fdcan:47 | A failed FDCAN1 doesn't silence FDCAN2 TX (#48 regression) | N:fault-injection | Force FDCAN1 bring-up failure, TX-FIFO full or bus-off in the model |
| B-003 | `b003_both_buses_under_traffic` · b_fdcan:65 | Concurrent INV RX / ACU TX; MessageRAM non-overlap (`ecu_config.hpp:568`) | V | Flood `0x461`/`0x466`; `0x100.dc_bus` equals the injected value exactly. Only meaningful if the FDCAN model honours RAM offsets |
| B-004 | `b004_tx_alive_before_any_peer` · b_fdcan:83 | TX from the first control cycle, in WaitInvVdcConfig (`control_task.cpp:305`) | V | First `0x100` within X ms of reset (the bench allows 6 s) |
| B-001 | `b001_heartbeat_present_and_cadenced` · b_heartbeat:38 | `0x100` present, max gap 3× period | V | Exact 10 ms cadence; continuity across every FSM transition. Duplicate ID |
| B-003 | `b003_dc_bus_decodes_sane` · b_heartbeat:56 | `0x100` dc_bus in range | V | Inject `0x466`=400 → `dc_bus`=400, valid; stop → 0, invalid after 500 ms (`vehicle_service.cpp:117-124`). Weak test today |

### C: FSM

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| C-001 | `c001_vdc_gate` · c_fsm:38 | Hold in WaitInvVdcConfig until a `0x466` (`control.cpp:153-155`, `control_task.cpp:93`) | V | Also: the gate latches forever after one `0x466` |
| C-002 | `c002_precharge_only_gate` · c_fsm:52 | Precharge → WaitStartBrake only on a fresh `0x020[0]` (`control.cpp:156-164`) | V | Add the 10 s precharge-timeout re-entry |
| C-004 | `c004_full_startup_to_active` · c_fsm:69 | Vdc → precharge → START + brake → 2 s R2D → inv 4 → Active (`control.cpp:165-186`) | N:ADC3#19 | Exact 2000 ms dwell; RTDS pulse width. D3 |
| C-005 | `c005_pit_diag_reflects_fsm` · c_fsm:89 | `0x700[0]` is a valid FSM enum | V | Fold into H-003 |

### E: inverter and fault recovery

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| E-003 | `e003_hard_fault_commands_reset` · e_fault_recovery:54 | inv 11 → `0x0D`, then Off + Flt_Clear (`control.cpp:327-332`) | V | Per-cycle sequence `[0x0D, 0x81, 0x362]`. D2; duplicate ID |
| E-004 | `e004_soft_fault_commands_fault` · e_fault_recovery:64 | inv 10 → `0x13`, `0x0D`, Off + Flt_Clear (`control.cpp:333-338`) | V | Ordered-burst assertion. D2 |
| E-005 | `e005_recovery_clears_and_fsm_advances` · e_fault_recovery:74 | A boot-latched fault is cleared; the FSM reaches Active | N:ADC3#19 | Inverter plant that clears only on the Flt_Clear edge. D2, D3 |
| E-006 | `e006_fault_in_active_cuts_torque` · e_fault_recovery:99 | Fault while driving → 0 torque, reset word, → WaitInvStandby (`control.cpp:191-233`) | N:ADC3#19 | Plus the FSM transition, `inv_redrive_count` on `0x708`, resume on Ready. D2; wants plant M6 |
| E-007 | `e007_ams_error_suppresses_recovery` · e_fault_recovery:127 | In AmsError an inverter fault gets Off, never a reset word (`control.cpp:327`) | V | No `0x0D`/`0x13` frame over N cycles |
| E-001 | `e001_off_ready_torqueenable` · e_inverter:66 | `0x360` Off → Ready → TorqueEnable (`control.cpp:249-308`) | N:ADC3#19 | Also inv 0 and 13 follow-words (`control.cpp:290-298`). D3 |
| E-002 | `e002_torque_tracks_apps` · e_inverter:96 | `0x362` negated, tracks APPS, 0 below the deadband (`control.cpp:35-43`, `inverter.cpp:11-22`) | N:ADC3#19 | Exact Nm map incl. 5 %/90 % deadbands and the 3 % agreement gate. D4 |
| E-003 | `e003_inverter_fault_no_torque` · e_inverter:126 | Inverter fault in Active → no torque | N:ADC3#19 | Superseded by E-006 + burst. Duplicate ID |

### F: AMS gate (simulated and real AMS)

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| F-001 | `f001_ams_error_to_amserror` · f_ams:37 | `0x4A0[0]`=5 (fresh) → AmsError from any state, 0 torque (`control.cpp:150`, `control_task.cpp:120`) | V | Inject in every state; entry within one tick. Duplicate ID |
| F-002 | `f002_rearm_on_ams_ok` · f_ams:55 | AMS ok → leave AmsError to WaitInvVdcConfig (`control.cpp:235-236`) | V | Exact target state 0 |
| F-003 | `f003_stale_ams_fail_safe` · f_ams:72 | Stale AMS → not ok | V | `ok_precharge` gated within 200 ms ± 10 ms. Skipped on the bench; the skip reason is obsolete |
| F-005 | `f005_ams_emits_consumed_frames` · f_real_ams:64 | The real AMS emits `0x020`, `0x12C`, `0x4A0` | N:M3#11 | Contract both sides, co-simulated |
| F-001 | `f001_ok_precharge_tracks_real_ams` · f_real_ams:75 | ECU `ok_precharge` mirrors the real AMS `0x020` | N:M3#11 | Drive the AMS through precharge; the ECU ladder follows. Duplicate ID |
| F-004 | `f004_ams_stale_drops_ok_precharge` · f_real_ams:92 | AMS power cut → stale → not ok (`control_task.cpp:117-119`) | N:M3#11 | `0x504`=0 within 200 ms. D7: asserts the raw flag |

### G: plausibility

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| G-001 | `g001_t11_8_9_apps_disagreement` · g_plaus:75 | APPS disagree > 10 % for ≥ 100 ms → `t11_8_9`, torque 0; clears on agree (`control.cpp:59-73`) | N:ADC3#19 | Torque at 99 ms, cut at 100 ms. Placeholder thresholds |
| G-002 | `g002_ev_2_3_brake_and_throttle` · g_plaus:93 | EV.2.3 latch | N:ADC3#19 | **Obsolete (D1)**: replace with "brake + throttle does not cut" |
| G-003 | `g003_failed_apps_no_torque` · g_plaus:117 | APPS ≤ min → 0 % → no torque (`control.cpp:13-19,39-41`) | N:ADC3#19 | Plus an ADC conversion error and single-sensor failure (fault injection) |

### H: pit-diag

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| H-001 | `h001_enable_starts_stream` · h_pitdiag:46 | `0x7E0` DEADBEEF → ack `0x7E1`=1, 100 ms stream (`can_rx_task.cpp:79-90`, `control_task.cpp:377-396`) | V | Every `0x700`–`0x70D` at exactly 100 ms. Fails per IFS_HIL#128; stale ID list |
| H-002 | `h002_disable_stops_stream` · h_pitdiag:71 | `0x7E0`=0 → ack 0, stream stops | V | Zero frames after the next tick; `0x704` continues |
| H-003 | `h003_all_payloads_decode` · h_pitdiag:95 | Every pit-diag payload decodes; non-zero git hash | V | Decode against the generated `ecu.dbc` incl. `0x706`–`0x70D`. #128 |

### I: health

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| I-001 | `i001_health_present_and_cadenced` · i_health:44 | `0x704` at 1 Hz from DiagTask, free_heap > 0 (`diag_task.cpp:53-65`) | V | Exact 1000 ms; survives a ControlTask stall (fault injection) |
| I-002 | `i002_reset_cause_real` · i_health:60 | `reset_cause` is a known enum (`reset_cause.cpp:21`) | N:other (RCC_RSR model) | Power cycle → POR, `0x002` → SOFT, IWDG → IWDG, exactly |
| I-003 | `i003_all_tasks_live` · i_health:73 | All 5 task-liveness bits set | V | Per cycle, not OR'd; freeze one task → its bit clears. CanRx wakes every 100 ms on its own |
| I-005 | `i005_heap_no_slide` · i_health:97 | No heap leak over 5 s | V | Hours of virtual time, exact heap trace |

### J: soak and telemetry

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| J-001 | `j001_drive_soak` · j_soak:68 | 60 s in Active under varying APPS, no reset | N:ADC3#19 | Multi-hour soak with a pedal-profile plant. `uptime_s` saturates at 255 (ECU #242); the test says it wraps |
| J-002 | `j002_reflash_cycles` · j_soak:152 | Reflash ×2; each boots | B: endurance needs real flash; retries handle the mcp251x wedge | One deterministic A-003 is enough |
| J-001 | `j001_inverter_rpm_mirrors_to_pitdiag` ×4 · j_telemetry:48 | `0x463` erpm (20-bit signed) ÷ 10 → `0x702.inv_rpm` (`vehicle_service.cpp:96-105`, `pit_diag.cpp:185`) | V | Boundaries ±524287; staleness → 5500 rpm assumption. Duplicate ID |
| J-002 | `j002_inverter_temps_mirror_to_pitdiag` · j_telemetry:62 | `0x464` temperatures → `0x706` (−50 offset) (`vehicle_service.cpp:179-188`) | V | Plus the thermal-cap fields and the 0xFF sentinel |

### L: DV

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| L-001 | `l001_ts_active_tracks_precharge` · l_dv:120 | `0x504` = `ok_precharge` && AMS fresh (`control_task.cpp:117-119,333`) | V | Exact 200 ms stale edge. Supersedes F-003 |
| L-002 | `l002_brake_over_limit` · l_dv:140 | `0x505` = brake > 2500 (`control_task.cpp:334`) | N:ADC3#19 | 2500/2501 boundary |
| L-003 | `l003_motor_rpm` ×4 · l_dv:154 | `0x506` = erpm ÷ 10, s32 LE, 10 ms (`udv_tx.cpp:42-52`) | V | Cadence assertion. Overlaps J-001 (telemetry) |
| L-004 | `l004_refusal_without_ebs_braking` · l_dv:166 | `0x510` without brake > 2500 → no DV R2D (`control.cpp:174`) | N:ADC3#19 | Table-driven brake sweep |
| L-005 | `l005_dv_entry` · l_dv:180 | `0x510` + hard brake → R2D → Active with DV latch; `0x511`=1 | N:ADC3#19 | Plus RTDS and the exact 2 s dwell |
| L-006 | `l006_stale_dv_request_refused` · l_dv:202 | `0x510` older than 200 ms doesn't trigger (`control_task.cpp:152-153`) | N:ADC3#19 | Brake at t+199 ms enters, t+201 ms refused |
| L-009 | `l009_manual_precedence` · l_dv:216 | START + brake wins over DV; no latch | N:ADC3#19 | With the START GPIO. D3 |
| L-007 | `l007_torque_from_0x507` · l_dv:235 | DV-Active: `0x507` % → torque (`control.cpp:83-85`) | N:ADC3#19 | Firmware map, known caps via `0x463`/`0x464`. D5, D6 |
| L-008 | `l008_stale_torque_zero_no_apps_fallback` · l_dv:250 | Stale `0x507` (> 100 ms) → 0 torque, no APPS fallback | N:ADC3#19 | Exact 100 ms edge. D5 |
| L-010 | `l010_conditioner_failsafes` · l_dv:276 | `0x507` clamp < 0 → 0, > 100 → 100, deadband → 0 (`vehicle_service.cpp:107-115`) | N:ADC3#19 | Unit-level plus a capped chain. D5, D6: can't pass |
| L-011 | `l011_ev23_exemption` · l_dv:298 | DV torque not cut by hard braking | N:ADC3#19 | Keep the torque assertion; drop `ev_2_3` (D1) |
| L-012 | `l012_cycle_exit_clears_latch` · l_dv:318 | Leaving the drive cycle clears the DV latch (`control.cpp:21-30`) | N:ADC3#19 | Plus: the latch persists across a re-drive. D3 |
| L-013 | `l013_ams_error_preempts_dv` · l_dv:346 | AmsError from DV-Active → Off, 0 torque, `0x511`=0 | N:ADC3#19 | Within a single tick |
| L-014 | `l014_udv_traffic_does_not_hold_ams_fresh` · l_dv:366 | uDV frames don't refresh `last_ams_tick` (`vehicle_service.cpp:270-281`) | V | Exact 200 ms edge; documents D7 |

## Gaps (firmware behaviour no IFS_HIL ECU test covers)

1. ⚠ **A silent inverter in Active is not detected.** `inv_present` is computed (`control_task.cpp:92`) but unused by `Controller::step`. If `0x461` stops in state 4 or 6, the ECU keeps commanding torque.
2. **Precharge 10 s timeout and re-entry** (`control.cpp:161-163`).
3. **RTDS output:** 2 s R2D pulse; AS-Emergency 150 ms pulses for 10 s (`as_buzzer.cpp`, `control.cpp:179-183,256-258,359-363`).
4. **Active → WaitInvStandby re-drive** with `inv_redrive_count` (`0x708`); the inv 0/13 follow-words (`control.cpp:191-233,290-298`).
5. **Torque limiter chain:**
   - power envelope, with 5500 rpm assumed when stale (`power_limit.cpp`)
   - cell derate with IR compensation (`cell_derate.cpp`)
   - motor thermal cap, 60 % when the temperature is unknown (`motor_thermal.cpp`)
   - pack thermal cap from `0x136`/`0x137` (`pack_thermal.cpp`)
6. **AmsError exits when the AMS goes silent.** `ams_error` requires `ams_fresh` (`control_task.cpp:120`); confirm this is intended.
7. **The Vdc gate latches forever** after one `0x466` (`control_task.cpp:93`).
8. **BL reboot refused in drive states:** `reboot_allowed_in`, `g_boot_trigger_refused`, `0x704 boot_refused`. The refusal path is testable now.
9. **IWDG:** only ControlTask kicks it (`control_task.cpp:424`); a stall → IWDG reset → reset cause IWDG.
10. **Fault latch across reset:** HardFault, MemManage, stack overflow or malloc failure → BKP1R → `0x704 last_fault` (`stm32h7xx_it.c:93-138`, `freertos.c:151,168`). The deferred I-004.
11. **Calibration NVM session** (`0x7E2`–`0x7E5`): `vehicle_safe` gate, commit/persist, torn-record fallback, `cal_status` (`cal_session.cpp`, `pedal_cal_nvm.cpp`, `control_task.cpp:202-270`).
12. **DC-link discharge:** `0x021` → PB6, release on measured voltage, 30 s timeout, `0x100.discharge_engaged` (`discharge.cpp`, `control_task.cpp:282-303`).
13. **Dash FDCAN3** `0x510`–`0x521` every 200 ms (`telemetry_task.cpp:56-171`); nRF24 snapshot (needs an SPI/GPIO model).
14. **GPS:** NMEA on USART10 → `0x508`/`0x509` every 200 ms (`gps_task.cpp`, `gps_nmea.cpp`, `gps_tx.cpp`).
15. **TX overload:** the software queue drops into `tx_dropped` (`can_tx_task.cpp:59-72`); the `HAL_FDCAN_AddMessageToTxFifoQ` return is ignored (`:52`), so a full FIFO drops silently. A candidate cause for IFS_HIL#128 (`0x703` posted late among ~20 frames per tick).
16. **START debounce** 5 × 10 ms (`io_signals.hpp:33-43`); AmsError status LEDs (`control_task.cpp:372-375`).
17. **RX robustness:** short-DLC rejection in every parser; `0x463` 20-bit sign extension at its limits.

## Counts

| Class | Count |
|---|---|
| virtual | 26 |
| virtual-needs: ADC3#19 | 21 |
| virtual-needs: M3#11 | 3 |
| virtual-needs: bootloader#4 | 3 |
| virtual-needs: fault-injection | 1 |
| virtual-needs: other (RCC reset flags) | 1 |
| physical-only | 1 |
| bench-artifact | 1 |
| **Total** | **57** |

**Likely failing on firmware `dev` because of drift:** E-003…E-006 (fault recovery), E-002, G-002, F-004 (real AMS), L-007, L-010, and H-001/H-003 (#128). F-003 is skipped.
