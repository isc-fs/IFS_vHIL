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

```yaml
kind: scenario
system: ams
description: "TSMS on and a press: Precharge, then Run once the link follows"
virtual_ms: 7000
stimuli:
  - {kind: can_periodic, name: vcu, at_ms: 2500, bus: can_acu, id: 0x100, data: "000002", period_ms: 10}
  - {kind: gpio, name: tsms, at_ms: 4500, board: ams, pin: PF9, level: true}
  - {kind: gpio, at_ms: 4700, board: ams, pin: PF10, level: true}
  - {kind: gpio, at_ms: 4750, board: ams, pin: PF10, level: false}
  - {kind: stop_periodic, at_ms: 5800, periodic: vcu}
  - {kind: can_periodic, name: vcu-link, at_ms: 5800, bus: can_acu, id: 0x100, data: "640102", period_ms: 10}
watch:
  - {kind: symbol, board: ams, name: g_state_telemetry, size: 1, period_ms: 10}
expect:
  - {check: eventually, name: precharge, at_ms: 4700, until_ms: 4800, signal: "symbol:ams.g_state_telemetry", value: 1}
  - {check: eventually, name: run-on-can, at_ms: 5800, until_ms: 6900, signal: "frame:can_acu.AMS_status.fsm_state", value: 3}
  - {check: always, name: air-plus-closed, at_ms: 6000, signal: "pin:ams.PB5", value: high}
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
| `watch: symbol` | `board`, `name`, `size` (1, 2, 4), `period_ms` | samples a firmware global |
| `watch: pin` | `board`, `pin` | records its edges |

A frame the scenario sends is in the trace with `src: "stimulus"`.

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
1), `true`/`false`, `high`/`low` for a pin, or a label of the field's value
table (`CAN_VAL`), compared with its raw value with `==` or `!=` only.
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
  field and label are in the contract, a symbol is in the ELF; a value
  outside a field's range and a frame whose length isn't its message's
  are warnings. Firmware not built here leaves these to the run (a warning).
- **Limits** ([deploy.md](deploy.md#limits)): `VHIL_MAX_STIMULI`,
  `VHIL_MAX_WATCHES`, `VHIL_MAX_EXPECTS`, `VHIL_MAX_VIRTUAL_MS`.

## API

| | |
|---|---|
| `GET /api/systems/{id}/contract?branch=&fw=<image>=<ref>` | the CAN contract the system's firmware speaks, without a run, and which images are built |
| `GET /api/systems/{id}/scenarios?branch=` | its scenarios, each with its last run (`last_run`) |
| `GET /api/scenarios` | every system's, as checked out |
| `GET /api/systems/{id}/scenarios/{name}?branch=` | one: `{yaml, scenario, errors, warnings}` |
| `POST /api/systems/{id}/scenarios/{name}/preview` | `{yaml}` or `{scenario}` → the same, nothing saved |
| `PUT /api/systems/{id}/scenarios/{name}` | `{scenario` or `yaml, message, branch, base?}` → a commit on `branch` |
