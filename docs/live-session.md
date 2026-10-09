# Live sessions

A live session is the bench at your desk: a run of a system that goes on
until you stop it, paced to wall time, which you drive while it runs: send
CAN frames once or periodically, flip GPIO inputs, set analog voltages,
start watching a symbol or a pin, pause and resume. Feature 3 of the
[editor workspace plan](architecture/editor-workspace.md) (steps 14 and 15).
Every op is applied in virtual time and written into the run's trace, so a
session is recorded as it happens and saves as a [scenario](scenarios.md)
that replays it exactly.

## The run

A live session is a normal run (`POST /api/runs`, the same queue, worker and
history) whose `run` scenario says `live: true`:

```json
{"system": "ams", "scenario": {"kind": "run", "live": true}}
```

| | |
|---|---|
| `virtual_ms` | a cap, not a length: default and limit `VHIL_MAX_LIVE_MS` (1 h) |
| `slice_ms` | default 50: the trace is flushed, and ops are taken, every 50 ms of virtual time |
| `stimuli`, `watch`, `expect` | as any run's: a session may start from a scenario's rows |

The worker (`vhil/worker.py`, `execute_run(..., session=LiveSession)`)
records every board's [state view](state-view.md), as every web-app run does,
and paces virtual time to wall time at each slice boundary: when the
emulation runs faster than real time it waits, and when it runs slower it
goes as fast as it can and never sprints to catch up (a deficit over 250 ms
is forgiven, as `models/renode/VhilPacer.cs` does for the bench, and the
next slice runs at once). Each slice
writes a `clock` record:

```json
{"kind": "clock", "t_us": 2550000, "rtf": 0.98, "paused": false, "wall_s": 2.71, "idle_left_s": 1799}
```

`rtf` is virtual over wall time across the last second (0 while paused). A
session ends when it is stopped (`stop`), when it is idle for
`VHIL_LIVE_IDLE_S` (30 min without an op; a `keepalive` op counts), when it
reaches its cap, or when it is cancelled (`POST /api/runs/{id}/cancel`). The
first three end it `passed` (`summary.stopped`: `op`, `idle` or `end`); its
summary counts `ops_applied` and `ops_refused`.

## The channel

```
WS /api/runs/{id}/session
```

Behind the app's guard (a session; same origin, so no other site can open it:
`vhil/server/auth.py`). The client speaks first:

| Client → server | |
|---|---|
| `{kind: "hello", csrf?, control?}` | first message. `csrf`: the session's CSRF token (`vhil_csrf` cookie), required in github mode to take control; `control: false` only watches |
| `{kind: "op", cid?, op}` | an op (below); `cid` is echoed back |
| `{kind: "take"}` | take control, if nobody holds it |

| Server → client | |
|---|---|
| `{kind: "hello", run, live, state, role, holder, may_control, limits, slice_ms}` | `role`: `control` or `view`; `holder`: who controls; `limits`: `ops_per_s`, `max_periodic`, `idle_s`, `max_ops` |
| `{kind: "queued", cid, op_id}` | the op is in; the worker takes it at its next slice boundary |
| `{kind: "refused", cid, detail}` | refused here: its shape, the system, the rate, or the connection only watches |
| `{kind: "ack", op_id, op, status, at_us, detail, login}` | the worker applied it (`at_us`: when, in virtual time) or refused it (`detail`) |
| `{kind: "control", role, holder}` | control changed hands |
| `{kind: "end", state}` | the run ended; the socket closes |

**Control.** One live session per run (owner decision): one connection
controls it, every other one watches. Control may be held by the run's owner
or an admin (`VHIL_ADMINS`; anyone in dev mode), and in github mode only by a
connection whose hello carried the CSRF token. It is a lease in the database
(`session_control`, 15 s, renewed every 5 s while the connection is open,
dropped when it closes), so the owner's second tab watches too, and takes
over (`take`) once the first one closes.

**Ops** are exactly the scenario stimulus kinds, without their time (the
worker gives it), and four controls:

| Op | |
|---|---|
| `can_send`, `can_periodic`, `stop_periodic`, `gpio`, `analog`, `watch` | as the [scenario rows](scenarios.md#rows), checked by the same models and against the run's system; a `can_periodic` needs a `name`, no `until_ms`, and a name once per session (a recording must stay a valid scenario) |
| `pause`, `resume` | hold virtual time where it is, and go on |
| `stop` | end the session at the end of the next slice |
| `keepalive` | nothing, but the session is not idle |
| `debug` | a board's debugger: breakpoints, stepping, the stack, locals and registers ([debugger.md](debugger.md)); never in the recording |

**Where an op takes effect.** The API and the workers share only the
database and the runs volume (docs/deploy.md), so an op goes through the
`session_ops` table: the API inserts it as pending; at its next slice
boundary the worker takes the pending ops in order and schedules each
stimulus at the end of the slice it is about to run, with exactly the
scheduling a scenario row at that time gets (`execute_run`'s `schedule`), and
settles the row (`applied` with that time, or `refused` with why). So an op
takes effect between one and two slices (50 to 100 ms of virtual time) after
it was sent, at a slice boundary, the same way however fast the emulation
runs. Pause, resume and keepalive take effect at the boundary itself. A
slice end is a sync point (slices are whole ms, the quantum 500 µs), so an
op is never moved the way a scenario row between two sync points is
([scenarios.md](scenarios.md#times)), and its recording replays at the
same times.

**In the trace,** every op is an `op` record at the virtual time it took
effect (a refused one at the boundary that refused it):

```json
{"kind": "op", "t_us": 2550000, "op_id": 12, "op": {"kind": "gpio", "board": "ams", "pin": "PF9", "level": true}, "status": "applied", "login": "raul"}
```

and the frames a `can_send` or `can_periodic` puts on the bus are frame
records with `src: "stimulus"`, as a scenario's are. The live WebSocket
(`/api/runs/{id}/live`) polls a live run's trace every 50 ms (200 ms for
other runs).

## Recording

`GET /api/runs/{id}/session/scenario` turns a session's applied stimuli
(its trace's `op` records) into a scenario's data: each row at the time it
took effect, the session's slice, and `virtual_ms` to where it ended. The
editor's "Save session as scenario" opens it in the Scenario tab, and
Commit… saves it like any scenario. Run, it gives the session's trace again:
the same frames, edges and samples at the same virtual times
(`tests/sim/test_ams_live_session.py` on the AMS, from Start through Precharge to
Run; `tests/unit/test_session.py` on the fake Sim). A session longer than
`VHIL_MAX_VIRTUAL_MS` (10 min) records a scenario over that limit: the
Scenario tab says so.

## In the editor

● Live in the top bar starts a session of the saved system (step 15 of the
plan; `editor/pipeline-manager/CHANGELOG-VHIL.md`). The mode pill reads LIVE
(PAUSED while paused), the topology is locked, and the top bar has the clock
(`t=12.345 s · RTF 0.98×`), who controls the session, Pause/Resume (Space)
and Stop. The Bus tab's Monitor and Trace stream, and its Send view composes
a frame from the contract (or raw hex) and sends it once or periodically,
with the senders running listed (Stop, Stop all) and a banner while any
runs. A board's inputs (its role's routed GPIO inputs as switches, analog
inputs as voltages; the contract's `inputs`) are in the inspector and in its
context menu ("Inputs and analog…"). The State tab, the inspector's card and
the node pill follow the clock; bus rails show frames a second and their CAN
wires march while they carry traffic. Another tab that opens the run
watches. When the session ends, it stays on the workspace as a REPLAY, and
"Save as scenario…" puts its recording in the Scenario tab.

## Logs

Every run streams its logs into its trace as `log` records while it runs
(`vhil/runlog.py`; a live session, a scenario's run and a plain run alike,
and a finished run's replay reads them back). Each says where it came from
and how loud it is:

```json
{"kind": "log", "t_us": 2300000, "source": "renode", "level": "warning", "board": "ams", "text": "watchdog: Watchdog reset triggered!"}
```

| `source` | What | `t_us` |
|---|---|---|
| `worker` | the worker's notes: the system, the firmware it runs, an attempt, an error | virtual time; 0 before power-on, with `wall_s` |
| `build` | `python -m vhil.system build` line by line (git, cmake, the compiler) while a run waits for a firmware it hasn't built; whole in the `build.log` artifact | 0, with `wall_s` |
| `pytest` | a `pytest` run's output; whole in `pytest.txt` | 0, with `wall_s` |
| `renode` | Renode's log, filtered (below); whole in `renode.log`. Also each board's jump from its bootloader to its app, and back to it on a reset (its PC at the slice boundary) | the end of the slice it was read in |
| `<board>.<UART>` | what the firmware sends on a UART of its board's catalogue (`ecu.USART10`), captured by Renode's UART file backend without changing the firmware; whole in `uart/<board>.<UART>.txt` | the end of the slice it was read in |
| `scenario` | stimuli, and each expect's result (`error` when it failed) | when it took effect |
| `session` | a live session's own notes (idle, stopping) | when it happened |

**Time.** Virtual time as every record's, except before power-on: a build or
a pytest run has no virtual time, so its records are at `t_us` 0 and carry
`wall_s`, the Unix time the worker wrote them. A line the worker reads from a
file (Renode's log, a UART) is stamped with the end of the slice it was read
in, at most one slice (`slice_ms`: 50 ms live, 100 ms otherwise) after the
firmware wrote it; the trace stays in virtual-time order.

**Renode's filter** (`RenodeFilter`) keeps what a user wants to see live and
leaves the rest in `renode.log`: errors (with a few lines of a stack after
them); the watchdog firing, every time; a machine starting and a CPU
starting (a power-on or a reset; Renode's `Setting initial values`); each
unmodelled access the peripheral guard's rules (`configs/peripherals.yaml`,
invariant 7) don't explain, once per register; any other warning the first
time it is said (numbers aside), its repeats counted in one record at the
end. Explained accesses, info and the host's own warnings (the translation
cache) stay out.

**Rate limit.** At most `VHIL_LOG_LINES_PER_S` (20) lines a second per
source, in virtual seconds (wall seconds for `wall_s` records); the lines
over it are counted in one `warning` record per second with `dropped: N`,
so a UART printing every millisecond can't flood the trace or the socket.
Errors always pass.

**Firmware consoles today.** No board's dev firmware prints a console: the
AMS initialises USART2 (`IFS08-CE-AMS Core/Src/main.c:663`) and sends
nothing on it, and its `_write` goes to a weak `__io_putchar` it never
defines (`Core/Src/syscalls.c:35,80`); the ECU's only transmitter is
USART10, the PMTK commands it sends its GPS at start
(`IFS08-CE-ECU Core/Src/app/gps_task.cpp:96`), which show as
`ecu.USART10`. A UART a firmware starts printing on needs only its entry in
the board's `uart` table (`catalog/boards/mainlite.yaml`) and its peripheral
in the platform.

In the editor, the Log tab shows each line's time, source and level
(warnings and errors coloured), a chip per source to hide or show its lines,
and a link to download the run's whole `renode.log`.

## Limits

| Variable | Bounds | |
|---|---|---|
| `VHIL_MAX_LIVE_MS` [3600000] | a live run's virtual time | 422 |
| `VHIL_LIVE_IDLE_S` [1800] | wall time without an op before the worker stops the session | stops `passed`, `stopped: idle` |
| `VHIL_LIVE_OPS_PER_S` [20] | ops a second per connection (a token bucket) | refused |
| `VHIL_LIVE_MAX_PERIODIC` [16] | periodic senders running at once | refused by the worker |
| `VHIL_MAX_STIMULI` [1000] | stimulus ops in one session (its recording's rows) | refused |
| `VHIL_LOG_LINES_PER_S` [20] | log lines a second per source ([Logs](#logs)) | counted, not shown |

The run's own limits hold too ([deploy.md](deploy.md#limits)): the active
runs per user and in all, and `VHIL_MAX_TRACE_MB` (a 1 ms periodic for an
hour fills about 400 MB of trace).
