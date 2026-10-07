"""The debugger's plumbing (step 16 of docs/architecture/editor-workspace.md;
docs/debugger.md): GDB/MI parsing, what may reach GDB, the `debug` op, and
the worker holding a live session at a stop. Against the fake Sim of
test_runs.py and a fake Debugger; the real ones are tests/sim/test_debugger.py."""
import json
import sqlite3

import pytest

pytest.importorskip("fastapi")

from tests.unit.test_runs import FakeSim  # noqa: E402
from vhil import gdb  # noqa: E402
from vhil.gdb import DebugError, DebugHub, check_op, expression, location, parse_line  # noqa: E402
from vhil.server.runs import read_trace  # noqa: E402
from vhil.server.session import OpError, SessionStore, ack, parse_op  # noqa: E402
from vhil.system import REPO, System  # noqa: E402
from vhil.worker import TraceWriter, execute_run  # noqa: E402

ECU_AMS = REPO / "systems" / "ecu-ams.yaml"


# -- MI ------------------------------------------------------------------------------

def test_mi_records_parse_into_values():
    rec = parse_line('*stopped,reason="breakpoint-hit",disp="keep",bkptno="1",frame={addr='
                     '"0x0802310a",func="ecu::Controller::step",args=[{name="this",value='
                     '"0x24003404 <ucHeap+6308>"},{name="now_ms",value="200"}],file="control.cpp",'
                     'fullname="/vhil/fw/ecu@dev/Core/Src/app/control.cpp",line="35",arch='
                     '"armv7e-m"},thread-id="1",stopped-threads="all"')
    assert rec["type"] == "exec" and rec["class"] == "stopped"
    r = rec["results"]
    assert r["reason"] == "breakpoint-hit" and r["bkptno"] == "1"
    assert r["frame"]["args"][1] == {"name": "now_ms", "value": "200"}
    assert gdb.frame_of(r["frame"]) == {"func": "ecu::Controller::step", "addr": "0x0802310a",
                                        "file": "/vhil/fw/ecu@dev/Core/Src/app/control.cpp",
                                        "line": 35}
    res = parse_line('5^done,variables=[{name="a1",type="const uint8_t",value="200 \'\\\\310\'"}]')
    assert res["token"] == 5 and res["class"] == "done"
    assert res["results"]["variables"][0]["value"] == "200 '\\310'"
    assert parse_line('~"Breakpoint 1, at x.c:3\\n"') == {"type": "console",
                                                          "text": "Breakpoint 1, at x.c:3\n"}
    assert parse_line("(gdb) ") is None
    lst = parse_line('7^done,stack=[frame={level="0",func="f"},frame={level="1",func="g"}]')
    assert [f["frame"]["func"] for f in lst["results"]["stack"]] == ["f", "g"]


@pytest.mark.parametrize("loc,want", [
    ({"function": "ecu::Controller::step"}, "ecu::Controller::step"),
    ({"function": "HAL_GetTick"}, "HAL_GetTick"),
    ({"file": "Core/Src/app/control.cpp", "line": 35}, "Core/Src/app/control.cpp:35"),
    ({"file": "/vhil/fw/ecu@dev/Core/Src/main.c", "line": 1}, "/vhil/fw/ecu@dev/Core/Src/main.c:1"),
    ({"address": "0x0802310a"}, "*0x0802310a"),
    ({"address": 0x08000000}, "*0x08000000"),
])
def test_a_location_is_a_function_a_line_or_an_address(loc, want):
    assert location(loc) == want


@pytest.mark.parametrize("loc", [
    {"function": "f; monitor help"}, {"function": "f\nquit"}, {"function": "$_shell"},
    {"file": "../../etc/passwd", "line": 1}, {"file": "a.c", "line": "1"},
    {"file": "a.c", "line": 0}, {"file": "a b.c", "line": 3}, {"address": "0x1 if 1"},
    {"address": -1}, {"function": "f", "file": "a.c"}, "main", None, {},
])
def test_anything_else_is_refused_before_gdb(loc):
    with pytest.raises(DebugError):
        location(loc)


@pytest.mark.parametrize("expr", ["now_ms", "in.apps1_raw", "this->state_", "cells[3].v",
                                  "*p", "&g_state", "ecu::g_cfg.cal.apps1_min"])
def test_an_expression_is_a_variable_path(expr):
    assert expression(expr) == expr


@pytest.mark.parametrize("expr", ["x=1", "x = 1", "f()", "$_shell(\"id\")", "$pc", "(int)x",
                                  "x++", "a,b", "{int}0x2000", "x\nmonitor", "sizeof x", "",
                                  "a[i]", "x+1"])
def test_calls_assignments_casts_and_convenience_variables_are_refused(expr):
    with pytest.raises(DebugError):
        expression(expr)


# -- the op ---------------------------------------------------------------------------

def test_a_debug_op_is_checked_against_the_system():
    system = System(ECU_AMS)
    assert parse_op({"kind": "debug", "board": "ecu", "cmd": "break",
                     "location": {"function": "main"}}, system) == \
        {"kind": "debug", "board": "ecu", "cmd": "break", "location": {"function": "main"}}
    assert parse_op({"kind": "debug", "board": "ams", "cmd": "eval", "expr": "uwTick",
                     "frame": 2}, system)["frame"] == 2
    assert parse_op({"kind": "debug", "board": "ecu", "cmd": "disassemble",
                     "address": "0x08020000"}, system)["address"] == 0x08020000
    for op, needle in [
        ({"kind": "debug", "board": "udv", "cmd": "continue"}, "no board"),
        ({"kind": "debug", "board": "ecu", "cmd": "monitor"}, "unknown debug cmd"),
        ({"kind": "debug", "board": "ecu", "cmd": "continue", "location": {}}, "takes no"),
        ({"kind": "debug", "board": "ecu", "cmd": "eval", "expr": "x=1"}, "variable path"),
        ({"kind": "debug", "board": "ecu", "cmd": "watches", "exprs": ["a"] * 17}, "at most"),
        ({"kind": "debug", "board": "ecu", "cmd": "locals", "frame": 99}, "not a frame"),
    ]:
        with pytest.raises(OpError, match=needle):
            parse_op(op, system)


def test_a_debug_ops_result_comes_back_with_its_ack(tmp_path):
    store = SessionStore(tmp_path / "vhil.db")
    op_id = store.add_op(1, {"kind": "debug", "board": "ecu", "cmd": "locals"}, "raul")
    store.settle(1, [(op_id, "applied", 2_000_000, "", [{"name": "now_ms", "value": "3"}])])
    row = store.settled_after(1, 0)[0]
    assert ack(row)["result"] == [{"name": "now_ms", "value": "3"}]
    big = store.add_op(1, {"kind": "debug", "board": "ecu", "cmd": "locals"}, "raul")
    store.settle(1, [(big, "applied", 1, "", ["x" * 70_000])])
    assert "over" in store.settled_after(1, op_id)[0]["result"]["error"]
    plain = store.add_op(1, {"kind": "pause"}, "raul")
    store.settle(1, [(plain, "applied", 1, "")])
    assert "result" not in ack(store.settled_after(1, big)[0])


def test_a_database_from_before_the_debugger_gains_the_result_column(tmp_path):
    db = sqlite3.connect(tmp_path / "vhil.db")
    db.executescript("CREATE TABLE session_ops (id INTEGER PRIMARY KEY AUTOINCREMENT, run INTEGER "
                     "NOT NULL, op TEXT NOT NULL, login TEXT NOT NULL DEFAULT '', created REAL "
                     "NOT NULL, state TEXT NOT NULL DEFAULT 'pending', at_us INTEGER, detail TEXT "
                     "NOT NULL DEFAULT '');")
    db.close()
    store = SessionStore(tmp_path / "vhil.db")
    op_id = store.add_op(1, {"kind": "keepalive"})
    store.settle(1, [(op_id, "applied", 1, "", {"ok": True})])
    assert store.settled_after(1, 0)[0]["result"] == {"ok": True}


# -- the worker, with a fake debugger ------------------------------------------------

class FakeDebugger:
    """Stops at a breakpoint's `hit_us` once virtual time passes it, and at
    an interrupt at the next RunFor; reads locals as a stopped board."""
    hit_us = 2_120_000

    def __init__(self, sim, board, workdir):
        self.sim, self.board, self.elf = sim, board, f"/fw/{board}.elf"
        self.state, self.stop, self.breakpoints, self.watches = "detached", None, {}, []
        self._events, self._interrupt, self.closed = [], False, False

    def start(self, resume=True):
        self.state = "running"
        if not resume:      # attaching stops the board, before its next instruction
            self._stopped(reason="attached", frame={"func": "idle", "addr": "0x8"})

    def close(self):
        self.closed, self.state = True, "detached"

    def _stopped(self, **stop):
        self.state, self.stop = "stopped", stop
        self._events.append({"event": "stopped", **stop})

    def events(self):
        if self.state == "running":
            if self._interrupt:
                self._interrupt = False
                self._stopped(reason="signal-received", signal="SIGINT",
                              frame={"func": "idle", "addr": "0x8"})
            elif self.breakpoints and self.sim.now >= self.hit_us:
                self.hit_us = 1 << 60
                self._stopped(reason="breakpoint-hit", bkpt=1,
                              frame={"func": "step", "file": "/fw/ecu/control.cpp", "line": 35,
                                     "addr": "0x0802310a"})
        out, self._events = self._events, []
        return out

    def interrupt(self):
        self._interrupt = True

    def break_insert(self, loc):
        n = len(self.breakpoints) + 1
        self.breakpoints[n] = {"number": n, "location": loc}
        return self.breakpoints[n]

    def break_delete(self, n):
        if n not in self.breakpoints:
            raise DebugError(f"no breakpoint {n!r}")
        del self.breakpoints[n]

    def set_watches(self, exprs):
        self.watches = list(exprs)
        return self.watches

    def resume(self, how):
        if self.state != "stopped":
            raise DebugError("running")
        self.state, self.stop = "running", None
        if how == "next":
            self._stopped(reason="end-stepping-range",
                          frame={"func": "step", "file": "/fw/ecu/control.cpp", "line": 36,
                                 "addr": "0x0802311a"})

    def locals(self, frame=0):
        if self.state != "stopped":
            raise DebugError(f"{self.board} is running: break first")
        return [{"name": "now_ms", "value": str(self.sim.now // 1000)}]

    def halt_times(self):
        return [(self.sim.now, "Breakpoint")]

    def snapshot(self):
        return {"frames": [self.stop["frame"]], "locals": self.locals(), "registers": [],
                "watches": [{"expr": e, "value": "1"} for e in self.watches]}


class Driver:
    """Each op once the one before settled and its condition holds."""
    max_periodic, idle_s = 16, 1e9

    def __init__(self, steps):
        self.steps, self.n, self.settled, self.hub, self.waits = steps, 0, [], None, 0
        self.taken = []

    def take(self, now_us=0):
        """The unsettled ops taken so far, and the next one once the last
        debug op settled (a stimulus taken while held waits; the user goes on)."""
        done = {s[0] for s in self.settled}
        out = [r for r in self.taken if r["id"] not in done]
        if any(r["op"]["kind"] == "debug" for r in out):
            return out
        if self.n < len(self.steps) and self.steps[self.n][0](self, now_us):
            self.n += 1
            row = {"id": self.n, "op": self.steps[self.n - 1][1], "login": "dev"}
            self.taken.append(row)
            out.append(row)
        return out

    def settle(self, settled):
        self.settled += settled

    def idle(self):
        return False

    def wait(self):
        self.waits += 1

    def rebase(self, now_us):
        pass

    def pace(self, now_us, until_us):
        pass

    def clock(self, now_us, paused=False):
        return {"kind": "clock", "t_us": now_us, "rtf": 1.0, "paused": paused}


def held(d, _now):
    return d.hub is not None and d.hub.stopped() == ["ecu"]


def at(us):
    return lambda d, now: now >= us


def run_debug(tmp_path, steps):
    sim = FakeSim(ECU_AMS)
    session = Driver(steps)

    def hub(s):
        session.hub = DebugHub(s, tmp_path, factory=FakeDebugger)
        return session.hub

    trace = TraceWriter(tmp_path / "trace.jsonl")
    summary = execute_run(sim, {"kind": "run", "live": True, "virtual_ms": 10_000,
                                "slice_ms": 50}, trace, session=session, debug_hub=hub)
    trace.close()
    return session, summary, read_trace(tmp_path / "trace.jsonl")


def dbg(**op):
    return {"kind": "debug", "board": "ecu", **op}


def test_a_breakpoint_holds_the_session_until_continue(tmp_path):
    session, summary, trace = run_debug(tmp_path, [
        (at(2_000_000), dbg(cmd="attach")),
        (at(2_000_000), dbg(cmd="break", location={"function": "step"})),
        (held, dbg(cmd="locals")),
        (held, {"kind": "can_send", "bus": "can_acu", "id": 0x100, "data": "00"}),
        (held, dbg(cmd="next")),
        (held, dbg(cmd="continue")),
        (at(2_300_000), {"kind": "stop"}),
    ])
    hub = session.hub
    assert summary["stopped"] == "op" and not hub.active(), "every board let go at the end"
    by_id = {s[0]: s for s in session.settled}
    # The break came while the ECU ran: deferred, the board interrupted, and
    # applied (with its result) when it stopped for that, at the next slice.
    assert by_id[2][1] == "applied" and by_id[2][4]["number"] == 1
    assert by_id[2][2] > 2_000_000
    assert by_id[3][4] == [{"name": "now_ms", "value": "2150"}]
    # A stimulus waits while the system is held: it goes at the next boundary.
    assert by_id[4][1] == "applied" and by_id[4][2] > by_id[6][2]
    stops = [r for r in trace if r["kind"] == "debug" and r["event"] == "stopped"]
    assert [s["reason"] for s in stops] == ["breakpoint-hit", "end-stepping-range"]
    assert stops[0]["at_us"] == 2_150_000 and stops[0]["locals"][0]["name"] == "now_ms"
    paused = [r for r in trace if r["kind"] == "clock" and r.get("debug")]
    assert paused[0]["paused"] and paused[0]["debug"] == {
        "board": "ecu", "reason": "breakpoint-hit", "func": "step",
        "file": "/fw/ecu/control.cpp", "line": 35, "addr": "0x0802310a"}
    # No SIGINT stop for the deferred break is shown.
    assert all(s.get("signal") != "SIGINT" for s in stops)
    assert [r["t_us"] for r in trace] == sorted(r["t_us"] for r in trace)
    assert [r["op"]["cmd"] for r in trace if r["kind"] == "op"
            and r["op"]["kind"] == "debug"] == ["attach", "break", "next", "continue"]


def test_queries_on_a_running_board_are_refused_and_say_why(tmp_path):
    session, _, trace = run_debug(tmp_path, [
        (at(2_000_000), dbg(cmd="attach")),
        (at(2_000_000), dbg(cmd="locals")),
        (at(2_000_000), dbg(cmd="clear", number=4)),
        (at(2_000_000), {"kind": "debug", "board": "ams", "cmd": "continue"}),
        (at(2_100_000), {"kind": "stop"}),
    ])
    by_id = {s[0]: s for s in session.settled}
    assert by_id[1][1] == "applied" and by_id[1][4]["state"] == "running"
    assert by_id[2][1] == "refused" and "running" in by_id[2][3]
    assert by_id[3][1] == "refused" and "no breakpoint" in by_id[3][3]
    assert by_id[4][1] == "refused" and "attach first" in by_id[4][3]
    assert [r["event"] for r in trace if r["kind"] == "debug"][0] == "attached"


def test_a_stop_while_held_lets_every_board_go(tmp_path):
    session, summary, trace = run_debug(tmp_path, [
        (at(2_000_000), dbg(cmd="break", location={"function": "step"})),
        (held, {"kind": "stop"}),
    ])
    assert summary["stopped"] == "op"
    assert not session.hub.active()
    assert summary["virtual_ms"] <= 2_300


def test_the_session_channel_carries_debug_ops(tmp_path):
    from fastapi.testclient import TestClient
    from tests.unit.test_session import hello
    from vhil.server import create_app
    from vhil.server.config import Settings
    settings = Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs",
                        auth="dev")
    client = TestClient(create_app(settings))
    r = client.post("/api/runs", json={"system": "ecu-ams", "scenario": {"kind": "run",
                                                                         "live": True}})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    with client.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        hello(ws)
        ws.send_json({"kind": "op", "cid": "a", "op": {"kind": "debug", "board": "ecu",
                                                       "cmd": "break",
                                                       "location": {"function": "main"}}})
        assert ws.receive_json()["kind"] == "queued"
        ws.send_json({"kind": "op", "cid": "b", "op": {"kind": "debug", "board": "ecu",
                                                       "cmd": "eval", "expr": "f()"}})
        got = ws.receive_json()
        assert got["kind"] == "refused" and "variable path" in got["detail"]
    ops = SessionStore(settings.db).ops(run_id)
    assert [o["op"] for o in ops] == [
        {"kind": "debug", "board": "ecu", "cmd": "break", "location": {"function": "main"}}]


def test_a_break_on_a_board_not_debugged_attaches_and_goes_in_at_once(tmp_path):
    session, _, trace = run_debug(tmp_path, [
        (at(2_000_000), dbg(cmd="break", location={"function": "step"})),
        (held, dbg(cmd="continue")),
        (at(2_300_000), {"kind": "stop"}),
    ])
    by_id = {s[0]: s for s in session.settled}
    # Attaching stopped the board: the breakpoint went in there, at once.
    assert by_id[1][1] == "applied" and by_id[1][2] == 2_000_000
    stops = [r for r in trace if r["kind"] == "debug" and r["event"] == "stopped"]
    assert [s["reason"] for s in stops] == ["breakpoint-hit"], "attaching's stop is not shown"


def test_an_interrupt_on_a_board_not_debugged_is_its_attach_stop(tmp_path):
    session, _, trace = run_debug(tmp_path, [
        (at(2_000_000), dbg(cmd="interrupt")),
        (held, dbg(cmd="continue")),
        (at(2_200_000), {"kind": "stop"}),
    ])
    stops = [r for r in trace if r["kind"] == "debug" and r["event"] == "stopped"]
    assert [s["reason"] for s in stops] == ["attached"]
    assert [s[1] for s in session.settled] == ["applied"] * 3
