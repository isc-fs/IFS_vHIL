"""The debugger on the real ECU and AMS (step 16 of
docs/architecture/editor-workspace.md; docs/debugger.md): Renode's GDB stub
per board (models/renode/VhilGdb.cs) and GDB in MI mode (vhil/gdb.py).

ECU facts (IFS08-CE-ECU): ecu::Controller::step(const CtrlInputs& in,
uint32_t now_ms) is the control task's step (Core/Src/app/control.cpp),
called every control tick from ecu_control_task_run (control_task.cpp); its
first line reads the APPS calibration (apps_pct).

Lockstep: a halted CPU holds Renode's time source, so while the ECU sits at
a breakpoint the AMS does not run either: the two boards' HAL ticks (uwTick,
ms since each one's HAL_Init) keep the same difference however long the
breakpoint holds in wall time, and no timeout of either firmware fires.
"""
import json
import threading
import time

import pytest

from tests.unit.test_debugger import Driver as UnitDriver
from vhil.gdb import Debugger, DebugError, DebugHub
from vhil.system import REPO
from vhil.worker import TraceWriter, execute_run

pytest.importorskip("fastapi")

ECU_AMS = REPO / "systems" / "ecu-ams.yaml"
STEP = {"function": "ecu::Controller::step"}


def gdb_or_skip():
    from vhil.gdb import find_gdb
    try:
        return find_gdb()
    except RuntimeError as e:
        pytest.skip(str(e))


def ticks(sim):
    return sim.read_symbol("ecu", "uwTick", 4), sim.read_symbol("ams", "uwTick", 4)


def test_a_breakpoint_holds_every_board_and_reads_the_firmware(make_sim, tmp_path):
    gdb_or_skip()
    sim = make_sim("ecu-ams")
    sim.run_for(ms=100)
    ecu0, ams0 = ticks(sim)
    dbg = Debugger(sim, "ecu", tmp_path)
    # Attaching stops the board before its next instruction: the
    # breakpoint goes in there.
    dbg.start(resume=False)
    try:
        assert dbg.state == "stopped" and dbg.stop["reason"] == "attached"
        bp = dbg.break_insert(STEP)
        assert bp["file"].endswith("control.cpp") and bp["line"] > 0, bp
        dbg.resume("continue")
        sim.run_for(ms=1)
        box = {}
        runner = threading.Thread(target=lambda: box.update(t=sim.run_for(ms=200)))
        runner.start()
        stop = dbg.wait_stop(timeout_s=120)
        assert stop["reason"] == "breakpoint-hit" and stop["frame"]["func"] == "ecu::Controller::step"
        # Held: the RunFor waits on the halted ECU, whatever the wall time.
        time.sleep(3)
        assert runner.is_alive(), "virtual time went on past a breakpoint"
        names = {v["name"]: v for v in dbg.locals()}
        assert "now_ms" in names and int(names["now_ms"]["value"]) > 0, names
        assert int(dbg.evaluate("now_ms")) == int(names["now_ms"]["value"])
        frames = dbg.frames()
        assert frames[0]["func"] == "ecu::Controller::step"
        assert any(f["func"] == "ecu_control_task_run" for f in frames), frames
        regs = {r["name"]: r["value"] for r in dbg.registers()}
        assert int(regs["pc"], 16) == int(stop["frame"]["addr"], 16)
        line = stop["frame"]["line"]
        dbg.resume("next")
        nxt = dbg.wait_stop(timeout_s=60)
        assert nxt["reason"] == "end-stepping-range" and nxt["frame"]["line"] != line, nxt
        dbg.resume("step")
        assert dbg.wait_stop(timeout_s=60)["reason"] == "end-stepping-range"
        dbg.resume("finish")
        assert dbg.wait_stop(timeout_s=60)["reason"] == "function-finished"
        dbg.break_delete(bp["number"])
        dbg.resume("continue")
        runner.join(120)
        assert not runner.is_alive()
        # Break, once it ran on: it stops where it is, before its next
        # instruction, and the system is held there as at a breakpoint.
        dbg.interrupt()
        runner = threading.Thread(target=lambda: sim.run_for(ms=10))
        runner.start()
        assert dbg.wait_stop(timeout_s=60)["signal"] == "SIGINT"
        time.sleep(1)
        assert runner.is_alive(), "virtual time went on past an interrupt"
        dbg.resume("continue")
        runner.join(60)
    finally:
        dbg.close()
    ecu1, ams1 = ticks(sim)
    # 211 ms of virtual time ran since the first read (1 + 200 + 10), and in it
    # the two boards' ticks moved together: the AMS did not run on while the
    # ECU was held, for the 3+ s of wall time it was.
    assert 205 <= ecu1 - ecu0 <= 225, (ecu0, ecu1)
    assert abs((ams1 - ecu1) - (ams0 - ecu0)) <= 1, (ecu0, ams0, ecu1, ams1)


def test_the_stub_answers_no_monitor_command_and_no_write(make_sim, tmp_path):
    gdb_or_skip()
    sim = make_sim("ecu-ams")
    dbg = Debugger(sim, "ecu", tmp_path)
    dbg.start(resume=False)
    try:
        runner = threading.Thread(target=lambda: sim.run_for(ms=10))
        runner.start()
        # Past vhil.gdb's own checks, straight to GDB: the stub refuses a
        # monitor command, and a write never reaches the target's memory
        # (the stub has no M/X packets), whatever GDB makes of it.
        with pytest.raises(DebugError, match="not support"):
            dbg._mi('-interpreter-exec console "monitor help"')
        try:
            dbg._mi('-data-evaluate-expression "uwTick=123456"')
        except DebugError:
            pass
        dbg.resume("continue")
        runner.join(60)
    finally:
        dbg.close()
    assert sim.read_symbol("ecu", "uwTick", 4) < 100_000


class Driver(UnitDriver):
    """A live session's channel scripted as a user drives the Debug tab
    (tests/unit/test_debugger.py), waiting in wall time while held."""

    def wait(self):
        time.sleep(0.02)


def result(session, op_id):
    return next(s for s in session.settled if s[0] == op_id)[4]


def held(d, _now):
    return d.hub is not None and d.hub.stopped() == ["ecu"]


def at(us):
    return lambda d, now: now >= us


def test_a_live_session_breaks_steps_and_goes_on(images, tmp_path):
    gdb_or_skip()
    from vhil.sim import Sim
    dbg = lambda op: {"kind": "debug", "board": "ecu", **op}  # noqa: E731
    session = Driver([
        (at(2_300_000), dbg({"cmd": "break", "location": STEP})),
        (held, dbg({"cmd": "locals"})),
        (held, dbg({"cmd": "eval", "expr": "now_ms"})),
        (held, dbg({"cmd": "next"})),
        (held, dbg({"cmd": "clear", "number": 1})),
        (held, dbg({"cmd": "continue"})),
        (at(2_600_000), {"kind": "stop"}),
    ])

    def hub(sim):
        session.hub = DebugHub(sim, tmp_path / "gdb")
        return session.hub

    path = tmp_path / "live.jsonl"
    trace = TraceWriter(path)
    with Sim(ECU_AMS, images("ecu-ams"), log_path=tmp_path / "renode.log") as sim:
        summary = execute_run(sim, {"kind": "run", "live": True, "virtual_ms": 60_000,
                                    "slice_ms": 50}, trace, session=session, debug_hub=hub,
                              state_view=True)
    trace.close()
    assert summary["stopped"] == "op"
    assert [s[1] for s in session.settled] == ["applied"] * 7, session.settled
    bp = result(session, 1)
    assert bp["number"] == 1 and bp["file"].endswith("control.cpp")
    local_names = [v["name"] for v in result(session, 2)]
    assert "now_ms" in local_names and "in" in local_names
    assert int(result(session, 3)["value"]) > 0
    recs = [json.loads(line) for line in path.read_text().splitlines()]
    debug = [r for r in recs if r["kind"] == "debug"]
    stops = [r for r in debug if r["event"] == "stopped"]
    assert [s["reason"] for s in stops] == ["breakpoint-hit", "end-stepping-range"], stops
    first = stops[0]
    assert first["frame"]["func"] == "ecu::Controller::step" and first["frames"]
    assert any(v["name"] == "now_ms" for v in first["locals"])
    assert {"pc", "sp"} <= {r["name"] for r in first["registers"]}
    assert first["t_us"] <= first["at_us"] <= first["t_us"] + 50_000
    paused = [r for r in recs if r["kind"] == "clock" and r["paused"]]
    assert paused and paused[0]["debug"]["board"] == "ecu"
    assert paused[0]["debug"]["func"] == "ecu::Controller::step"
    assert any(r["event"] == "running" for r in debug)
    # The trace stays in virtual-time order, the held stretch included.
    times = [r["t_us"] for r in recs if "t_us" in r]
    assert times == sorted(times)
    # Queries leave no op record; the rest do.
    ops = [r["op"]["cmd"] for r in recs if r["kind"] == "op" and r["op"]["kind"] == "debug"]
    assert ops == ["break", "next", "clear", "continue"], ops
