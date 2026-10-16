# M8: the car's electrical plant (tractive system)

Plan for [#147](https://github.com/isc-fs/IFS_vHIL/issues/147), unparked on
2026-10-16 once HIL parity and the speed work were done. The plant FMU is
requested from IFSSIM in
[isc-fs/IFSSIM#669](https://github.com/isc-fs/IFSSIM/issues/669); until it
lands, a Python stand-in (TS-1) runs the same contract in CI.

## Status (2026-10-16)

Steps 1-5 below are merged: the plan and parameters (#310), the TS-1 stand-in
on an event-driven port (#311), the FMI 3.0 master (#313), the `plants:`
switch and the scenarios (#314), and this evidence. `ecu-ams` and
`ecu-ams-udv` run the plant in every scenario and web-app run.

**Results** against ECU dev 44610a5 and AMS dev 94b59a9. Every scenario
passes, locally (aarch64) and on CI (x86):

| Scenario (`systems/ecu-ams.scenarios/`) | What the firmware did |
|---|---|
| `car-arms-through-the-ecu` | Arms through Precharge to Run on the plant's link. AIR+'s contact closes at 335.0 V of 351.5 V (95.3 %; FS rule 90 %, firmware 95 %), 0.86 s after PRE and AIR- |
| `ams-fault-puts-the-ecu-in-ams-error`, `dead-ecu-opens-the-airs` | As before, on the plant's link instead of a scripted step |
| `precharge-never-conducts`, `precharge-into-a-large-capacitance`, `precharge-into-a-leaky-link` | Error, reason 12, at `PrechargeMaxMs`; AIR+ never driven |
| `welded-air-minus` | Arms as usual: undetectable without aux contacts |
| `welded-air-plus` | AIR- closes the pack straight onto the empty link (0 % at its closing), Run at once: undetectable without aux contacts |
| `airs-drop-out-in-run` | Bus collapse confirmed, back to Start without latching; the ECU secures the discharge |
| `rearm-refused-on-a-charged-link` | A press on the stranded link is refused while the ECU holds the bleed; after the release a press re-arms |
| `inverter-sense-floor-holds-the-discharge` | With a 48 V reading floor, the ECU secures a **drained** link at TSMS on and holds it for 30 s with a fault; the AMS refuses the arm meanwhile ([IFS08-CE-ECU#266](https://github.com/isc-fs/IFS08-CE-ECU/issues/266); with a silent floor the car never arms) |

No scenario fails against the firmware's intent, so none is an expected
failure. The welded-AIR cases are hardware the car lacks (aux contacts, out
of scope). The sense floor is an unmeasured inverter behaviour, filed for the
measurement.

**Speed**: the plant costs +0.1 % host CPU per virtual s against a scripted
0x466 of the same frames ([`performance.md`](performance.md#a-plant-on-the-co-simulation-port-m8-147)).
The identity fingerprint of every system is unchanged.

**#147's exit criteria**:

- [x] On `ecu-ams.yaml` the real AMS and ECU go Start → Precharge →
  Transition → Run on V_link from TS-1, and no scripted DC-bus value is left
  in its scenarios.
- [x] The fault scenarios give the expected firmware state and reason,
  deterministically.
- [x] The re-arm and sense-floor scenarios run in CI and pass. The sense
  floor's consequence is filed (IFS08-CE-ECU#266).
- [ ] Every parameter cited and the nominal curve validated against a car
  log. Only `c_link`, `vdc_resolution` and `cells_per_chip` are cited, and no
  car log exists, so reports say "uncommissioned".
- [x] Parity runs are unaffected. The bench path runs no plant, IFS_HIL's
  suites pass, and the fingerprint is unchanged.
- [x] `docs/cosim.md` and `docs/vision.md` are updated.

## Why

The firmware has safety logic that only a tractive-system plant can exercise,
and neither bench has one (the physical bench samples the AMS's PB4-PB7 as
logic levels and fakes precharge with a linear CAN ramp):

- AMS (IFS08-CE-AMS dev 94b59a9): `PrechargeMaxMs` 5000
  (`ams_config.hpp:190`), the 95 % precharge-complete test
  (`state_machine.hpp:117-128`), `BusCollapsePercent` 50 /
  `BusCollapseConfirmTicks` 20 (`:742-743`), the `DcBusDischargedV` 60 re-arm
  gate (`state_machine.hpp:163-171`, `ams_config.hpp:760`).
- ECU (IFS08-CE-ECU dev 44610a5): the held discharge and its
  `DischargeReleaseV` 10 / `DischargeTimeoutMs` 30000
  (`ecu_config.hpp:540`, `:548`; `discharge.hpp`), against the inverter's
  sense floor.

And `vhil/plants.py`'s `Inverter.dc_bus_v = 350` reports a charged link with
the AIRs open: a known departure from the car (invariant 9).

## The boundary (written into vision.md)

The vHIL owns the car's **electrical plant between the boards' connectors**:
what the boards drive and measure on their harness (contactor coils, the
tractive system's DC link, the cells the LTC6811 chain reads, the current
sensors). Vehicle, motor load, tyres and EBS pneumatics belong to
MingoCIL/IFSSIM (M9). The physics of the accumulator and powertrain is
IFSSIM's: it is requested as an FMU (IFSSIM#669), and the vHIL is its
co-simulation master. The vHIL's own Python plant is a stand-in for CI, not a
second battery model.

## The plant contract

One contract for the stand-in and the FMU: the variable names, units and
signs of IFSSIM#669 §3, so either drops in for the other. The vHIL maps them
to the system's port.

| Variable | Dir | Unit | Car signal (vHIL endpoint) | TS-1 stand-in |
|---|---|---|---|---|
| `air_pos_cmd`, `air_neg_cmd`, `pre_cmd` | in | bool | AMS PB5 / PB6 / PB7 coil drive (`gpio_out`) | yes |
| `discharge_cmd` | in | bool | ECU PB6, high = secure the bleed (`gpio_out`; ECU `control_task.cpp:301`) | yes |
| `sdc_closed` | in | bool | the coil supply AIR+_VCC, which the AMS senses on PF9 (schematic, below): the level driven on `ams.PF9` | yes |
| `i_inverter_dc` | in | A, + = discharge | scripted in M8 (0), IFSSIM powertrain in M9 | 0 only |
| `i_charger`, `cell_bleed[95]`, `t_ambient` | in | A, bool, K | | ignored |
| `v_link` | out | V | inverter DC link, reported on 0x466 (`can_tx`, can_inv) | yes |
| `v_pack` | out | V | pack terminals | sum of the LTC6811 model's cells |
| `air_pos_closed`, `air_neg_closed`, `pre_closed` | out | bool | contact state after its delay | yes |
| `v_cell[95]`, `t_sensor[N]`, `i_pack`, `soc_cell[95]` | out | V, K, A, 1 | the LTC6811 chain, the current sensors | not yet: the LTC6811 model stays the cell source (docs/cosim.md, "Cell source") |

Faults are boolean inputs of the plant (IFSSIM#669 §4), test API on the
stand-in: `welded_air_pos`, `welded_air_neg`, `pre_open_circuit` (the
precharge path never conducts), `air_external_open` (both AIRs' contacts drop
out with their coils still driven), `c_scale` (a large C) and `r_leak` (a
leaky one).

**Parameters**: [`catalog/plants/tractive-system.yaml`](../catalog/plants/tractive-system.yaml),
one entry each with `value`, `unit`, `status` (cited or uncommissioned),
`source` and `validated_against`. The FMU takes them as FMI parameters of the
same names.

| Parameter | Value | Status | Source |
|---|---|---|---|
| `c_link` | 400 uF | cited | W90 manual §5.1, 380-420 uF |
| `r_precharge` | 690 ohm | uncommissioned | IFS_HIL comment only (bench) |
| `r_discharge` | 6.5 kohm | uncommissioned | ECU's design estimate, tau ~2.6 s (`ecu_config.hpp:534-539`) |
| `r_inverter_bleed` | none | uncommissioned | not in the W90 manual |
| `air_close` / `air_open` | 20 / 10 ms | uncommissioned | AIR part unknown |
| `pre_close` / `pre_open` | 10 / 5 ms | uncommissioned | relay part unknown |
| `cells_per_chip` | 9, 10 | cited | AMS `ams_config.hpp:1130-1131` |
| `vdc_period` | 10 ms | uncommissioned | not in the DBC, not measured |
| `vdc_resolution` | 1 V | cited | DBC `DCBus_Voltage_V` 16\|10@1+ (1,0) |
| `sense_floor` / `below_floor` | 0 V / hold | uncommissioned | unmeasured (`ecu_config.hpp:520-533`) |

Nothing is validated against a car log: none exists in any repo (searched
2026-10-16). Every report on these runs says "uncommissioned".

**Hardware traced for this plan** (isc-fs/IFS08-ES main bcb4511,
`boards/AMS_IFS08/kicad/AMS IFS08.kicad_sch`, its netlist): PB5/PB6/PB7
(MainLite pads 7/8/9) drive Q2/Q1/Q3 DMN6075S low-side switches (1 kohm gate,
2.2 kohm pull-down) to J11.6/J11.5/J11.4; the coils' high side and flyback
diodes D5/D6/D2 return to **AIR+_VCC** (J11.1-3, J1.16), which is also what
PF9 (TSMS_FIL) senses through R18 62 kohm. Two consequences:

- the coil supply is the TSMS-switched shutdown circuit, and the AMS sees it:
  when the SDC opens upstream of AIR+_VCC, PF9 drops with it, and the AMS
  falls to Start (TSMS drop), not to its bus-collapse path;
- so `BusCollapse` (AIRs open while the AMS believes Run,
  `state_machine.hpp:415-421`) needs a cause the AMS cannot sense on PF9: a
  contact that drops out, a broken coil wire or return. The fault scenario
  uses `air_external_open` for that, and says why.

## Time and determinism

The plant steps in virtual time only, and a run with it is as deterministic as
one without: the same firmware, system, parameters and stimuli give the same
frames at the same times (a test holds two runs identical).

**Exchange on events, not every step.** Stopping the emulation costs: on
ecu-ams a 10 ms lock-step exchange (a `RunFor` per step, one edge query) costs
+0.15-0.25 host CPU s per virtual s over 100 ms slices, +20-30 % (measured
2026-10-16, M-series host, Colima). The budget is under 5 %. So the plant is
exchanged with only when something can change:

- **its inputs change**: an armed coil pin's edge ends the emulation's run at
  the next sync point (`emulation RunForUntilTrigger`, a C# runner that is
  `MasterTimeSource.RunFor`'s loop with one flag checked at each sync point;
  `VhilGpioProbe.Trigger`). The edge's exact virtual time comes with it
  (`BoardIO.edges(since_us)`), and contacts move after their delay, so a
  plant that learns of an edge at most one quantum (0.5 ms) late computes
  exactly what a plant stepped at every quantum would. A stimulus that drives
  PF9 is already a stop of the run;
- **its outputs change**: the stand-in is closed form (first-order RC
  segments between events), so at an exchange it computes the link until the
  next event it can foresee, and hands the inverter's 0x466 reports for that
  stretch to the CAN probe in one call (a periodic sender with a payload
  schedule). The run stops again only when the stretch ends (the link has
  settled: none) or an input changes.

Measured on the prototype: a run with the trigger armed gives a bus timeline
byte-identical to `emulation RunFor`'s, and returns at the sync point after
the edge (an AIR-/PRE edge at 3648.5 ms returned at 3649.0 ms).

A lock-step `Port.run` (native tests' plants: `Inverter`, `Pedals`) keeps its
fixed step; the TS plant joins it as one more plant and is exact at any step
for the reason above.

**The FMU** steps on a fixed communication grid (5 ms default, 1-10 ms),
inputs held over a step. The master steps it ahead to compute the coming
0x466 reports, and when an input edge arrives inside that stretch it rolls
back: `fmi3GetFMUState`/`fmi3SetFMUState` at each exchange when the FMU can
(`canGetAndSetFMUState`), else `fmi3Reset` and a replay of the recorded inputs
from t = 0 (deterministic by IFSSIM#669 §2; cheap at the requested 50x real
time over the few edges a run has). An input change takes effect at the
first grid point at or after it.

**Speed check** for every PR that touches a run: `scripts/speed.py`
interleaved A/B, ecu-ams, and an ecu-ams scenario with the scripted 0x466
against the same scenario plant-driven (both send 0x466 every 10 ms). The
identity fingerprint of every system without a plant stays byte-identical.

Found on the way, not M8's to fix: a CAN probe frame costs ~11 ms of wall
time on ecu-ams (a 10 ms 0x466 periodic takes ecu-ams from 0.48 to 1.57 wall
s per virtual s, CPU +20 %): Renode's `TimeSourceBase.WaitIfBlocked`
`blockingEvent.WaitOne(10)`. Every scenario with a periodic stimulus pays it
today; tracked separately.

## Where the plant runs

| Path | Plant | Why |
|---|---|---|
| Scenarios and web-app runs (`vhil/worker.py`) | yes, when the system file declares it | the plant is part of the system |
| Native tests (`vhil.sim` + `Port`) | when a test passes it to `Port.run` | tests choose their plants |
| Wall-clock bench, IFS_HIL parity (`vhil/bench.py`) | **no** | parity keeps the bench's stimulus: IFS_HIL's linear `ramp_to` |

The system file switch, on `ecu-ams.yaml` and `ecu-ams-udv.yaml`:

```yaml
plants:
  ts: {plant: tractive-system, cells: isospi}   # catalog/plants/tractive-system.yaml
port:
  step_ms: 10
  signals:
    air_pos_cmd:   {gpio_out: ams.PB5}
    air_neg_cmd:   {gpio_out: ams.PB6}
    pre_cmd:       {gpio_out: ams.PB7}
    discharge_cmd: {gpio_out: ecu.PB6}
    sdc_closed:    {gpio_in: ams.PF9}   # AIR+_VCC as the AMS senses it
    v_link:        {can_tx: {bus: can_inv, id: 0x466}}
```

A system without `plants:` renders and runs exactly as before.

## Scenarios (beyond-bench)

Plant-driven scenarios replace the scripted 0x466 steps in
`systems/ecu-ams.scenarios/`, and add, tagged `beyond-bench` (they never
gate parity runs):

| Scenario | Expect |
|---|---|
| nominal precharge | Start → Precharge → Transition → Run on V_link from the plant; AIR+ closes with V_link ≥ 90 % of V_pack (FS rule; the firmware waits for 95 %) |
| precharge never conducts (`pre_open_circuit`) | Error after `PrechargeMaxMs`, reason 12 |
| welded AIR- / welded AIR+ | the firmware's response (sequencing), recorded |
| AIRs drop out in Run (`air_external_open`) | bus collapse confirmed, back to Start (not latched) |
| large / leaky C | precharge slower / settles short: Run late or Error, reason 12 |
| re-arm on a charged link | a DASH_CHG press with V_link above `DcBusDischargedV` is refused (stays Start) |
| inverter sense floor | with a floor above `DischargeReleaseV`, the ECU's held discharge never releases and times out after `DischargeTimeoutMs` |

Timing is asserted only against validated parameters; with every parameter
uncommissioned today, the expects assert sequencing and the firmware's own
constants (`PrechargeMaxMs`, `BusCollapseConfirmTicks`), not the RC curve.
When the firmware fails one, invariant 9 decides: the car's hardware and
the firmware's intent, a firmware issue with the evidence, and the scenario
held as a strict expected failure.

## Steps

1. This plan, the boundary in vision.md, the parameter file.
2. The plant interface and the TS-1 stand-in (`vhil/plant_ts.py`): the
   contract, the parameter loader, the event-driven runner, the probe's
   payload schedule, `Inverter` reading V_link when a plant is present.
3. An FMI 3.0 co-simulation master (`vhil/fmi.py`, ctypes, no new
   dependency) and a reference FMU built in the tests from C: the same
   contract, so the stand-in and the FMU are compared on the same inputs.
4. The system switch and the scenarios above.
5. Speed and determinism evidence, docs, #147's exit checklist.

## Left after M8's steps

- Validation against a car log of a real precharge (0x466/0x100), with its
  id, date and firmware commit; until then "uncommissioned".
- IFSSIM's FMU swapped in for the stand-in.
- The accumulator's TS schematic (precharge R, AIR and precharge-relay part
  numbers, bleed R), to cite what is a placeholder now.
- TS-2 (SDC chain), the observer fixtures.
