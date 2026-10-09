# Integration contract

What an external simulation platform can rely on when it drives the virtual
bench or reads its runs: the planned car electrical plant (M8, #147), the
IFSSIM FMU integration (M9), MingoCIL, or any tool that writes scenarios and
reads traces. This page lists the surfaces, says what is stable about each
and how changes are versioned; each surface's own page is the reference, and
this one doesn't repeat it.

## Surfaces

| Surface | What it is | Reference | Source of truth |
|---|---|---|---|
| System file | boards, buses, wires, devices, the time quantum and the co-sim `port:` | [`CLAUDE.md`](../CLAUDE.md#systems-are-data), `python -m vhil.system validate` | [`schemas/vhil.schema.json`](../schemas/vhil.schema.json), `vhil/system.py` |
| Co-simulation port | a plant's signals (`analog`, `gpio_in`, `gpio_out`, `can_rx`, `can_tx`) exchanged in lock-step at each `step_ms` | [`cosim.md`](cosim.md) | `vhil/cosim.py` |
| Scenario | a run as data: timed stimuli, watches, expects | [`scenarios.md`](scenarios.md) | `RunScenario` and its rows in `vhil/server/runs.py` |
| Session ops | a live run's channel: the scenario's stimulus kinds without their time, plus `pause`, `resume`, `stop`, `keepalive`, `debug` | [`live-session.md`](live-session.md), [`debugger.md`](debugger.md) | `vhil/server/session.py` |
| Trace | a run's records in virtual time, a JSON line each | [below](#trace-records) | `vhil/worker.py`, `vhil/server/runs.py` (`TRACE_KINDS`, `trace_header`) |
| Contract | what each board's firmware puts on its buses (from its `.def` files), its state view, value labels and inputs, the sync quantum | [`state-view.md`](state-view.md), [`scenarios.md`](scenarios.md#api) | `system_contract` in `vhil/server/decode.py` |
| HTTP API | runs, traces, artifacts, scenarios, contracts | [`architecture/m5-web-app.md`](architecture/m5-web-app.md#api-contract-v1), [`scenarios.md`](scenarios.md#api) | `vhil/server/` |

Not part of the contract, free to change in any PR: the editor's graph and
Pipeline Manager's JSON-RPC (`vhil/editor.py`, its `FORMAT_VERSION` is
Pipeline Manager's own), the emulator's monitor commands and the C# models'
methods (`models/renode/`), the generated `.resc` scripts, the database
schema, and `vhil.sim`'s Python API below `Sim`, `Port` and the plant
interface.

## Time

Every time in every surface is virtual time from the system's power-on: µs in
the trace and the API (`t_us`, `at_us`, `virtual_us`), ms in a scenario's
rows (`at_ms`, `virtual_ms`) and in the port's `step_ms`. Wall time appears
only as a live session's pacing (`clock` records' `rtf`, `wall_s`).

- **Power-on is not the app's start.** Every MainLite boots through its CAN
  bootloader and spends its 2 s auto-jump window first (CLAUDE.md,
  invariant 5): a stimulus before then meets the bootloader.
- **Sync points.** The boards meet every `time.quantum_s` of the system
  (500 µs in `systems/*.yaml`). A stimulus takes effect at a sync point: a
  scenario row between two runs at the next one, reported
  ([`scenarios.md`](scenarios.md#times)); between boards, what a firmware
  sees can lag by up to one quantum ([`can-bus.md`](can-bus.md#virtual-time-and-the-sync-quantum)).
  The contract carries the quantum as `sync_quantum_us`.
- **Determinism.** The same system, firmware, scenario (or plants and their
  inputs) give the same records at the same virtual times, at any emulation
  speed. A plant computing between steps holds virtual time still
  ([`cosim.md`](cosim.md#time)).

## Trace records

A run's `trace.jsonl` (`GET /api/runs/{id}/trace`, paged; `WS
/api/runs/{id}/live`, as written). Its first line is the run header; every
other record has a `kind` and its `t_us`. The worker writes a slice at a
time: a slice's records come after every earlier slice's, and inside a slice
they are grouped by kind, not sorted, so a reader that needs one timeline
sorts by `t_us`.

| `kind` | Fields | Written | Reference |
|---|---|---|---|
| `run` (header) | `t_us: 0`, `run`, `token`, `attempt`, `contract` | first line, once per attempt | `trace_header` |
| `frame` | `bus`, `id`, `ext`, `data` (hex), `src?` (`"stimulus"`: the scenario's or the session's own frame) | every frame on a bus | [`m5-web-app.md`](architecture/m5-web-app.md#api-contract-v1) |
| `edge` | `board`, `pin`, `level`, `initial?` (the level as watching starts) | a watched pin's changes | [`scenarios.md`](scenarios.md#rows) |
| `sample` | `board`, `name`, `value` | a watched symbol, at its period (at sync points) | [`state-view.md`](state-view.md#what-a-run-records) |
| `radio` | `board`, `device`, `payload` (hex) | every payload a radio device (a model with `interface.radio`, e.g. the ECU's nRF24L01+) sent, at the end of its packet | [`backplanes/ecu.md`](backplanes/ecu.md#in-the-vhil) |
| `log` | `text`, `source?`, `level?` (`debug`, `info`, `warning`, `error`), `board?`, `wall_s?`, `dropped?` | the worker's notes, the firmware build, Renode's log (filtered), each board's UARTs, stimuli, expects' results, as they happen | [`live-session.md`](live-session.md#logs) |
| `bus_load` | `bus`, `load` (0..1), `window_us`, `exact` (false on Renode's hub: an estimate) | per bus and slice | [`can-bus.md`](can-bus.md#what-the-bus-does) |
| `op` | `op_id`, `op`, `status` (`applied`, `refused`), `login`, `detail?` | a live session's op, at the time it took effect | [`live-session.md`](live-session.md#the-channel) |
| `clock` | `rtf`, `paused`, `wall_s`, `idle_left_s`, `debug?` (where the system is held) | a live session, per slice | [`live-session.md`](live-session.md#the-run) |
| `debug` | `board`, `event` (`attached`, `breakpoints`, `stopped`, `running`, `detached`), and a stop's `at_us`, `reason`, `frame`, `frames`, `locals`, `registers`, `watches` | a live session's debugger | [`debugger.md`](debugger.md#in-the-trace) |

The live WebSocket ends with `{"kind": "end", "state"}`, which is not in the
file. A trace stays raw: frames are not decoded in it; the run's contract
(`GET /api/runs/{id}/contract`) decodes them, in the browser and in
`vhil/expect.py` alike.

## Versioning

One integer, `CONTRACT_VERSION` (`vhil/server/runs.py`), covers every surface
above. It is in each trace's header (`contract`), in `GET /api/health`
(`contract`) and in a live session's hello (`contract`).

- **Additions keep it:** a new field, record kind, stimulus kind, op, signal
  kind or endpoint. A reader ignores fields and record kinds it doesn't
  know.
- **A removal, a rename or a change of meaning bumps it** (a field's unit, a
  time's origin, when a stimulus takes effect), in the PR that makes the
  change, with this page and the surface's page updated in it, and a note
  here under [Changes](#changes).
- **Traces carry their version.** A run is replayed and decoded by the
  version its header names; a trace without `contract` predates the field
  and reads as version 1.
- **Not yet built, so not yet versioned:** the co-sim port's transport for an
  external engine ([`cosim.md`](cosim.md#for-an-external-engine-mingocil)).
  Its timing contract above is what the transport will carry: when it is
  built (for MingoCIL, or an FMU's master algorithm, whose communication step
  is the port's `step_ms`), it presents the engine as one more plant and
  documents its wire format here.

## Changes

- **1** (first): the surfaces as listed. Stimuli between sync points run at
  the next one, reported (`summary.aligned`, a `log` record, the check's
  warning; [`scenarios.md`](scenarios.md#times)). Added since, an addition
  that keeps it: the `radio` trace record (#193); the `log` record's
  `source`, `level`, `board`, `wall_s` and `dropped` (live logs: a record
  without `source` is the run's, at `info`).
