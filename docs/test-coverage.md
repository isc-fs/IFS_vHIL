# Test coverage of the vHIL's own suite

**Date:** 2026-10-08 · **Firmware:** AMS `main` @ `df1c961`, ECU `dev` @ `544b651`,
CAN bootloader `v1.7.0` (the refs `systems/*.yaml` declare) · **Suite:**
`tests/sim` (native tests) and `tests/scenarios` (every committed scenario).

> The catalogue's AMS has since moved to `dev` (`catalog/firmware/ams.yaml`,
> #208). The figures below were measured on AMS `main` and are not re-run;
> on `dev` the suite reads the split-rate `.BIN` logs and the 331-column
> `LOG.CSV`, and the AMS #553, #599 and #604 strict xfails are gone (fixed
> there).

The physical HIL is one bench; the vHIL's own suite is meant to stand on its
own. This page measures how much of the firmware it runs, maps the
firmware's requirements to the tests that check them, and lists what is
left: what the suite could still test, ranked by safety, and what it can't
test at all.

## 1. How it was measured

`--vhil-coverage` (`vhil/coverage.py`,
[setup.md](development/setup.md#firmware-coverage)) logs every
translation block Renode runs and maps it to functions and DWARF lines.
Hits are 0/1. The suite ran in Docker in slices, one coverage directory
each, and was merged with `python -m vhil.coverage <dirs> -o <out>`:

```sh
# each slice: python -m pytest <files> --vhil-coverage results/cov/<slice>
python -m vhil.coverage results/cov/* -o results/cov-merged
```

`soak`-marked tests (opt-in, `pytest.ini`) were not run. Tests that Renode
lost to the VM's OOM killer were re-run on their own; every baseline test
passed or xfailed as committed.

**Application code** is the firmware's own `Core/` sources (the `Core/Inc`
headers included, `FATFS/Target` for the AMS's SD glue). It leaves out the
HAL, CMSIS, FreeRTOS, FatFs' core, newlib and libstdc++. The CubeMX device
init (`main.c`, `gpio.c`, `*_hal_msp.c`, …) is counted apart. Each area
carries a **safety weight**:

- **3:** the safety function itself.
- **2:** safety-related: the CAN contract it relies on, balancing, discharge,
  DV, calibration, the bootloader hand-off.
- **1:** diagnostics and telemetry.

The **safety-weighted line coverage** is Σ w·hit / Σ w·lines.

## 2. Numbers

| | AMS app lines | AMS functions | AMS safety-weighted | ECU app lines | ECU functions | ECU safety-weighted |
|---|---|---|---|---|---|---|
| Before (dev, 2026-10-08) | 2741/3377 (81.2 %) | 322/388 (83.0 %) | 87.5 % | 2429/2556 (95.0 %) | 216/224 (96.4 %) | 94.6 % |
| After this work | 3186/3377 (94.3 %) | 379/388 (97.7 %) | 94.8 % | 2459/2556 (96.2 %) | 217/224 (96.9 %) | 96.4 % |
| Scenario suite alone, before (2 scenarios) | 1824/3377 (54.0 %) | 228/388 (58.8 %) | 64.9 % | 1229/2556 (48.1 %) | 117/224 (52.2 %) | 48.6 % |
| Scenario suite alone, after (5 scenarios) | 1846/3377 (54.7 %) | 231/388 (59.5 %) | 65.9 % | 1661/2556 (65.0 %) | 163/224 (72.8 %) | 61.6 % |

Whole images, all code (HAL and RTOS included), before → after:

- AMS.elf: 64.0 → 68.5 % of lines, 79.9 → 87.0 % of functions;
- ECU08.elf: 68.7 → 69.1 % of lines, 83.4 → 83.6 % of functions;
- the CAN bootloader: 68.6 % of functions (its Release build has no line info).

"After" adds 33 native tests and 3 scenarios to the baseline's 474 native
tests (soak excluded) and 2 scenarios. Bold marks an area that moved.

### By area, before → after

AMS (`systems/ams.yaml`, `systems/ecu-ams.yaml`):

| Area | W | Lines before | Lines after |
|---|---|---|---|
| FSM + SafetyTask (`state_machine.hpp`, `safety_task.cpp`) | 3 | 234/238 (98.3 %) | 234/238 (98.3 %) |
| Safety predicates (`safety_predicates.hpp`) | 3 | 74/76 (97.4 %) | 74/76 (97.4 %) |
| Error latch | 3 | 15/15 (100 %) | 15/15 (100 %) |
| Contactors (`relay_driver.cpp`) | 3 | 42/42 (100 %) | 42/42 (100 %) |
| Cell + temperature monitoring (BMS, LTC6811/6820, open wire, NTC) | 3 | 626/662 (94.6 %) | 626/662 (94.6 %) |
| Current monitoring | 3 | 106/106 (100 %) | 106/106 (100 %) |
| Watchdog + fault handlers and RTOS hooks | 3 | 26/54 (48.1 %) | **33/54 (61.1 %)** |
| CAN (ACU bus, heartbeat, encoders, bus-off) | 2 | 555/580 (95.7 %) | 562/580 (96.9 %) |
| Balancing | 2 | 54/54 (100 %) | 54/54 (100 %) |
| Bootloader hand-off | 2 | 32/32 (100 %) | 32/32 (100 %) |
| SoC estimator | 1 | 96/100 (96.0 %) | 96/100 (96.0 %) |
| SD logging | 1 | 451/673 (67.0 %) | **596/673 (88.6 %)** |
| LOGFS (ISO-TP diag) | 1 | 24/325 (7.4 %) | **304/325 (93.5 %)** |
| Pit-diag + health | 1 | 289/296 (97.6 %) | 295/296 (99.7 %) |
| IMU | 1 | 91/96 (94.8 %) | 91/96 (94.8 %) |

ECU (`systems/ecu.yaml`, `systems/ecu-ams.yaml`):

| Area | W | Lines before | Lines after |
|---|---|---|---|
| Control FSM (R2D, AmsError, inverter recovery: `control.cpp`, `control_task.cpp`) | 3 | 313/316 (99.1 %) | **315/316 (99.7 %)** |
| APPS / brake inputs and T.11.8.9 (`io_signals`, `pedal_cal.cpp`) | 3 | 75/79 (94.9 %) | **79/79 (100 %)** |
| Torque limiters (cell, motor, pack, power) | 3 | 146/146 (100 %) | 146/146 (100 %) |
| AS Emergency buzzer (`as_buzzer.cpp`) | 3 | 10/28 (35.7 %) | **28/28 (100 %)** |
| Watchdog + fault latch + fault handlers | 3 | 71/96 (74.0 %) | 71/96 (74.0 %) |
| Inverter protocol | 2 | 25/25 (100 %) | 25/25 (100 %) |
| DC-link discharge | 2 | 17/19 (89.5 %) | **19/19 (100 %)** |
| DV (uDV seam) | 2 | 32/32 (100 %) | 32/32 (100 %) |
| CAN (RX/TX, vehicle service) | 2 | 345/359 (96.1 %) | 349/359 (97.2 %) |
| Calibration (session, NVM) | 2 | 250/255 (98.0 %) | 250/255 (98.0 %) |
| Bootloader hand-off | 2 | 20/20 (100 %) | 20/20 (100 %) |
| Pit-diag + health | 1 | 443/443 (100 %) | 443/443 (100 %) |
| Dash + radio telemetry | 1 | 424/472 (89.8 %) | 424/472 (89.8 %) |
| GPS | 1 | 238/246 (96.7 %) | 238/246 (96.7 %) |

What the before-run never executed, weight ≥ 2:

- **AMS:**
  - every Cortex-M fault handler and `ams_fault_landing` (the relays-first landing);
  - `vApplicationMallocFailedHook`;
  - the SPI/isoSPI error returns in `bms_poll_task.cpp` and `ltc6820.cpp` (a failed transfer, a failed balance quiesce or restore; reached since by `test_ams_spi_faults.py`, #196, below);
  - the defensive branches of `state_machine.hpp` (`step()` entered in Error, an Undecided mode in Transition) and `safety_predicates.hpp`.
- **ECU:**
  - `AsBuzzer::trigger_` (no test ever raised AS Emergency);
  - the Precharge 10 s retry (`control.cpp:162`);
  - WaitInvStandby's Off-only word for an inverter in Shutdown(13) (`control.cpp:291`, the #148 recovery);
  - three of `validate_cal()`'s refusals (`pedal_cal.cpp:36, 60, 69, 73`);
  - the discharge timeout (`discharge.cpp:53-60`);
  - the MemManage/BusFault/UsageFault handlers;
  - `vApplicationMallocFailedHook`;
  - the `TorqueCap` clamp, a bring-up toggle that is compiled out (100).

Since, on AMS `dev` @ `ec8ab44` (#196 section 1, `test_ams_spi_faults.py`
against `test_ams_chain.py`, `test_ams_balancing.py`, `test_ams_temps.py`
and `test_ams_open_wire.py`): `bms_poll_task.cpp` 182 → 192/195 lines,
`ltc6820.cpp` 44 → 45/46. Every bus-error return is now run. Left:
`bms_poll_task.cpp:268` (a CelFrame with no current mark), `:903-904` (the
event group gone, defensive) and `ltc6820.cpp:133` (receive-only, which
nothing calls).

## 3. Behaviour → test matrix

Requirements come from the firmware's own documentation:

- **AMS:** `docs/FSM_OVERVIEW.md`, `FMEA.md`, `ARCHITECTURE.md` and `CAN_MAP.md`, and the FS rules `ams_config.hpp` cites.
- **ECU:** `README.md`, `HANDOVER.md` and `docs/commissioning.md`, and the rules quoted in the code (T.11.8.9, EV 2.2.1, the DV AS Emergency signal).
- **IFS_HIL:** the test analysis ([`analysis/ifs-hil-tests.md`](analysis/ifs-hil-tests.md) and its appendices, issue #3).

**Tests** are files in `tests/sim/`; **scenarios** are files in
`systems/<system>.scenarios/`. *New* marks what this work added.

### AMS

| # | Behaviour (source) | W | Tests | Scenarios |
|---|---|---|---|---|
| A1 | Cell UV/OV at 2800/4200 mV, debounced, reason + module; tap-artifact guard (`safety_predicates.hpp`) | 3 | `test_ams_cells.py` | |
| A2 | Lost NTC → Error 13 < 500 ms; OT/UT not armed while `TempFaultsTrusted = false` (FMEA COMMISSION-3) | 3 | `test_ams_temps.py` | |
| A3 | Module offline (2) before BmsStale (3); cut isoSPI link and recovery; PEC retry and localisation | 3 | `test_ams_chain.py`, `test_ams_cells.py::test_a_silent_chip_is_reported_offline` | |
| A4 | Open wire (ADOW, 16) | 3 | `test_ams_open_wire.py` | |
| A5 | Over-current on the IIR curve at 185 A, sticky; sensor leg fault (8); dead ADC → CurrentStale (9) | 3 | `test_ams_current.py` | |
| A6 | VcuStale (11) once Car-locked; a dead ECU faults the armed AMS | 3 | `test_ams_fsm_car.py`, `test_sys_ecu_ams.py` | *New* `ams/vcu-stale-opens-the-airs` |
| A7 | Car FSM: TSMS + press, 95 % precharge, 5 s timeout, Transition guard, TSMS drop, bus collapse, sticky Error | 3 | `test_ams_fsm_car.py` | `ams/tsms-precharge-run` |
| A8 | Charger FSM: lock, AIR- then AIR+, PRE never; ChargerStale (14); ChargerTsmsOpen (15) | 3 | `test_ams_fsm_charger.py` | |
| A9 | Contactor/AMS_OK edges, atomic open on Error within a tick | 3 | `test_ams_contactors.py`, `test_ams_boot.py` | `ams/tsms-precharge-run`, *New* `ams/vcu-stale-opens-the-airs` |
| A10 | Error latch across warm reset, cleared by a power cut; overflow hook → IWDG → boots in Error | 3 | `test_ams_reset.py`, `test_broker_power.py` | |
| A11 | **CPU fault in Run opens the contactors before the watchdog; next boot reports it, unlatched** (ARCHITECTURE.md item 9) | 3 | *New* `test_ams_faults.py` | |
| A12 | Re-arm gate: `discharge_engaged` / link > 60 V refuses the press (state_machine.hpp `rearm_permitted`) | 3 | `test_ams_fsm_car.py::test_the_bleed_interlock_refuses_to_arm`; *New* `test_sys_ecu_ams.py::test_a_stranded_link_is_drained_before_the_car_re_arms` | |
| A13 | Balancing: Off/BALN/BALX/BALM, hysteresis, ≤ 8 per module, no neighbours, 50 °C lockout, quiesce before ADCV | 2 | `test_ams_balancing.py`, `test_ams_balancing_modes.py` | |
| A14 | **Forced balancing refused in Run** (FMEA SEASON-3, AMS#553) | 2 | *New* `test_ams_balancing_modes.py::test_a_forced_command_never_balances_in_run` (strict xfail: fixed on AMS dev by #594, not main) | |
| A15 | CAN contract: DLCs, cadences, heartbeat, extended-ID filter, noise; bus-off recovery | 2 | `test_ams_can.py`, `test_fdcan_faults.py`, `test_can_bus.py` | `ams/tsms-precharge-run` (`AMS_status` period) |
| A16 | Boot trigger 0x002, refusal while energised, bootloader flash/verify/jump | 2 | `test_ams_reset.py`, `test_ams_bootloader.py`, `test_bootloader.py` | |
| A17 | SD logging: mount, 4 Hz rows, seal + `.CRC`, power cut, dead/absent card | 1 | `test_ams_sd.py`, `test_ams_imu.py` | |
| A18 | **LOGFS: session, read-only, pull = card bytes + CRC, refused with the TS live, served in Error** (diag_dispatch.hpp) | 1* | *New* `test_ams_logfs.py` | |
| A19 | Pit-diag, fw health, SoC, IMU | 1 | `test_ams_can.py`, `test_ams_telemetry.py`, `test_ams_imu.py` | |
| A20 | **isoSPI transfer errors** (SPI1 HAL timeout): each bus error in the voltage poll, open-wire scan and temperature sweep counted and retried or its channel skipped; a dead bus → BmsStale (3) < 500 ms and chain recovery; a failed balance quiesce holds the selector, a failed restore or mask write is redone next update (FMEA BALANCE-1) | 3 | *New* `test_ams_spi_faults.py` (strict xfails AMS#631, #632) | |

\* LOGFS is diagnostics, but its vehicle-state gate is a safety
property: 0x012 out-prioritises 0x100, so a pull in Run could starve the VCU
heartbeat into VcuStale (`diag_dispatch.hpp:29-43`).

### ECU

| # | Behaviour (source) | W | Tests | Scenarios |
|---|---|---|---|---|
| E1 | T.11.8.9 APPS disagreement > 10 points for 100 ms cuts torque, clears on agreement | 3 | `test_ecu_torque.py` (`t11_8_9_*`) | |
| E2 | APPS map, deadbands, sensor short to ground; EV 2.3 gone (brake + throttle not cut) | 3 | `test_ecu_torque.py` (`test_an_apps_shorted_to_supply_is_detected_at_full_throttle`: strict xfail ECU#247) | |
| E3 | Start-up gates: Vdc latch, ok_precharge, START debounce + brake, 2 s RTDS | 3 | `test_ecu_startup.py`, `test_cosim.py` | `ecu/heartbeat-r2d` |
| E4 | **Precharge 10 s window retries, never leaves without the AMS** (control.cpp:156-163) | 3 | *New* `test_ecu_startup.py::test_precharge_retries_past_its_timeout` | |
| E5 | AmsError from any state within a tick; re-arm from WaitInvVdcConfig; 200 ms AMS freshness | 3 | `test_ecu_ams_gate.py`, `test_ecu_dv.py`, `test_sys_ecu_ams.py` | *New* `ecu/ams-error-inhibit` |
| E6 | **AmsError status LEDs; a silent AMS in Error never arms the car** (gap 6 of the IFS_HIL analysis) | 3 | *New* `test_ecu_ams_gate.py::test_ams_error_swaps_the_status_leds`, `::test_a_silent_ams_in_error_never_arms_the_car` | |
| E7 | **AS Emergency: 10 s of 150 ms pulses on the RTDS, edge-triggered, stale fail-safe** (FSG DV, as_buzzer.hpp) | 3 | *New* `test_ecu_as_buzzer.py` | *New* `ecu/as-emergency` |
| E8 | Torque limiters: power envelope, stale rpm, cell derate, motor and pack thermal caps | 3 | `test_ecu_torque.py` | |
| E9 | IWDG on a stalled ControlTask; HardFault latch across reset; stack overflow (strict xfail ECU#252) | 3 | `test_ecu_robustness.py` | |
| E10 | Inverter: ordered fault bursts, Flt_Clear edge, re-drive, silent inverter (strict xfail ECU#246) | 2 | `test_ecu_inverter.py`, `test_cosim.py` | |
| E11 | **The climb to Ready per inverter state: Ready / Off+Ready / Off only from Shutdown, and the TS-off recovery** (#148/#168) | 2 | *New* `test_ecu_inverter.py::test_the_climb_to_ready_speaks_each_state_s_word`, `::test_a_ts_off_inverter_climbs_back_through_off_to_active` | |
| E12 | DC-link discharge: three terms, hold through a lost 0x021, release below 10 V | 2 | `test_ecu_discharge.py` | |
| E13 | **Discharge timeout (30 s) with a fault, cleared by the AMS; sense floor; never into a precharge** | 2 | *New* `test_ecu_discharge.py::test_a_discharge_that_never_completes_gives_up_with_a_fault`, `::test_an_arm_never_secures_the_discharge_into_the_precharge` (ECU#259, fixed by ECU#263) | |
| E14 | **Discharge interlock end to end with the real AMS** (AMS FMEA DISCHARGE-1, ECU#212) | 2 | *New* `test_sys_ecu_ams.py::test_a_stranded_link_is_drained_before_the_car_re_arms` | |
| E15 | DV seam: 0x504/0x505/0x506/0x511, DV entry and refusal, stale request and command, latch, AmsError pre-emption | 2 | `test_ecu_dv.py` | |
| E16 | Calibration session, vehicle_safe gate, commit/persist, torn/corrupt record | 2 | `test_ecu_cal.py` | |
| E17 | **validate_cal() refuses a backwards APPS, a stiff brake, an inverted brake** (pedal_cal.cpp:64-74 "load-bearing") | 2 | *New* `test_ecu_cal.py::test_an_implausible_sweep_is_refused_and_never_written` | |
| E18 | Boot trigger, refusal in the drive ladder, flash/verify/jump | 2 | `test_ecu_bootloader.py`, `test_ecu_robustness.py` | |
| E19 | Heartbeat 0x100 every tick on the ACU bus only, health, task liveness, heap | 2 | `test_ecu_boot.py` | `ecu/heartbeat-r2d` |
| E20 | TX FIFO overflow (strict xfail ECU#251), RX robustness, babbling node | 2 | `test_ecu_robustness.py`, `test_can_bus.py` | |
| E21 | Pit-diag stream, dash FDCAN3, GPS | 1 | `test_ecu_diag.py`, `test_ecu_pedals.py` | |
| E22 | Reset cause (strict xfails ECU#245, bootloader#193) | 1 | `test_reset_cause.py` | |

## 4. Gaps, ranked

Filled by this work (ranked by safety relevance, as they were found):

1. **ECU AS Emergency tone** (E7). Before: `trigger_` never ran. A DV
   rule's acoustic signal, untested.
2. **AMS CPU-fault landing** (A11). Before: no fault handler ran. Relays
   first is the one thing that keeps AIR+ and AIR- from staying closed for
   up to ~190 ms with no firmware running.
3. **Discharge interlock pairing** (E13, E14): FMEA DISCHARGE-1's open
   item, run end to end. It found **IFS08-CE-ECU#259**: a stale 0x021 lets
   the ECU secure the bleed into a precharge and hold it for 30 s (fixed by
   IFS08-CE-ECU#263).
4. **Forced balancing in Run** (A14): FMEA SEASON-3, pinned as a strict
   xfail until the catalogue's AMS carries #594.
5. **AmsError status LEDs and the silent-AMS exit** (E6): the exit from
   AmsError on a silent AMS is documented, and the car can't arm from it.
6. **The #148 climb words and the Precharge retry** (E11, E4).
7. **Calibration ordering refusals** (E17).
8. **LOGFS** (A18): 7 % → 94 % of its lines. Its vehicle-state gate keeps
   a log pull from starving the VCU heartbeat in Run.

Left, ranked (each needs a model or hook the vHIL doesn't have; issues filed in IFS_vHIL):

| Rank | Gap | Why it matters | What it needs |
|---|---|---|---|
| 1 | ~~AMS SPI/isoSPI transfer failures: `bms_poll_task.cpp` error returns, a failed balance quiesce (FMEA BALANCE-1), a failed chain recovery~~ | The paths the AMS takes when the isoSPI link itself errors, rather than goes silent or returns a bad PEC | **Done (#196):** an SPI1 transfer that never runs (`Stm32H7Spi.cs`) and an LTC6811 WRCFGA that does not take (`IsoSpi.cs`); A20, which found AMS#631 and #632 (below) |
| 2 | Contactor and DC-link physics: welded AIR or PRE (FMEA RELAY-2), precharge against a real R·C, BusCollapse with real AIRs, `DischargeReleaseV` against the inverter's sense floor | Timing assertions need commissioned parameters | M8 TS-1 plant, #147 (parked) |
| 3 | ~~Malloc-failed hooks (AMS and ECU)~~ | Same landing as the overflow hook | **Done (#196):** `Sim.fail_malloc`; both hooks now run (below) |
| 4 | ~~MemManage/BusFault (ECU and AMS)~~ | Same landing as HardFault; their own reason codes | **Done (#196):** a bus-error range in the platform and `Sim.bus_fault_at`, an MPU region for MemManage; both handlers now run (below) |
| 5 | AMS SD rotation at 5 min / 4 MiB, LOGFS LIST paging past 46 entries | Diagnostics only | A long run, or a pre-filled card image; not safety |

Ranks 3 and 4, closed by #196 (`test_ams_faults.py`, `test_ecu_robustness.py`;
[setup.md](development/setup.md#cpu-fault-injection)):

- **Allocation failure.** The AMS allocates in Run (FatFs' sync object at every
  mount attempt): the hook opens the contactors within 0.2 ms and sets the
  ErrorLatch, but it spins in SdLoggerTask with interrupts on, so SafetyTask
  keeps feeding the IWDG: no reset, Run reported until BmsStale, no
  `MallocFail` on 0x6CA (**IFS08-CE-AMS#634**, two strict xfails). The ECU
  allocates only at boot: a failed allocation latches 0xF6 and returns, and
  with `can_rx_queue` missing the ECU never sends a 0x704 (ECU#252, strict
  xfail).
- **MemManage / BusFault.** Neither firmware sets SHCSR's fault enables, so on
  the car a BusFault or MemManage escalates to HardFault (HFSR.FORCED): their
  handlers and reason codes are unreachable (**IFS08-CE-AMS#633**,
  **IFS08-CE-ECU#264**, strict xfails). The vHIL routes them as the
  ARMv7-M ARM says: tests that write the enables, as the fix would, land in
  `BusFault_Handler` (precise, BFAR) and, through an MPU no-access region,
  `MemManage_Handler` (DACCVIOL, MMFAR), and the next boot reports 6/5 (AMS)
  and 0xF3/0xF2 (ECU). A jump into peripheral space (an instruction fetch
  from an XN region of the default memory map) is a MemManage with IACCVIOL
  (#239, `models/renode/VhilExecuteNever.cs`): HardFault/FORCED as the
  firmware stands, `MemManage_Handler` with MEMFAULTENA set. A fetch from a
  reserved range (IBUSERR on the chip) still aborts the Renode machine; the
  run fails at once. UsageFault stays uncovered: its trigger is the same
  escalation, and no test enables it.

`--vhil-coverage` over the new tests alone (AMS dev `ec8ab44`, ECU dev
`44610a5`) hits `vApplicationMallocFailedHook`, `MemManage_Handler` and
`BusFault_Handler` in both images (`freertos.c:122-134` / `:175-188`,
`stm32h7xx_it.c:132-154` / `:105-130`); the suite-wide figures above are
not re-run.

Some lines are unreachable by design, and are not gaps:

- the defensive branches of `state_machine.hpp` (193, 205, 362, 396): backstops
  SafetyTask handles before it steps the FSM;
- the compiled-out `TorqueCap` clamp (`control_task.cpp:192`);
- the returns of the FreeRTOS task entry stubs (ECU `freertos.c:270-340`),
  which never return.

**Firmware issues this work filed:** isc-fs/IFS08-CE-ECU#259 (the discharge
secured into a precharge; fixed by ECU#263). The isoSPI fault hooks (#196)
filed isc-fs/IFS08-CE-AMS#631 (after a failed quiesce the open-wire scan
runs on a bleeding chain, FMEA BALANCE-1 point 2) and #632 (the quiesce is
never verified: a WRCFGA a chip rejects counts as a quiesce, and the poll
converts under bleed unflagged), both pinned as strict xfails. The new tests also pin, as strict xfails, the
already-filed IFS08-CE-AMS#553 (forced balancing in Run; fixed on AMS dev, not
on main). The strict xfails the suite already carried are unchanged:
ECU #245, #246, #247, #248, #249, #251, #252; AMS #599, #604, #616, #619,
#620, #623; stm32-can-bootloader #191, #192, #193.

## 5. What the vHIL can't cover

These need the physical bench, the car, or the firmware's own host tests:

- **Analog accuracy:** ADC gain and offset, current-sensor calibration
  (FMEA COMMISSION-2), NTC curve accuracy through the ADG731. The vHIL sets
  pin voltages; it doesn't measure them.
- **Bus physics:** bit timing, error counters, EMC on the isoSPI and CAN
  (the AMS's torque-EMI history), real bus-off from electrical faults. The
  vHIL injects bus-off; it can't cause it.
- **Power:** cold-boot behaviour on real rails, brown-out, power loss
  mid-flash (F-077: the H7's double-bit ECC brick), backup-domain decay
  without VBAT over time.
- **Flash wear and ECC:** the vHIL has no flash ECC, so a torn write reads
  back clean (`test_ecu_cal.py` exercises the parser instead).
- **Timing tolerance:** the LSI's ±47 % (IWDG period), crystal drift.
  Renode's clocks are exact.
- **Electromechanics:** contactor coil and weld behaviour, buzzer and LED
  output, the discharge relay itself (#253: PB6 is not in any schematic).
  Until M8 these are sequencing only.
- **32-bit tick wrap** (AMS FMEA TICK-1): 49.7 days of virtual time. The
  firmware's host tests are the place for it.
- **A MainTask that loops but computes wrong answers** (FMEA WATCHDOG-2): a
  design gap, not a behaviour to test.

## 6. Re-running

```sh
scripts/vhil-docker.sh coverage 'test_ams_*'      # one slice; results/coverage
python -m vhil.coverage results/cov/* -o results/cov-merged
```

Run at most about three Renode containers at once in the 8 GB Colima VM:
the OOM killer takes Renode first, and the test then fails with "Renode
closed the monitor connection".
