# Scenarios

A scenario is a run of a system as data: the stimuli it applies, what it
watches and what it expects, in virtual time from power-on. Its `expect`
rows make it a test, and every committed scenario is part of the vHIL's own
test suite. The editor's Scenario tab edits them
([editor workspace plan](architecture/editor-workspace.md), feature 1); a
live session's operations (feature 3) are the same stimulus kinds.

## Where they live

`systems/<system>.scenarios/<name>.yaml`, next to the system they drive:
`systems/ams.scenarios/tsms-precharge-run.yaml` runs `systems/ams.yaml`. A
name is lowercase letters, digits and `-` (at most 64). The directory is not
a system (`systems/*.yaml` stays the systems), and it is outside the code a
run at a saved ref must share with the deployment
(`CODE_PATHS`, `vhil/server/runs.py`), so a branch that adds a scenario still
runs in the web app. The editor saves one as it saves a system: a one-file
commit on a branch (Commit…), then Open PR.

## The file

From `systems/ams.scenarios/tsms-precharge-run.yaml` (shortened):

```yaml
kind: scenario
system: ams
description: "Car mode: the ECU's 0x100 at 0 V, TSMS on and a DASH_CHG press arm Precharge ..."
virtual_ms: 7600
stimuli:
  - {kind: can_periodic, name: vcu, at_ms: 2500, bus: can_acu, id: 0x100, data: "000002", period_ms: 10}
  - {kind: gpio, name: tsms-on, at_ms: 5500, board: ams, pin: PF9, level: true}
  - {kind: gpio, name: dash-press, at_ms: 5600, board: ams, pin: PF10, level: true}
  - {kind: gpio, name: dash-release, at_ms: 5650, board: ams, pin: PF10, level: false}
  - {kind: stop_periodic, at_ms: 6500, periodic: vcu}
  - {kind: can_periodic, name: vcu-link, at_ms: 6500, bus: can_acu, id: 0x100, data: "640102", period_ms: 10}
watch:
  - {kind: symbol, board: ams, name: g_state_telemetry, size: 1, period_ms: 10}
expect:
  - {check: never, name: no-error, at_ms: 0, signal: "symbol:ams.g_state_telemetry", value: 5}
  - {check: eventually, name: precharge, at_ms: 5600, until_ms: 5700, signal: "symbol:ams.g_state_telemetry", value: 1}
  - {check: eventually, name: pre-closes, at_ms: 5600, until_ms: 5700, signal: "pin:ams.PB7", value: high}
  - {check: eventually, name: run-on-can, at_ms: 6500, until_ms: 7550, signal: "frame:can_acu.AMS_status.fsm_state", value: 3}
  - {check: always, name: air-plus-closed, at_ms: 6800, signal: "pin:ams.PB5", value: high}
  - {check: period, name: status-period, at_ms: 3000, signal: "frame:can_acu.AMS_status", min_ms: 450, max_ms: 550}
```

The editor writes it canonically: one row a line, ids in hex, defaults
filled in (`at_ms: 0`), and an unchanged scenario keeps its text, comments
and all. By hand, any YAML that parses to the same data is fine.

| Key | |
|---|---|
| `kind` | `scenario` |
| `system` | the system's id: the file's directory |
| `description` | optional text, at most 2000 characters |
| `virtual_ms` | the run's virtual time from power-on (default 3000; each MainLite spends its bootloader's 2 s first) |
| `slice_ms` | how often the trace is flushed and a cancel seen (default 100) |
| `stimuli`, `watch`, `expect` | the rows below |

The rows are exactly a `run` scenario's (`RunScenario` in
`vhil/server/runs.py`): the file is what `POST /api/runs` takes as
`{"kind": "run", "name": "<name>", ...}`, and what the worker executes.

## Rows

Every row may have a `name` (letters, digits, `_`, `-`): a label on the
timeline and in results, and what a `stop_periodic` names.

| Kind | Fields | |
|---|---|---|
| `can_send` | `at_ms`, `bus`, `id`, `data` (hex, ≤ 64 bytes), `ext` | one frame |
| `can_periodic` | as `can_send`, `period_ms`, `until_ms` | every `period_ms` from `at_ms` |
| `stop_periodic` | `at_ms`, `periodic` (a periodic's name) | stops it |
| `gpio` | `at_ms`, `board`, `pin` (a GPIO), `level` | drives an input |
| `analog` | `at_ms`, `board`, `pin` (an analog input), `volts` (0..3.6) | sets its voltage |
| `watch` | `at_ms`, `board`, `symbol` (with `size`, `period_ms`) or `pin` | from `at_ms` to the end: samples a global (default every 10 ms, its size from the ELF) or records a pin's edges, with its level then |
| `watch: symbol` | `board`, `name`, `size` (1, 2, 4), `period_ms` | samples a firmware global from power-on |
| `watch: pin` | `board`, `pin` | records its edges from power-on |

A frame the scenario sends is in the trace with `src: "stimulus"`. The
`watch` stimulus is the one that starts mid-run, a live session's op too;
the `watch` list's rows hold from power-on. The web app's runs also record
every board's state view ([state-view.md](state-view.md)).

## Expects

An expect checks one signal over a window of virtual time,
`[at_ms, until_ms]` (`until_ms` defaults to the end of the run), after the
run, against its trace (`vhil/expect.py`):

| `check` | Takes | Passes when |
|---|---|---|
| `eventually` | `signal`, `op`, `value` | the value meets `op value` at some time in the window |
| `always` | `signal`, `op`, `value` | it meets it at every time in the window (and there is a value) |
| `never` | `signal`, `op`, `value` | it meets it at no time in the window |
| `period` | a frame `signal`, `min_ms`, `max_ms` | every gap between its arrivals is in range; with `max_ms`, so is the silence from the window's start to the first and from the last to its end (two frames at least) |
| `count` | a frame `signal`, `min`, `max` | it arrives between `min` and `max` times (`max: 0`: never) |

**Signals.** `frame:<bus>.<message>.<field>` is a field of the firmware's
CAN contract (its `.def` files, `vhil/candef.py`), the message by name or
`0x` id; `frame:<bus>.<message>` is the frame itself (`period`, `count`;
a raw `0x` id needs no contract). `symbol:<board>.<name>` is a firmware
global, sampled every 10 ms unless a watch samples it already, its size
from the ELF. `pin:<board>.<pin>` is a GPIO, watched from power-on with its
level then recorded (`edge` with `initial: true`).

**Values** are as of the last observation at or before a time: a frame's
field from the last such frame (the scenario's own frames don't count), a
symbol's last sample, a pin's level after its last edge. The value carried
into the window counts as seen at its start. `value` is a number (a field's
physical value, `raw * factor + offset`; a symbol's raw value; a pin's 0 or
1), `true`/`false`, `high`/`low` for a pin, or a label, compared with its
raw value with `==` or `!=` only: of the field's value table (`CAN_VAL`), or
of a symbol's enum, from the image's DWARF or its state view's table
([state-view.md](state-view.md); the contract's `labels`), so
`symbol:ams.g_state_telemetry == Precharge` reads as the firmware does.
`op` defaults to `==`.

**Results** are the run's `summary.expects`, one per expect:
`{index, name, check, signal, at_us, until_us, passed, t_us, value, detail}`
with the evidence (`t_us`, `value`): the first match, the first violation,
the worst gap or the count. A run with a failed expect ends `failed`; a
run whose expects all hold, `passed`. Each is also a `log` record at the
run's end.

## Checks

A scenario is checked before it is saved, on every edit in the editor
(`POST .../preview`), and when a run is queued:

- **Schema and injection** (`RunScenario`): names, buses, boards and pins by
  pattern, `data` as hex, signals by grammar, labels by pattern, numbers in
  range; unknown keys refused. Nothing in a scenario reaches the emulator
  but as one of these.
- **Against the system**: each bus, board and pin exists, and a pin is of
  the kind the row needs (gpio or analog); a stop names a periodic started
  no later; an expect ends by the run's end.
- **Against the firmware**, where its build is here: an expect's message,
  field and label are in the contract, a symbol is in the ELF and its label
  in its enum; a value outside a field's range and a frame whose length
  isn't its message's are warnings. Firmware not built here leaves these to
  the run (a warning).
- **Limits** ([deploy.md](deploy.md#limits)): `VHIL_MAX_STIMULI`,
  `VHIL_MAX_WATCHES`, `VHIL_MAX_EXPECTS`, `VHIL_MAX_VIRTUAL_MS`.

## The suite

Every committed scenario is a test of the vHIL's own suite,
`tests/scenarios/test_scenarios.py`: one test per file, run on Renode from
power-on as the worker runs it, its expects checked against the trace with
the contract the images' own `.def` files give. A failing test lists every
expect with its evidence; each expect is also a JUnit property of its test.
A scenario whose system's images are missing is skipped.

```sh
scripts/vhil-docker.sh scenarios               # in Docker, the last built firmware; JUnit in results/
python -m pytest tests/scenarios -o junit_family=xunit1 --junitxml=scenarios.xml
```

CI runs it in `.github/workflows/scenarios.yml` with `full-ci`, nightly on
`dev` and on dispatch. It is **advisory** for now (owner decision): a
failing scenario is a warning annotation and a row of the step summary's
table, not a failed check. The seeds, which pass against the firmware the
systems declare:

| Scenario | What it checks |
|---|---|
| `ams/tsms-precharge-run` | Car mode: TSMS and a DASH_CHG press arm Precharge (AIR- and PRE close, `g_state_telemetry` 1); the link following to the pack gives Run (AIR+ closes, PRE opens; `AMS_status.fsm_state` 3 and `AMS_relay_status.air_positive` on CAN); never Error; AMS_OK held; `AMS_status` every 500 ms |
| `ecu/heartbeat-r2d` | Nothing on the bus in the bootloader's window; `0x100` every 10 ms on the ACU bus only, `0x704` every second; the inverter's DC-bus report and the AMS's `ok_precharge` reach WaitStartBrake; START without the brake sounds no RTDS; with it, R2dDelay and 2 s of RTDS, then WaitInvStandby |

## In the editor

The workspace's Scenario tab (`docs/architecture/editor-workspace.md`,
feature 1) edits the open system's scenarios: a timeline (a lane per bus and
per board; drag a mark to move it, 1/5/10 ms snap, Ctrl+wheel zoom; the
watches below) over the row table and a row editor, where frames are edited
as the contract's decoded fields. The top bar's pick is what Run runs; when
the run ends it is replayed on the tab, each expect's result on its lane at
its evidence time (a click moves the scrubber there) over the frames and pin
edges the run produced. The Tests view in the sidebar lists the scenarios
with their last results.

## API

| | |
|---|---|
| `GET /api/systems/{id}/contract?branch=&fw=<image>=<ref>` | the CAN contract the system's firmware speaks, without a run, and which images are built; each board's state view (`state`) and the value labels of its symbols (`labels`) |
| `GET /api/firmware/{id}/enums?ref=` | an image's DWARF enums and enum-typed globals ([state-view.md](state-view.md)) |
| `GET /api/systems/{id}/scenarios?branch=` | its scenarios, each with its last run (`last_run`) |
| `GET /api/scenarios` | every system's, as checked out |
| `GET /api/systems/{id}/scenarios/{name}?branch=` | one: `{yaml, scenario, errors, warnings}` |
| `POST /api/systems/{id}/scenarios/{name}/preview` | `{yaml}` or `{scenario}` → the same, nothing saved |
| `PUT /api/systems/{id}/scenarios/{name}` | `{scenario` or `yaml, message, branch, base?}` → a commit on `branch` |
