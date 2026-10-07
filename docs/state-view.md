# State view

What the editor's state panel shows of a board (feature 2 of the
[editor workspace plan](architecture/editor-workspace.md)): its FSM state,
contactors and relays, faults and key values. The firmware declares it once,
in the catalogue next to its CAN contract, so every system that places a
board running that firmware shows the same card.

## In the catalogue

`catalog/firmware/<id>.yaml`, `state_view`: an ordered list, each item cited
in the firmware at the catalogue's ref in a trailing comment, as the rest of
the catalogue is.

```yaml
state_view:
  # The FSM state, mirrored on every transition (safety_task.cpp:51 ...)
  - {label: State, kind: state, source: "symbol:g_state_telemetry", enum: "ams::fsm::State"}
  - {label: AIR+, kind: relay, source: "pin:PB5"}
  - {label: Fault, kind: fault, source: "symbol:g_fault_reason_telemetry",
     enum: "ams::safety::FaultReason", values: {"12": FsmError}}
  - {label: Min cell, source: "frame:FDCAN1.AMS_status.min_cell_mV", unit: mV}
```

| Key | |
|---|---|
| `label` | what the card says (letters, digits, ` ._+/()%-`, at most 32) |
| `source` | `frame:<connector>.<message>.<field>`, `symbol:<name>` or `pin:<pin>` |
| `kind` | `state` (the FSM state, one per view), `relay` (a contactor, relay or digital line: a square pill, filled when non-zero), `fault` (active when non-zero), `value` (default) |
| `unit` | shown after a value |
| `enum` | the DWARF enumeration, qualified (`ams::fsm::State`), whose enumerators label a symbol's values |
| `values` | labels by raw value, cited in the firmware: values DWARF has no enumerator for, or a plain integer's |
| `period_ms` | how often a symbol is sampled: default 10 ms for `state`, `relay` and `fault`, 50 ms for `value` |

A source is a scenario expect's signal ([scenarios.md](scenarios.md)) without
the system's names: the connector stands for the bus it is on, the board is
the one running the firmware. On a system's board it becomes that system's
signal (`vhil/stateview.py` `resolve`): `frame:FDCAN1.AMS_status.min_cell_mV`
on `systems/ams.yaml`'s `ams` is `frame:can_acu.AMS_status.min_cell_mV`,
`symbol:g_state_telemetry` is `symbol:ams.g_state_telemetry`, `pin:PB5` is
`pin:ams.PB5`. A connector on no bus of the system, or a pin that isn't one
of the board's GPIOs, is an error of that item.

The schema (`schemas/vhil.schema.json`, `$defs/firmware`) holds every key to
a pattern: nothing in a view reaches the page but as text, or the emulator
but as a symbol name the ELF has.

## Labels

- **A symbol** is labelled by the enum its item names, read from the board's
  ELF (`vhil/elf.py` `enums`: DWARF 2 to 5, as Arm GNU 14.2 writes it with
  `-g`, which both firmwares' CMake builds pass), or by the enum the global's
  own type is. The AMS's `g_state_telemetry` is a `uint8_t`, so its item
  names `ams::fsm::State`.
- **A frame field** is labelled by its `.def` value table (`CAN_VAL`).
- **`values`** go over either: the AMS's FaultReason 12 (FsmError) is a bare
  constant with no enumerator (`safety_predicates.hpp:76-83`).
- An image without DWARF (built without `-g`) has no enums; the item says so
  (`note`) and the panel shows the raw value with a "no enum" hint, or the
  catalogue's `values` alone.

`GET /api/firmware/<id>/enums?ref=` lists an image's enums and enum-typed
globals. The contract (`GET /api/systems/<id>/contract`,
`GET /api/runs/<id>/contract`; `vhil/server/decode.py`) carries each board's
resolved view (`state`) and every labelled signal (`labels`), so an expect
and the Scenario tab take `== Precharge` where a number was needed.

## What a run records

The web app's worker (`vhil/worker.py`, `execute_run(..., state_view=True)`)
watches every view pin from power-on, with its level then as an `initial`
edge, and samples every view symbol at its period, so a run's trace holds
what its state panel shows: frames, edges and samples, as the trace always
has, decoded in the browser as live data will be. A symbol the image lacks
(an older firmware) is left out and the run's log says so.

## The views

AMS (IFS08-CE-AMS `main`): State (`g_state_telemetry`, `ams::fsm::State`);
AIR+, AIR-, PRE, AMS_OK (PB5, PB6, PB7, PB4); Fault
(`g_fault_reason_telemetry`, `ams::safety::FaultReason` and FsmError);
Mode (`g_mode_locked_telemetry`, `ams::fsm::Mode`); min and max cell, max
temperature, pack current and voltage, SoC (0x4A0, 0x4A2, 0x135, 0x4A1,
0x130 on FDCAN1).

ECU (IFS08-CE-ECU `dev`): State (`g_last_ctrl_state`, `ecu::CtrlState`);
RTDS, Discharge (PB4, PB6), START (debounced, `g_last_start_button`); APPS
T.11.8.9, discharge fault, the last reset's fault (0x704); torque request,
APPS1/2, brake raw; the
inverter's state (PitDiag_status 0x700, sent only once pit-diag is armed by
0x7E0) and the DC bus (0x100). The inverter's own App_State frame (0x461 on
FDCAN1) isn't in the ECU's .def contract yet (`all_messages.inc`
TODO(inverter)), and no global holds it.
