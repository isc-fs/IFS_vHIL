# Appendix B: IFS_HIL AMS tests (`tests/hil/ams/`) and bench self-tests

Part of [the IFS_HIL test analysis](ifs-hil-tests.md).

**Versions analysed:**
- IFS_HIL `origin/dev` @ `fa11a25`: 139 AMS test functions (≈146 cases).
- AMS firmware `main` @ `df1c961`.

**Firmware path keys** (relative to `Core/`):

| Key | Path |
|---|---|
| CFG | `Inc/app/ams_config.hpp` |
| SP | `Inc/app/safety_predicates.hpp` |
| SM | `Inc/app/state_machine.hpp` |
| ST | `Src/app/safety_task.cpp` |
| VS | `Src/app/vehicle_service.cpp` |
| ACU | `Src/app/acu_can_task.cpp` |
| BL | `Inc/app/bootloader.hpp` |
| BMS | `Src/app/bms_service.cpp` |
| BPT | `Src/app/bms_poll_task.cpp` |
| BC | `Inc/app/balance_controller.hpp` |
| AIT | `Src/app/app_init_task.cpp` |
| CT | `Src/app/current_task.cpp` |
| MAIN | `Src/main.c` |

**Classes:**

| Code | Class |
|---|---|
| V | `virtual` |
| N:x | `virtual-needs:x` |
| P | `physical-only` |
| B | `bench-artifact` |

## Drift between the suite and the firmware

| Finding | Affects |
|---|---|
| **Balancing is inverted.** With `0x103` never seen, or stale for more than 5 s, the effective command is **Off**, not Auto (`VS:110,115`, `CFG:502-511`). `0x6C0[2]` means "effective = Off" (`ACU:315-320`). | BAL-01 and BAL-04 contradict main. BAL-02, -03 and -05 fail in their shared setup. |
| **Over- and under-temperature faults are gated off.** `TempFaultsTrusted = false` (`CFG:125`, gate at `SP:227`). | B-026c, B-029-OT and K-100 expect an Error that never comes. |
| **A silenced chain reports BmsModuleOffline (2).** The mask drops after 350 ms (`BMS:313-321`), and reason 2 (undebounced, `SP:176`) is checked before BmsStale (`SP:182`). | B-029-stale asserts 3. |
| **A TSMS drop in Charge latches.** The firmware latches ChargerTsmsOpen (15) (`SM:231`, `SP:68`). | C-039c expects a return to Start. |
| **The charger precharge timeout reports ChargerStale.** It fires first, as 14 (`SP:262-264`), not FsmError (12). | C-037c (also skipped on the bench). |
| **The current limit is 185 A** (`CFG:147`). The profile says 200 A (`ams_profile.yaml:138-139`). | J-102 trips at 190 A. The I-100/N-003 sweeps latch reason 10. |
| **Profile values have drifted.** `bms_stale_ms` is missing (tests use 1500 ms; the firmware uses 350, `CFG:162`). The cell debounce is ~250 ms, not 300 (`CFG:226`). | B-022 window; A-011's DLC list omits `0x130` and `0x021`. |
| **Pit-diag frames need arming.** `0x6C0`–`0x6C9` are only sent after a `0x7F0` (`ACU:177-180`). | Tests that read them must arm first. |

**Hidden ADC3 dependency.** Every test expecting a healthy Start or AMS_OK high after the 2 s grace also needs fresh pack-current samples: CurrentStale fires at 200 ms (`SP:241`), and the OUT_P leg must sit in 700–2300 mV (`CT:194-215`). Until #19 those rows can't show a healthy Start. They are marked † below.

## Blocks A–F

### A: boot

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| A-001 | `a001_relays_open_within_50ms` · a:52 | PB4..PB7 driven LOW at GPIO init, never HIGH before Start (`MAIN:711`) | V | GPIO write history from reset to the first `0x4A0`. The input-Z transient is electrical. |
| A-002 | `a002_bl_discover` · a:125 | Bootloader answers DISCOVER with node 0x02 | N:bootloader#4 | — |
| A-003 | `a003_flash_and_jump` · a:183 | Bootloader flashes, verifies and jumps; app comes up | N:bootloader#4 | — |
| A-004 | `a004_first_telemetry_is_start` · a:225 | First `0x4A0` reports Start (`ST:137`) | V | First frame at ~500 ms of virtual time |
| A-005 | `a005_module_mask_and_cell_v` · a:250 | Mask 0x1F, min/max cell | V | Covered today by `ams_smoke`. Bench failures came from Pico #116. |
| A-006 | `a006_pack_voltage` · a:284 | `0x4A1` pack voltage = sum of cells | V | One cell +100 mV → pack +100 mV. Current field after #19. |
| A-007 | `a007_temps` · a:310 | `0x4A2` min/max/avg temperature | V | Covered today. Pico #117 showed a +9 °C error. |
| A-007 | `a007_heartbeat_increments` · a:325 | Heartbeat +1 per frame (`ST:407`) | V | Duplicates part of B-010 |
| A-008 | `a008_cadence_60s` · a:346 | 500 ms telemetry (`CFG:260`) | V | Every delta 500 ± 10 ms over 60 s virtual. Marginal on the bench. |
| A-009 | `a009` · a:395 | fwinfo `reserved[0]` = node id (`CFG:1022`) | V | Static .bin check |
| A-010 | `a010` · a:437 | Cockpit byte 0x80 in Start (`ST:388-393`) | V | — |
| A-011 | `a011` · a:480 | TX DLCs per contract (#238) | V | List omits `0x130` and `0x021` |
| A-012 | `a012` · a:522 | FDCAN1 hardware filter rejects extended IDs (`AIT:103-107`) | V | Only if the Renode FDCAN model honours the global filter; verify first |
| A-013 | `a013` · a:590 | `0x6C6` semver, git hash, node id | V | Arm pit-diag; compare with VERSION and `-DGIT_HASH` |
| A-014 | `a014` · a:672 | `0x4A4` at 100 ms, reserved bytes zero (`ST:415`) | V | 10 frames/s; byte0 0x00 during grace |

### B: safety

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| B-010 | `b010_heartbeat_advances_at_500ms_for_60s` · b:76 | No main-loop stall | V | Exactly 120 in 60 s virtual |
| B-022 | `b022_bms_stale_trips_error` · b:119 | Silent chain → Error (`SP:176`) | V | Silence all 10 chips; Error within ~410 ms. Duplicates E-065. |
| B-021 | `b021_vcu_stale_trips_error` · b:155 | Car lock + `0x100` silent for more than 200 ms → Error (`SP:252-254`) | V | Error at 200–210 ms |
| B-027 | `b027_pre_lock_silent_vcu_stays_start` · b:184 | VcuStale only in Car mode (`ST:205`) | V | Silent VCU stays in Start; Car lock → reason 11, mode 1 |
| B-026a | `b026_cell_overvoltage` · b:247 | Cell above 4200 mV → Error (`SP:218`) | V | — |
| B-026b | `b026_cell_undervoltage` · b:256 | Cell below 2800 mV → Error (`SP:214`) | V | 2790 mV → reason 4 after the debounce |
| B-026c | `b026_cell_overtemperature` · b:272 | Over-temperature → Error | V | Must **not** trip while gated; must trip in a trusted build. Drift. |
| B-028 | `b028_transient_dip_does_not_latch` · b:302 | One-poll dip doesn't latch (`SP:306`) | V | Exactly one 200 ms poll |
| B-028 | `b028_sustained_dip_latches` · b:327 | Sustained under-voltage latches | V | 240 ms vs 260 ms boundary |
| B-029 | `b029_no_fault_reads_zero` · b:349 | Reason 0 when healthy | V | — |
| B-029 | `b029_cell_undervoltage_reason` · b:358 | Reason 4 + module detail | V | Module 3 → (4, 3) |
| B-029 | `b029_cell_overvoltage_reason` · b:374 | Reason 5 | V | — |
| B-029 | `b029_cell_overtemp_reason` · b:387 | Reason 7 | V | Drift: gated |
| B-029 | `b029_bms_stale_reason` · b:402 | Reason for a silent chain | V | Expect 2 + mask. Drift: the test asserts 3. |
| B-029 | `b029_vcu_stale_reason` · b:417 | Reason 11 | V | — |
| B-030 | `b030_wrong_magic_ignored` · b:467 | `0x101` without "CHRG" ignored (`VS:50-57`) | V | — |

### BAL: balancing

DCC bits are read straight from the LTC6811 models.

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| BAL-01 | `b01_autonomous_balancing_in_charge` · bal:90 | Auto balances in Charge (`BC:113`) | V | Send "BALX", Charge, +100 mV cell → DCC set. Drift: absent → Off. |
| BAL-02 | `b02_balo_suppresses` · bal:105 | "BALO" clears all DCC | V | Setup fails on main |
| BAL-03 | `b03_balx_resumes` · bal:123 | "BALX" resumes | V | DCC set again within 800 ms |
| BAL-04 | `b04_stale_reverts_to_auto` · bal:144 | Stale `0x103` → ? | V | Expect **Off** after 5 s. Drift. |
| BAL-05 | `b05_wrong_magic_ignored` · bal:167 | Unknown payload keeps the previous command (`VS:75`) | V | — |
| BAL-06 | `b06_balo_no_effect_outside_charge` · bal:193 | Auto inactive outside Charge; FSM and AMS_OK unaffected | V | Add a BALN-in-Run case |

### C: state machine

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| C-030 | `c030_tsms_only` · c:142 | TSMS without a DASH_CHG edge stays in Start (`SM:274`) | V | — |
| C-031 | `c031_dash_chg_only` · c:163 | Press without TSMS stays in Start | V | — |
| C-032 | `c032` · c:189 | TSMS + edge → Precharge, Car lock (`ST:288-306`) | V | Precharge within 20 ms |
| C-032b | `c032b_held_dash_does_not_fire` · c:234 | DASH held from boot is not an edge (`ST:152-153`) | V | GPIO high before reset |
| C-033 | `c033` · c:295 | Bus ≥ 95 % → Transition → Run (`SM:117,320-330`) | V | 94 % holds, 95 % runs. A plant (M6) lets the bus follow the relays. |
| C-034 | `c034_car_precharge_timeout` · c:329 | Precharge longer than 5 s → Error 12 (`SM:300`) | V | At 5000–5020 ms |
| C-039a | `c039a_tsms_drop_rearms` · c:369 | TSMS drop in Run → Start, unlatched, AMS_OK held (`SM:220-240`) | V | Then re-arm; `rearm_permitted` (`SM:163`) |
| C-037 | `c037_charger_entry` · c:418 | VCU absent + fresh `0x101` → Charger; PRE never closes (`SM:276-282`) | V | PB7 write history never HIGH |
| C-037b | `c037b_charger_proceeds_to_charge` · c:460 | Charger proceeds on `0x101` freshness (`SM:320-324`) | V | Duplicates C-037 |
| C-037c | `c037c_charger_stale_request_times_out` · c:483 | AIR+ never onto an unplugged charger | V | Expect reason **14** at ~T+1 s. Drift; bench skip. |
| C-038 | `c038_dead_vcu_no_request_locks_car` · c:542 | No VCU + no `0x101` → Car → VcuStale (`ST:302`) | V | — |
| C-039c | `c039c_charge_survives_dash_release` · c:585 | Charge ignores DASH; TSMS drop → ? | V | Expect reason **15**. Drift: the test expects Start. |
| C-043 | `c043` · c:623 | Error is sticky within a boot (`SM:192`) | V | Still Error 60 s after the cause clears |
| C-039b | `c039b_run_survives_dash_release` · c:678 | Run ignores DASH (`SM:380-384`) | V | — |
| C-041 | `c041_mode_locked_retained_through_error` · c:720 | Mode latch kept in Error | V | — |
| C-042 | `c042_cockpit_byte_per_state` · c:774 | Cockpit byte per state; bit0 = live DASH (`ST:388-393`) | V | — |
| C-045 | `c045_no_inputs_no_transition` · c:880 | Undriven inputs stay in Start; pull-downs (`MAIN:723-725`) | V | Assert PUPDR. Floating inputs are electrical. |
| C-046 | `c046_ams_ok_low_in_grace` · c:926 | AMS_OK LOW for the first 2000 ms (`SP:283`) | V | PB4 never HIGH before 2000 ms |
| C-047 | `c047_ams_ok_high_when_healthy` · c:948 | AMS_OK HIGH after grace (`ST:371`) | V | PB4 HIGH at 2000–2010 ms † |
| C-048 | `c048_ams_ok_drops_on_error` · c:965 | AMS_OK LOW within 10 ms of Error (`ST:85`) | V | Same tick as the Error state |
| C-049 | `c049_bus_collapse_to_start_rearms` · c:1023 | Bus below 50 % for 200 ms in Run → Start (`ST:268-278`, `SM:373`) | V | — |
| C-050 | `c050_brief_dip_stays_run` · c:1066 | Dip shorter than the debounce stays in Run | V | 190 ms stays, 210 ms → Start |

### CAN1M: 1 Mbit/s (module skipped; reverted by AMS #351)

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| M-01 | `m01_telemetry_decodes_at_1mbps` · m:73 | Telemetry clean at 1 Mbit/s | P: bit timing | — |
| M-02 | `m02_dc_bus_rx` · m:113 | Standard `0x100` updates dc_bus | V | Duplicates A-012 |
| M-02 | `m02_charger_lock` · m:143 | Charger lock | V | Duplicates C-037 |
| M-03 | `m03_pit_diag_grid_and_utilisation` · m:162 | Pit-diag grid present, bus load below 1 % | V | Count per second of virtual time |
| M-04 | `m04_error_counters_clean` · m:192 | No CAN errors | P: error counters | — |
| M-05 | `m05_reboot_and_reflash` · m:214 | Trigger + reflash | N:bootloader#4 | Soak |
| M-06 | `m06_zero_errors_5min` · m:260 | Sample-point compatibility | P: bit timing | Soak |

### D: bootloader trigger

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| D-051 | `d051` · d:95 | `002#B007AD11` in Start → BKP0R magic + reset (`ACU:559-569`, `BL:67`) | V | BKP0R write `0xB00710AD` + reset |
| D-051b | `d051b_trigger_works_from_error` · d:144 | Trigger honoured in Error (`BL:61`) | V | Add: refused in Run |
| D-050 | `d050` · d:197 | Cold boot: bootloader → app in under 2 s | N:bootloader#4 | — |
| D-045 | `d045` ×4 · d:225 | Wrong ID/DLC/payload/bus doesn't reboot (`BL:67-73`) | V | The wrong-bus case needs FDCAN2 modelled |
| D-052 | `d052_jump_reason_in_pit_diag` · d:285 | `0x6C4[0..3]` = CanTrigger after a trigger reboot | N:bootloader#4 | A bootloader stub that just jumps would do |

### E: LTC chain

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| E-060 | `e060` · e:64 | Discovery → mask 0x1F | V | Duplicates A-005 |
| E-061 | `e061` · e:85 | Voltage poll under 50 ms, no drift (`BPT:834`) | V | `0x6C1` over 60 s (emulated SPI time) |
| E-063 | `e063` · e:123 | Per-IC PEC counts 0 (`BMS:266`) | V | Fails on the bench (IFS_HIL#44) |
| E-064 | `e064` · e:145 | Temperature-sweep fail mask 0 (`BPT:790`) | V | Pico #135 could retarget the mux |
| E-065 | `e065` · e:172 | Silent chain → Error | V | Duplicates B-022 |
| E-066 | `e066_module2_only` · e:207 | Module 2 silent → mask 0x1B → Error | V | Silence chips 4 and 5; reason 2, detail 0x1B |
| E-067 | `e067_pec_localisation` · e:277 | PEC fault counted only on the faulty chip | V | Blocked on the bench by #44 |

### F: relays

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| F-060 | `f060` · fr:98 | AIR− closes on Start → Precharge (`SM:278-282`) | V | PB6 HIGH in the same FSM step |
| F-061 | `f061` · fr:119 | AIR+ closes at Transition | V | — |
| F-062 | `f062` · fr:137 | PRE closes (Car) | V | — |
| F-063 | `f063` · fr:160 | AMS_OK HIGH except in Error | V | Duplicates C-047/C-048 |
| F-064 | `f064` · fr:199 | AIR−=1, PRE=1, AIR+=0 | V | Atomic pattern |
| F-065 | `f065` · fr:228 | AIR−=1, AIR+=1, PRE=0 (`SM:327`) | V | PRE never overlaps AIR+ for more than one step |
| F-066 | `f066` · fr:254 | Error opens all three relays (`ST:84`) | V | Same tick as the latch |

### F: flash endurance (all soak)

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| F-070 | `f070_cold_soak` · ff:255 | 100 power cycles boot cleanly | P: power, real bootloader boot | — |
| F-071 | `f071_can_trigger_soak` · ff:334 | Trigger → flash → jump ×100 | N:bootloader#4 | One cycle suffices when deterministic |
| F-072 | `f072_cross_trigger_mix` · ff:393 | Cold and trigger reboots alternated | N:bootloader#4 | The cold leg is physical |
| F-073 | `f073_crc_integrity` · ff:472 | Readback CRC every cycle | P: flash integrity/wear | — |
| F-074 | `f074_bus_busy_flash` · ff:538 | Flashing under bus load | N:bootloader#4 | 200 frames/s of filler |
| F-075 | `f075_mixed_version_round_trip` · ff:617 | `0x6C6` matches the flashed image | N:bootloader#4 | — |
| F-076 | `f076_hil_clear_set` · ff:715 | HIL_CLEAR build clears the latch on boot (`AIT:68-86`) | N:other (backup domain across warm reset) | Always skipped on the bench |
| F-076 | `f076_hil_clear_unset` · ff:730 | Flight build restores Error after a warm reset (`ST:130-135`) | N:other (same) | — |
| F-077 | `f077_interrupted_flash_recovery` · ff:802 | Power cut mid-flash recoverable | P: power loss mid-write | — |
| F-078 | `f078_power_off_duration` ×5 · ff:947 | Clean boot for any off duration | P: backup-domain decay | — |
| F-079 | `f079_discover_latency_long_soak` · ff:1011 | DISCOVER never missed, latency stable | B: latency is can-flasher process time | The zero-miss half goes to `ams-bootloader` |
| F-080 | `f080_trigger_from_error` · ff:1099 | Trigger from Error ×20 | V | Duplicates D-051b |
| F-081 | `f081_bench_noise_immunity` · ff:1189 | Filler causes no reboot; the trigger still works | V | 200 frames/s on `0x500`..`0x5FF` for 60 s |

## Blocks G–U

| ID | Test · line | Behaviour verified | Class | Native idea · notes |
|---|---|---|---|---|
| E-050 | `e050` · g_soak:132 | Idle Start 30 min: state, 500 ms cadence, heartbeat (`ST:407`) | V | 30 min virtual, zero outliers. Soak † |
| E-051 | `e051` · g_soak:147 | Same invariants in Run | V | Scripted TSMS/DASH/bus; soak † |
| E-052 | `e052` · g_soak:177 | 50 cold boots: first `0x4A0` in time, Start | V | Exact grace timing; decide whether BKP survives reset. Soak. |
| G-097 | `g097_boot_diag` · g_soak:264 | `0x6C4`: jump reason 0, init progress 7, FDCAN1 start OK (`AIT:112-165`) | V | Plus a warm reset with BKP2R magic → non-zero jump reason |
| G-102 | `g102_pec_localisation` · g_soak:303 | Silencing ICs 2 and 3 raises only `0x6C7[2..3]` (`ACU:370-372`) | V | Exact deltas, zero collateral. Duplicates E-067. |
| I-100 | `i100_..._tracks_injection` · i_current:77 | `0x4A1` filtered_mA tracks the injection (`current_service.cpp:73-88`) | N:ADC3#19 | Exact against `adc_to_mA`; IIR settling. Gain calibration stays physical. Stale: ±200 A > 185 A. |
| I-101 | `i101_zero_..._zero` · i_current:109 | 0 A reads ~0 | N:ADC3#19 | Offset is physical. Overlaps M-042. |
| J-100 | `j100_overcurrent_trips_error` · i_current:129 | \|I\| > CurrentMaxMa → reason 10, no debounce (`SP:244`) | N:ADC3#19 | Trip time on the IIR curve within one 50 ms tick |
| J-101 | `j101_..._sticky` · i_current:153 | Over-current latch holds (`error_latch.cpp:12-23`) | N:ADC3#19 | Plus reset behaviour |
| J-102 | `j102_under_limit_...` · i_current:176 | Under the limit doesn't trip | N:ADC3#19 | ±1 LSB around 185 A. **Fails on main** (190 > 185). |
| J-132 | `j132_no_spurious_recovery` · j_busoff:70 | `busoff_recovery_count` stays put on a healthy bus | V | 60 s virtual |
| J-130 | `j130_busoff_is_recovered` · j_busoff:81 | Bus-off → Stop/Start after `FdcanBusOffRetryMs` (`ACU:150-175`) | N:other (FDCAN bus-off hook) | Skipped on the bench |
| J-131 | `j131_recovery_count_increments` · j_busoff:98 | Count += 1 per recovery | N:other (same) | Skipped |
| K-100 | `k100` · k_encoders:62 | `0x4A0` consistent across Start/Precharge/Run/Error | V | Reach Error via cell UV. **Broken on main** (OT gated). |
| K-101 | `k101` · k_encoders:150 | `0x4A1` pack mV + filtered_mA | N:ADC3#19 | Empty `@skip` (#189); fold into I-100 |
| K-102 | `k102_cockpit_byte_sentinel_set` · k_encoders:165 | `0x4A2[5]` bit-7 sentinel | V | Plus TSMS/DASH bit mapping |
| K-103 | `k103` · k_encoders:188 | Heartbeat +1 mod 256 over 1000 frames | V | Duplicates A-007/E-050 |
| M-040 | `m040_boot_scheduler_cadence` · m:41 | Reaches Start, cadence | V | Redundant with E-050/A † |
| M-041 | `m041_all_tasks_producing` · m:54 | `0x4A0`/`0x4A1`/`0x4A2`/`0x135`/`0x12C` all present | V | Plus `0x4A4`, `0x6CA`, `0x130` † |
| M-042 | `m042_adc_dualcal_and_readback` · m:66 | ADC3 single and differential calibration completes; ~0 A (`CT:180-182`) | N:ADC3#19 | Duplicates I-101 |
| N-001 | `n001_outp_disconnect_latches_reason8` · n_disconnect:58 | OUT_P < 700 mV ×3 reads → reason 8 (`CT:194-215`) | N:ADC3#19 | Exactly the 3rd 50 ms read; 2 bad + 1 good doesn't trip. Pull-down on a real open pin stays physical. |
| N-002 | `n002_disconnect_latch_sticky` · n_disconnect:74 | Reconnect doesn't clear reason 8 | N:ADC3#19 | Merge with J-101 |
| N-003 | `n003_normal_range_no_false_trip` · n_disconnect:90 | In-range current never gives reason 8 | N:ADC3#19 | Stale: ±190 A trips reason 10 first |
| N-004 | `n004_outn_open_is_overlimit` · n_disconnect:106 | OUT_N open → reason 10, not 8 (`SP:240-244`) | N:ADC3#19 | — |
| R-110 | `r110_start_all_open_amsok_high` · r_relays:53 | `0x4A4` relays 0 in Start, AMS_OK 1 | V | `0x4A4` vs the GPIO ODR † |
| R-111 | `r111_ams_ok_bit_tracks_pit_diag` · r_relays:61 | `0x4A4` bit 3 = `0x6C0[3]`; drops on UV | V | † |
| R-112 | `r112_car_precharge_closes_resistor` · r_relays:78 | Car: AIR− + PRE; Run: AIR+, PRE open | V | Timestamped GPIO edge log † |
| R-113 | `r113_charger_precharge_is_skipped` · r_relays:94 | Charger never closes PRE (`SM:251-258`) | V | A GPIO trace beats 100 ms frame sampling † |
| R-114 | `r114_run_steady_both_airs_closed` · r_relays:110 | Run: both AIRs closed | V | Merge into R-112 † |
| R-115 | `r115_error_opens_all_contactors` · r_relays:119 | Error opens everything | V | Within 1 SafetyTask tick (10 ms) |
| S-140 | `s140_card_in_boots_safety_runs` · s_sdcard:49 | Card in: boots, Start, AMS_OK high | V | † |
| S-141 | `s141_no_boot_loop` · s_sdcard:55 | Card in: no boot loop | N:SDMMC-IDMA | Vacuous until writes work |
| S-144 | `s144_boot_time_no_sd_stall` · s_sdcard:70 | First telemetry < 4 s; SD init off the boot path (`MAIN:222-310`) | V | With and without a card |
| S-142 | `s142_bl_recovery_no_card` · s_sdcard:80 | No card: the `0x002` trigger still reaches the bootloader | N:bootloader#4 | Operator-gated today |
| S-143 | `s143_no_card_boots` · s_sdcard:93 | No card: boots, AMS_OK high | V | Removes a manual card pull. Note: Renode needs CMDSENT behaviour (see CLAUDE.md invariant 8). |
| T-150 | `t150_logger_no_maintask_impact` · t_sdlogger:42 | Logger never stalls `0x4A0` | N:SDMMC-IDMA | SD write latency in the model |
| T-151 | `t151_log_content_manual` · t_sdlogger:69 | CSV: 314 columns, 4 Hz, rotation, `.TMP` → `.CSV` | N:SDMMC-IDMA | Parse the card image against seeded cells and temperatures. Manual today. |
| T-152 | `t152_power_loss_durability_manual` · t_sdlogger:77 | Power cut mid-write → clean remount, ≤ 1 s lost | N:SDMMC-IDMA | Reset at a random virtual tick. Card FTL stays physical. |
| U-160 | `u160_mask_stable_across_reboots` · u_multirun:120 | N boots × 60 s logging; mask holds | N:SDMMC-IDMA | 3 resets × 60 s |
| U-161 | `u161_file_per_run_no_fragmentation` · u_multirun:177 | One sealed file per run (AMS#495) | N:SDMMC-IDMA | Parse the image |
| U-162 | `u162_no_truncation_and_sealed` · u_multirun:214 | Increasing indices, `.CRC` per `.CSV` (#448) | N:SDMMC-IDMA | Check CRC values |

## Bench self-tests (`tests/hil/test_*.py`)

All of these test the backplane, not firmware. Classed **B**, with no vHIL equivalent.

| File | What it tests |
|---|---|
| `test_can.py` | 3× MCP2515 init, loopback, counters, INT |
| `test_i2c.py` | I²C scan, INA226 IDs, TCA9555 |
| `test_spi_adc.py` | 3 SPI ADCs |
| `test_spi_dac.py` | 4× DAC80504 |
| `test_mlc_power.py` | INA226 per slot |
| `test_relays.py` | K1–K4 relays |
| `test_nrf24.py` | nRF24 presence and FIFO |
| `test_example.py` | Placeholder (delete) |

## Gaps (AMS behaviour no IFS_HIL test covers)

- **Predicate boundaries and order:**
  - faults present before grace latch exactly at 2000 ms (`SP:170`)
  - 2799/2800 and 4200/4201 mV
  - exact debounces
  - BmsModuleOffline before BmsStale
  - the 95 % precharge ratio
  - VcuFreshMs and ChargeReqFreshMs edges
- **Open-wire (ADOW, reason 16):** interior and endpoint conductors, retry within a poll. Needs an open-wire model.
- **Temperature:**
  - disconnect (reason 13): a channel that was valid and then reads open faults in under 360 ms; one that was never valid doesn't
  - OT/UT for both values of `TempFaultsTrusted`
- **Tap-artifact guard** (`CFG:109-116`): a real UV/OV next to a normal cell still faults.
- **Balancing:** BALN; `0x104` masks and their dead-man; 800 ms update; 50/20 mV hysteresis; max 8 per module; no adjacent cells; 50 °C lockout; no balancing after a cell-data fault; DCC quiesce before ADCV (visible in the LTC model).
- **Charger:** ChargerStale (14), ChargerTsmsOpen (15), PRE skipped.
- **Re-arm interlock and guards:** re-arm blocked by `discharge_engaged` or `dc_bus_valid=0`, with the press consumed (`SM:163`, `VS:33-40`); bus slump at Transition → Error (`SM:343`).
- **Reset path:**
  - error latch across a warm reset (flight build) vs. the HIL_CLEAR build
  - boot trigger refused while energised, and counted on `0x6C0`
- **isoSPI:**
  - link cut after chip k → downstream mask, reason 2 with detail, Error stays latched after restore, mask/PEC recovery
  - PEC retry policy: one bad attempt is absorbed by 2 retries (`CFG:939`, `BPT:515`)
- **Current [ADC3]:**
  - CurrentStale (200 ms); the DC-DC channel
  - over-current trip-time curve (200 A → 2.1 s, 400 A → 0.5 s, `CFG:134-140`)
  - SOC on `0x130`: Kalman, 0xFF when unknown, re-anchor after 5 min at rest, invalidated on a > 500 ms gap
- **SD logger:**
  - exact column values; 4 Hz; 5 min and 4 MiB rotation; `next_index` resume; 9999 ceiling
  - card full; card pulled and remounted at runtime (PE3)
  - LOGFS-over-CAN (IFS_HIL #406/#452)
- **IMU:** BMI088 on I²C2; a missing or failing IMU never faults; retry every 1 s. Needs a BMI088 model.
- **Diagnostics:** pit-diag arm/ack (off after every boot); `0x6C5` post-mortem; `0x6CA` at 1 Hz unarmed; `0x6C9` TX-fail count; `0x6C1` timing.
- **Watchdog:** IWDG when SafetyTask stops refreshing; refresh while in Error.

## Counts

| Class | A–F | G–U | AMS total |
|---|---|---|---|
| virtual | 79 | 20 | 99 |
| virtual-needs: ADC3#19 | — | 11 | 11 |
| virtual-needs: bootloader#4 | 9 | 1 | 10 |
| virtual-needs: SDMMC-IDMA | — | 7 | 7 |
| virtual-needs: other (FDCAN bus-off; backup domain) | 2 | 2 | 4 |
| physical-only | 7 | 0 | 7 |
| bench-artifact | 1 | 0 | 1 |
| **Total** | **98** | **41** | **139** |

**Contradict AMS main today:** B-026c, B-029-OT, B-029-stale, BAL-01…05, C-039c, C-037c, J-102, K-100.
