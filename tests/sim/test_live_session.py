"""A live session on the real Sim (step 14 of
docs/architecture/editor-workspace.md; docs/live-session.md): the AMS driven
op by op as the editor's Send panel and pin switches drive it, then the
session's recording (vhil/server/session.py as_scenario) run as a scenario
on a fresh Sim. The two traces must be the same: frames, edges and samples,
record for record, at the same virtual times.

Firmware facts (IFS08-CE-AMS, as systems/ams.scenarios/tsms-precharge-run.yaml):
  0x100 from the ECU at 0 V, TSMS (PF9) and a DASH_CHG press (PF10) arm
  Precharge; the link following to the pack (0x100 "640102") gives Run.
"""
import json

import pytest

pytest.importorskip("fastapi")

from vhil.server.scenarios import parse  # noqa: E402
from vhil.server.session import as_scenario  # noqa: E402
from vhil.server.runs import read_trace  # noqa: E402
from vhil.system import REPO  # noqa: E402
from vhil.worker import TraceWriter, execute_run  # noqa: E402

AMS = REPO / "systems" / "ams.yaml"
STATE = "g_state_telemetry"     # ams::fsm::State: Start 0, Precharge 1, Transition 2, Run 3
VCU = {"kind": "can_periodic", "bus": "can_acu", "id": 0x100, "period_ms": 10}


class Scripted:
    """Ops as they would come over the channel, each once virtual time
    reaches its time (a slice boundary takes it): no pacing, never idle."""
    max_periodic = 16
    idle_s = 1e9

    def __init__(self, script):
        self.script, self.n, self.settled = script, 0, []

    def take(self, now_us=0):
        out = []
        while self.n < len(self.script) and self.script[self.n][0] <= now_us:
            self.n += 1
            out.append({"id": self.n, "op": self.script[self.n - 1][1], "login": "test"})
        return out

    def settle(self, settled):
        self.settled += settled

    def idle(self):
        return False

    def wait(self):
        pass

    def rebase(self, now_us):
        pass

    def pace(self, now_us):
        pass

    def clock(self, now_us, paused=False):
        return {"kind": "clock", "t_us": now_us, "rtf": 0.0, "paused": paused}


def comparable(trace):
    return sorted(json.dumps(r, sort_keys=True) for r in trace
                  if r["kind"] in ("frame", "edge", "sample"))


def run(images, tmp_path, name, scenario, session=None):
    from vhil.sim import Sim
    path = tmp_path / f"{name}.jsonl"
    trace = TraceWriter(path)
    with Sim(AMS, images("ams"), log_path=tmp_path / f"{name}.log") as sim:
        summary = execute_run(sim, scenario, trace, state_view=True, session=session)
    trace.close()
    return summary, read_trace(path)


def test_a_live_session_replays_exactly_as_its_recorded_scenario(images, tmp_path):
    session = Scripted([
        (2_420_000, {**VCU, "name": "vcu", "data": "000002"}),
        (5_420_000, {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True}),
        (5_510_000, {"kind": "gpio", "board": "ams", "pin": "PF10", "level": True}),
        (5_560_000, {"kind": "gpio", "board": "ams", "pin": "PF10", "level": False}),
        (6_420_000, {"kind": "stop_periodic", "periodic": "vcu"}),
        (6_420_000, {**VCU, "name": "vcu-link", "data": "640102"}),
        (6_430_000, {"kind": "watch", "board": "ams", "symbol": "g_fault_reason_telemetry",
                     "period_ms": 20}),
        (7_220_000, {"kind": "stop"}),
    ])
    summary, live = run(images, tmp_path, "live", {"kind": "run", "live": True,
                                                   "virtual_ms": 60_000, "slice_ms": 50},
                        session)
    assert [s[1] for s in session.settled] == ["applied"] * 8, session.settled
    # Each applied at the end of the slice after the one it came in: within
    # two slices of when it came.
    for (came, _), (_, _, at_us, _) in zip(session.script, session.settled):
        assert came < at_us <= came + 100_000, session.settled
    stop_us = session.settled[-1][2]
    assert summary["stopped"] == "op" and summary["virtual_ms"] == -(-stop_us // 1000)
    states = [r["value"] for r in live if r["kind"] == "sample" and r["name"] == STATE]
    assert {0, 1, 3} <= set(states), sorted(set(states))
    assert 5 not in states, "the AMS went to Error"

    doc = as_scenario({"id": 1, "system": "ams", "virtual_us": stop_us,
                       "scenario": {"kind": "run", "live": True, "slice_ms": 50}}, live)
    sc, _, errors = parse(doc, "ams", "recorded")
    assert not errors, errors
    _, replayed = run(images, tmp_path, "replay",
                      {"kind": "run", **sc.model_dump(exclude={"kind", "name", "live"})})
    a, b = comparable(live), comparable(replayed)
    assert len(a) > 2000
    if a != b:
        only_live, only_replay = sorted(set(a) - set(b)), sorted(set(b) - set(a))
        pytest.fail(f"{len(only_live)} records only live, {len(only_replay)} only replayed: "
                    f"{only_live[:3]} / {only_replay[:3]}")
