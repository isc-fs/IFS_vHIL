# The debugger

Source-level debugging of a board in a [live session](live-session.md):
breakpoints by function, file:line or address; continue, step, next and
finish; the stack, locals, registers, expressions and watches. Feature 4 of
the [editor workspace plan](architecture/editor-workspace.md) (steps 16 and
17). It works in LIVE sessions only: a REPLAY is a recorded trace, with no
emulator behind it to stop, so it shows where a session stopped (the trace's
`debug` records) and nothing more.

## How it runs

```
editor (Debug tab) ── WS /api/runs/{id}/session ──▶ API ── session_ops (DB) ──▶ worker
                                                                                  │
                     worker: vhil.gdb ── MI ── arm-none-eabi-gdb ── RSP, 127.0.0.1 ──▶ Renode GDB stub (one per board)
```

- **Renode's own stub, per board, on demand** (`models/renode/VhilGdb.cs`):
  the first debug op for a board compiles it and starts it with
  `machine StartVhilGdbServer <port> <halt log>`. It is Renode's `GdbStub`
  on Renode's `SocketServerProvider`, with two changes: its listening
  socket is bound to 127.0.0.1 (Renode 1.17's `StartGdbServer` binds every
  interface, with no option), and it answers only `Z z c s g p m ? qSupported
  qXfer qCRC`. Everything else gets the empty reply ("not supported"): no
  `qRcmd` (GDB's `monitor`, which runs Renode monitor commands, and those
  read and write host files), no memory or register writes (`M X P`), no
  `vRun`, kill, reverse execution or Trace32 register access.
- **GDB** (`vhil/gdb.py`): the pinned toolchain's `arm-none-eabi-gdb`
  (14.2.Rel1; it needs `libncurses5`, in the image) or `$VHIL_GDB`
  (`gdb-multiarch` in CI), a subprocess of the worker in MI mode, on the
  board's ELF (built with `-g`), with `-nx -nh`, auto-loading and debuginfod
  off. Only commands built in `vhil/gdb.py` from checked values reach it: a
  location is `{function}`, `{file, line}` or `{address}`; an expression is a
  C variable path (names, `.`, `->`, `[n]`, unary `*` and `&`), so no calls,
  assignments, casts or `$` (convenience variables and functions such as
  `$_shell`).
- **Lockstep.** A halted CPU holds Renode's time source: `emulation RunFor`
  waits until it runs again, and every other board stops at the next sync
  point (the system's time quantum, 0.5 ms). No firmware timeout fires while
  a board is held, on any board: the ECU and AMS HAL ticks keep the same
  difference across a breakpoint held for seconds of wall time
  (`tests/sim/test_debugger.py`). With a debugger attached, the worker runs
  each slice's RunFor in a thread and services the debuggers from its own;
  Renode's stub writes each halt's virtual time to a file, since the monitor
  is busy in that RunFor.
- A breakpoint, clear or watch set while the board runs is **deferred**: GDB
  can't change breakpoints of a running target in all-stop mode, so the
  worker interrupts the board, and when it stops for that (in the next
  slice's RunFor, at once and with no virtual time passing) applies the edit
  and lets it run on. That stop is not shown.

## Ops

A `debug` op on the session channel, `{kind: "debug", board, cmd, ...}`:

| cmd | | result (in its `ack`) |
|---|---|---|
| `attach` | start the board's stub and GDB (`break`, `watches` and `interrupt` attach too) | the board's debug state |
| `detach` | breakpoints deleted, the board runs on, GDB gone | |
| `break` | `location: {function} \| {file, line} \| {address}` | the breakpoint: number, func, file, line, addr |
| `clear` | `number` | |
| `watches` | `exprs`: the expressions to show at every stop (≤ 16) | |
| `interrupt` | stop the board where it is | |
| `continue`, `next`, `step`, `finish` | from a stop | |
| `frames`, `registers` | a stopped board's stack, registers | the list |
| `locals` | `frame?` | `[{name, type, value?, arg?}]` |
| `eval` | `expr`, `frame?` | `{expr, value}` |
| `disassemble` | `address?` (default the pc) | `[{addr, func, offset, inst}]` |

Queries refuse a board that runs ("break first"). While the system is held at
a breakpoint, the worker takes debug ops and the controls (`stop`, `resume`,
which continues every stopped board, `pause`, `keepalive`) as they come;
stimuli wait for the next slice boundary. A settled op's result is in the
`session_ops` table's `result` column (JSON, ≤ 64 KiB) and in its `ack`.
Queries leave no `op` record in the trace; the other debug ops do. Debug ops
are never in a session's recording: a scenario replays without them.

## In the trace

```json
{"kind": "debug", "t_us": 2150000, "board": "ecu", "event": "stopped", "at_us": 2151501,
 "reason": "breakpoint-hit", "bkpt": 1,
 "frame": {"func": "ecu::Controller::step", "file": "/vhil/fw/ecu@dev/Core/Src/app/control.cpp", "line": 35, "addr": "0x0802310a"},
 "frames": [...], "locals": [...], "registers": [...], "watches": [...]}
{"kind": "clock", "t_us": 2150000, "paused": true, "rtf": 0, "debug": {"board": "ecu", "reason": "breakpoint-hit", "func": "ecu::Controller::step", "file": "...", "line": 35, "addr": "0x0802310a"}}
{"kind": "debug", "t_us": 2150000, "board": "", "event": "running"}
```

`event` is `attached`, `breakpoints` (the board's breakpoints and watches
after a change), `stopped`, `running` or `detached`. A stop's `t_us` is the
slice boundary before it, so the file stays in virtual-time order; `at_us`
is when the board halted (Renode's machine time). The paused `clock` record
is what the top bar shows as PAUSED, with where.

## Security

Who can reach what:

- **The stub** listens on 127.0.0.1 inside the worker's network namespace,
  one per debugged board, on a free port. Workers have no inbound route
  (`jobs` is an internal network; deploy/compose.prod.yaml), and the API
  never connects to a worker: nothing outside the worker container can reach
  it. Inside the container, it is as reachable as Renode's monitor already is
  (#135: loopback only): by processes of the worker itself, which runs one run
  at a time. What it answers is read-only plus breakpoints and stepping, so
  even a local client can't write memory or run monitor commands through it.
- **The transport** is the session channel that already exists: the browser
  speaks only to the API (`WS /api/runs/{id}/session`, same origin, CSRF on
  the hello in github mode, control held by the run's owner or an admin as a
  lease, rate-limited), the API checks the op (`vhil.gdb.check_op`) and
  queues it in the database, and the worker takes it. The API and the workers
  still share only the database and the runs volume (#134's model is
  unchanged: no new port, no new network, no route from the API to a
  worker). Results come back through the same table and the trace.
- **GDB** gets only commands `vhil/gdb.py` builds from checked fields, so no
  op can reach `monitor`, `shell`, `python`, `source`, file or memory-write
  commands, inferior calls or convenience functions; and the stub refuses
  those packets anyway.
- **Sources** for the Debug tab are served by the API from the firmware
  checkout of the board's image in the fw volume (mounted read-only there),
  only under that checkout, only a source-file name, never through a symlink
  that leaves it (step 17).
- **Viewers** of a run see its `debug` records (stops, locals, registers) in
  the trace, as they see its frames; only the controlling connection sends
  debug ops.

## Limits

- One GDB per board, all-stop: while any board is held, the others are held
  too, and a board that runs can't be changed until the held one goes on.
- The stack is the current FreeRTOS task's; other tasks' stacks aren't shown.
- Stepping over a call that blocks (`vTaskDelay`) runs the system, in
  lockstep, until the step completes.
- Sources are the fw volume's checkout of the image's ref, which a rebuild of
  that ref replaces (the image is reused by ref name: docs/deploy.md).
