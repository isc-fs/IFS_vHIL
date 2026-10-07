"""Live sessions (step 14 of docs/architecture/editor-workspace.md;
docs/live-session.md): the session channel's ops, the worker applying them
at slice boundaries in virtual time, ownership, the idle timeout, and the
determinism of a recorded session replayed as a scenario. Against the fake
Sim of test_runs.py; the real Sim is tests/sim/test_live_session.py."""
import json
import threading
import time
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from tests.unit.test_runs import FakeSim, as_user, github_app  # noqa: E402
from vhil.server import create_app  # noqa: E402
from vhil.server import scenarios as vscen  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.server.runs import Limits, read_trace  # noqa: E402
from vhil.server.session import (OpError, RateLimit, SessionStore, as_scenario,  # noqa: E402
                                 parse_op)
from vhil.system import REPO, System  # noqa: E402
from vhil.worker import LiveSession, TraceWriter, Worker, execute_run  # noqa: E402

AMS = REPO / "systems" / "ams.yaml"
LIVE = {"kind": "run", "live": True}


@pytest.fixture
def settings(tmp_path):
    return Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")


@pytest.fixture
def store(settings):
    return SessionStore(settings.db)


# -- ops ---------------------------------------------------------------------------

def test_an_op_is_a_scenario_stimulus_without_its_time():
    system = System(AMS)
    assert parse_op({"kind": "gpio", "at_ms": 12, "board": "ams", "pin": "PF9", "level": True},
                    system) == {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True}
    assert parse_op({"kind": "can_periodic", "name": "vcu", "bus": "can_acu", "id": 0x100,
                     "data": "000002", "period_ms": 10}, system) == {
        "kind": "can_periodic", "name": "vcu", "bus": "can_acu", "id": 0x100,
        "data": "000002", "period_ms": 10.0}
    assert parse_op({"kind": "pause"}, system) == {"kind": "pause"}


@pytest.mark.parametrize("op, needle", [
    ({"kind": "reboot"}, "unknown op kind"),
    ({"kind": "pause", "now": 1}, "takes nothing"),
    ({"kind": "gpio", "board": "ams", "pin": "PF9"}, "level"),
    ({"kind": "gpio", "board": "ams", "pin": "PZ99", "level": True}, "PZ99"),
    ({"kind": "gpio", "board": "nope", "pin": "PF9", "level": True}, "no board"),
    ({"kind": "can_send", "bus": "can_x", "id": 1}, "no bus"),
    ({"kind": "can_send", "bus": "can_acu", "id": 1, "data": "zz"}, "hex"),
    ({"kind": "can_periodic", "bus": "can_acu", "id": 1, "period_ms": 10}, "needs a name"),
    ({"kind": "can_periodic", "name": "a", "bus": "can_acu", "id": 1, "period_ms": 10,
      "until_ms": 50}, "until"),
    ({"kind": "watch", "board": "ams", "symbol": "x; quit"}, "pattern"),
    ("gpio", "an object"),
])
def test_bad_ops_are_refused_and_say_why(op, needle):
    with pytest.raises(OpError, match=needle):
        parse_op(op, System(AMS))


def test_the_rate_limit_is_a_token_bucket():
    t = [0.0]
    rl = RateLimit(2, clock=lambda: t[0])
    assert [rl.take() for _ in range(3)] == [True, True, False]
    t[0] = 0.5
    assert rl.take() and not rl.take()


# -- the executor with a session ----------------------------------------------------

class Scripted:
    """A session whose ops arrive when virtual time reaches theirs: [(t_us,
    op)], t_us None for one that comes while paused; ids count from 1. No
    pacing, never idle (unless `idle_after`)."""
    max_periodic = 16
    idle_s = 60.0

    def __init__(self, script, idle_after=None):
        self.script = sorted(script, key=self.key)
        self.n, self.taken, self.settled, self.clocks = 0, [], [], 0
        self.idle_after, self.now = idle_after, 0
        self.waits = 0

    @staticmethod
    def key(item):
        return float("inf") if item[0] is None else item[0]

    def take(self, now_us=0):
        self.now = now_us
        out = []
        while self.n < len(self.script) and self.script[self.n][0] is not None \
                and self.script[self.n][0] <= now_us:
            self.n += 1
            out.append({"id": self.n, "op": self.script[self.n - 1][1], "login": "dev"})
        self.taken += out
        return out

    def settle(self, settled):
        self.settled += settled

    def idle(self):
        return self.idle_after is not None and self.now >= self.idle_after

    def wait(self):
        # Paused: wall time passes, virtual time doesn't; the script's ops
        # with no time come now.
        self.waits += 1
        self.script = self.script[:self.n] + sorted(
            [(self.now if t is None else t, op) for t, op in self.script[self.n:]], key=self.key)

    def rebase(self, now_us):
        pass

    def pace(self, now_us):
        pass

    def clock(self, now_us, paused=False):
        self.clocks += 1
        return {"kind": "clock", "t_us": now_us, "rtf": 1.0, "paused": paused}


def run_live(tmp_path, script, system=AMS, virtual_ms=1000, name="live.jsonl", **kw):
    sim = FakeSim(system)
    session = Scripted(script, **kw)
    path = tmp_path / name
    trace = TraceWriter(path)
    summary = execute_run(sim, {**LIVE, "virtual_ms": virtual_ms, "slice_ms": 50}, trace,
                          session=session)
    trace.close()
    return sim, session, summary, read_trace(path)


VCU = {"kind": "can_periodic", "name": "vcu", "bus": "can_acu", "id": 0x100, "data": "000002",
       "period_ms": 10}


def test_an_op_applies_at_the_end_of_the_slice_after_it_came(tmp_path):
    sim, session, summary, trace = run_live(tmp_path, [
        (120_000, VCU),
        (230_000, {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True}),
        (330_000, {"kind": "stop_periodic", "periodic": "vcu"}),
        (400_000, {"kind": "stop"})])
    # Taken at the boundary at 150 ms, applied at the next one, 200 ms.
    assert [s[:3] for s in session.settled] == [(1, "applied", 200_000), (2, "applied", 300_000),
                                                (3, "applied", 400_000), (4, "applied", 450_000)]
    ops = [r for r in trace if r["kind"] == "op"]
    assert [(r["t_us"], r["op_id"], r["status"]) for r in ops] == [
        (200_000, 1, "applied"), (300_000, 2, "applied"), (400_000, 3, "applied"),
        (450_000, 4, "applied")]
    assert ("set_input", 300_000, "sysbus.gpioPortF", 9, True) in sim.calls
    sent = [r["t_us"] for r in trace if r["kind"] == "frame" and r.get("src") == "stimulus"]
    assert sent == list(range(200_000, 400_000, 10_000))
    # Stop ends the run at the end of the slice its ops were scheduled for.
    assert summary["stopped"] == "op" and summary["virtual_ms"] == 450
    assert trace[-1]["t_us"] <= 450_000
    assert [r["t_us"] for r in trace].count(450_000) >= 1
    # A clock record a slice.
    clocks = [r for r in trace if r["kind"] == "clock"]
    assert [c["t_us"] for c in clocks][:3] == [50_000, 100_000, 150_000]


def test_a_refused_op_says_why_at_once_and_changes_nothing(tmp_path):
    sim, session, summary, trace = run_live(tmp_path, [
        (0, {"kind": "stop_periodic", "periodic": "nope"}),
        (0, VCU),
        (100_000, VCU),                                      # its name again
        (100_000, {"kind": "stop_periodic", "periodic": "vcu"}),
        (200_000, {**VCU, "name": "vcu2"}),
        (200_000, {"kind": "can_send", "bus": "can_x", "id": 1}),
        (300_000, {"kind": "stop"})])
    st = {s[0]: (s[1], s[3]) for s in session.settled}
    assert st[1][0] == "refused" and "no periodic named 'nope'" in st[1][1]
    assert st[2] == ("applied", "") and st[4] == ("applied", "")
    assert st[3][0] == "refused" and "ran in this session already" in st[3][1]
    assert st[5] == ("applied", "")
    assert st[6][0] == "refused" and "can_x" in st[6][1]
    refused = [r for r in trace if r["kind"] == "op" and r["status"] == "refused"]
    assert [(r["op_id"], r["t_us"]) for r in refused] == [(1, 0), (3, 100_000), (6, 200_000)]
    assert summary["ops_applied"] == 4 and summary["ops_refused"] == 3


def test_periodics_are_capped(tmp_path):
    script = [(0, {**VCU, "name": f"p{i}"}) for i in range(Scripted.max_periodic + 1)]
    _, session, _, _ = run_live(tmp_path, script + [(100_000, {"kind": "stop"})])
    assert [s[1] for s in session.settled].count("refused") == 1
    assert "VHIL_LIVE_MAX_PERIODIC" in session.settled[Scripted.max_periodic][3]


def test_pause_holds_virtual_time_until_resume(tmp_path):
    sim, session, summary, trace = run_live(tmp_path, [
        (100_000, {"kind": "pause"}),
        (None, {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True}),
        (None, {"kind": "resume"}),
        (300_000, {"kind": "stop"})])
    assert session.waits >= 1
    st = [(s[0], s[1], s[2]) for s in session.settled]
    # Paused at 100 ms; the gpio that came while paused applies at the end of
    # the next slice (150 ms) once resumed.
    assert st[:3] == [(1, "applied", 100_000), (2, "applied", 150_000), (3, "applied", 100_000)]
    clocks = [(c["t_us"], c["paused"]) for c in trace if c["kind"] == "clock"]
    assert (100_000, True) in clocks and (100_000, False) in clocks
    assert ("set_input", 150_000, "sysbus.gpioPortF", 9, True) in sim.calls


def test_an_idle_session_stops(tmp_path):
    _, _, summary, trace = run_live(tmp_path, [], idle_after=200_000)
    assert summary["stopped"] == "idle" and summary["virtual_ms"] == 250
    assert any("idle" in r.get("text", "") for r in trace if r["kind"] == "log")


def test_a_live_run_runs_to_its_cap_when_never_stopped(tmp_path):
    _, _, summary, _ = run_live(tmp_path, [], virtual_ms=300)
    assert summary["stopped"] == "end" and summary["virtual_ms"] == 300


# -- determinism: a recorded session replays as a scenario -------------------------------

def comparable(trace):
    """What the boards did and the scenario sent: frames, edges, samples."""
    return sorted(json.dumps(r, sort_keys=True) for r in trace
                  if r["kind"] in ("frame", "edge", "sample"))


def test_a_recorded_session_replayed_as_a_scenario_gives_the_same_trace(tmp_path):
    script = [
        (60_000, VCU),
        (160_000, {"kind": "watch", "board": "ams", "pin": "PB5"}),
        (160_000, {"kind": "watch", "board": "ams", "symbol": "g_state_telemetry",
                   "period_ms": 20}),
        (230_000, {"kind": "gpio", "board": "ams", "pin": "PB5", "level": True}),
        (240_000, {"kind": "analog", "board": "ams", "pin": "PF7", "volts": 1.65}),
        (300_000, {"kind": "can_send", "bus": "can_acu", "id": 0x20, "data": "01"}),
        (410_000, {"kind": "stop_periodic", "periodic": "vcu"}),
        (500_000, {"kind": "stop"})]
    live_sim, _, summary, trace = run_live(tmp_path, script)
    run = {"id": 7, "system": "ams", "owner": "dev", "virtual_us": summary["virtual_ms"] * 1000,
           "scenario": {**LIVE, "slice_ms": 50, "virtual_ms": 3_600_000}}
    doc = as_scenario(run, trace)
    assert doc["virtual_ms"] == 550 and doc["slice_ms"] == 50
    assert [s["at_ms"] for s in doc["stimuli"]] == [150, 250, 250, 300, 300, 350, 500]
    # It is a valid scenario file, and its canonical text round-trips.
    sc, _, errors = vscen.parse(doc, "ams", "recorded")
    assert not errors, errors
    assert vscen.load_text(vscen.dump_scenario(sc, "ams", doc["description"]), "ams",
                           "recorded")[0] == sc

    sim = FakeSim(AMS)
    path = tmp_path / "replay.jsonl"
    tw = TraceWriter(path)
    execute_run(sim, {"kind": "run", **sc.model_dump(exclude={"kind", "name", "live"})}, tw)
    tw.close()
    replayed = read_trace(path)
    assert comparable(replayed) == comparable(trace)
    assert len(comparable(trace)) > 40
    # The same inputs and stops at the same virtual times (the senders' keys aside).
    applied = lambda calls: [c[:3] if c[0] == "stop_periodic" else c for c in calls  # noqa: E731
                             if c[0] in ("set_input", "set_voltage", "stop_periodic")]
    assert applied(sim.calls) == applied(live_sim.calls) and len(applied(sim.calls)) == 3


# -- the worker --------------------------------------------------------------------------

class FixedResolver:
    def resolve(self, system, refs):
        return {}


def live_worker(settings, **limits):
    return Worker(settings, sim_factory=lambda *a: FakeSim(AMS), resolver=FixedResolver(),
                  limits=replace(Limits(), **limits))


def test_the_worker_applies_queued_ops_and_ends_a_stopped_session_passed(settings, store):
    run_id = store.create("ams", "", {}, {**LIVE, "virtual_ms": 3_600_000, "slice_ms": 50,
                                          "stimuli": [], "watch": [], "expect": []}, owner="dev")
    ids = [store.add_op(run_id, op, "dev") for op in (
        {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True}, VCU, {"kind": "stop"})]
    live_worker(settings).run_once()
    run = store.get(run_id)
    assert run["state"] == "passed", run["summary"]
    assert run["summary"]["stopped"] == "op" and run["summary"]["ops_applied"] == 3
    assert [(o["id"], o["state"], o["at_us"]) for o in store.ops(run_id)] == [
        (ids[0], "applied", 50_000), (ids[1], "applied", 50_000), (ids[2], "applied", 50_000)]
    trace = read_trace(settings.results / str(run_id) / "trace.jsonl")
    assert [r["op_id"] for r in trace if r["kind"] == "op"] == ids
    assert any(r["kind"] == "clock" for r in trace)


def test_the_worker_stops_an_idle_session_and_refuses_ops_that_came_late(settings, store):
    run_id = store.create("ams", "", {}, {**LIVE, "virtual_ms": 3_600_000, "slice_ms": 50,
                                          "stimuli": [], "watch": [], "expect": []}, owner="dev")
    # No op for a second of wall time (paced: about a second of virtual time).
    t0 = time.monotonic()
    live_worker(settings, live_idle_s=1).run_once()
    run = store.get(run_id)
    assert run["state"] == "passed" and run["summary"]["stopped"] == "idle"
    assert 900_000 <= run["virtual_us"] <= 2_000_000 and time.monotonic() - t0 < 10


def test_pacing_holds_virtual_time_to_wall_time():
    t = [0.0]
    slept = []
    s = LiveSession.__new__(LiveSession)
    LiveSession.__init__(s, store=None, run_id=1, limits=Limits(), clock=lambda: t[0],
                         wall=lambda: t[0], sleep=lambda d: slept.append(d))
    s.rebase(0)
    s.pace(50_000)                  # 50 ms of virtual time in no wall time: wait 50 ms
    assert slept == [pytest.approx(0.05)]
    t[0] = 1.0
    s.pace(100_000)                 # 0.9 s behind: forgiven, not repaid
    assert len(slept) == 1 and s._base == (100_000, 1.0)


# -- the channel ----------------------------------------------------------------------------

def live_run(client, **kw):
    r = client.post("/api/runs", json={"system": "ams", "scenario": {**LIVE, **kw}})
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def hello(ws, **kw):
    ws.send_json({"kind": "hello", **kw})
    return ws.receive_json()


def test_a_live_run_is_open_ended_with_a_live_slice(settings):
    client = TestClient(create_app(settings))
    run = client.get(f"/api/runs/{live_run(client)}").json()
    assert run["scenario"]["virtual_ms"] == Limits().max_live_ms
    assert run["scenario"]["slice_ms"] == 50 and run["scenario"]["live"] is True
    r = client.post("/api/runs", json={"system": "ams", "scenario": {**LIVE,
                                                                    "virtual_ms": 7_200_000}})
    assert r.status_code == 422 and "live session" in r.text
    r = client.post("/api/runs", json={"system": "ams", "scenario": {"kind": "run",
                                                                    "virtual_ms": 700_000}})
    assert r.status_code == 422


def test_ops_are_queued_then_acked_as_the_worker_settles_them(settings, store):
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    with client.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        h = hello(ws)
        assert h["kind"] == "hello" and h["role"] == "control" and h["live"] is True
        assert h["limits"]["ops_per_s"] == Limits().live_ops_per_s
        ws.send_json({"kind": "op", "cid": "a", "op": {"kind": "gpio", "board": "ams",
                                                         "pin": "PF9", "level": True}})
        q = ws.receive_json()
        assert q["kind"] == "queued" and q["cid"] == "a"
        ws.send_json({"kind": "op", "cid": "b", "op": {"kind": "gpio", "board": "ams",
                                                         "pin": "PB99", "level": True}})
        r = ws.receive_json()
        assert r["kind"] == "refused" and r["cid"] == "b" and "PB99" in r["detail"]
        assert [o["op"] for o in store.pending(run_id)] == [
            {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True}]
        store.settle(run_id, [(q["op_id"], "applied", 2_550_000, "")])
        ack = ws.receive_json()
        assert ack == {"kind": "ack", "op_id": q["op_id"], "status": "applied",
                       "at_us": 2_550_000, "detail": "", "login": "dev",
                       "op": {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True}}
        store.cancel(run_id)
        assert ws.receive_json() == {"kind": "end", "state": "cancelled"}


def test_one_connection_controls_and_a_second_tab_watches(settings, store):
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    with client.websocket_connect(f"/api/runs/{run_id}/session") as a:
        assert hello(a)["role"] == "control"
        with client.websocket_connect(f"/api/runs/{run_id}/session") as b:
            h = hello(b)
            assert h["role"] == "view" and h["holder"] == "dev" and h["may_control"] is True
            b.send_json({"kind": "op", "op": {"kind": "pause"}})
            r = b.receive_json()
            assert r["kind"] == "refused" and "watches" in r["detail"]
            b.send_json({"kind": "take"})        # held: nothing changes
            a.send_json({"kind": "op", "op": {"kind": "pause"}})
            assert a.receive_json()["kind"] == "queued"
    # The controller leaves: its lease goes as its connection ends, and
    # control is free for the next to take.
    deadline = time.monotonic() + 5
    while store.holder(run_id) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.holder(run_id) is None
    with client.websocket_connect(f"/api/runs/{run_id}/session") as c:
        assert hello(c)["role"] == "control"


def test_only_the_owner_or_an_admin_controls_and_only_with_the_csrf_token(settings, monkeypatch):
    app = github_app(settings, monkeypatch, admins={"carol"})
    alice, bob, carol = (as_user(app, n) for n in ("alice", "bob", "carol"))
    run_id = live_run(alice)
    token = alice.headers["X-CSRF-Token"]
    with bob.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        h = hello(ws, csrf=bob.headers["X-CSRF-Token"])
        assert h["role"] == "view" and h["may_control"] is False
        ws.send_json({"kind": "op", "op": {"kind": "stop"}})
        assert "owner or an admin" in ws.receive_json()["detail"]
    with alice.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        assert hello(ws)["role"] == "view"                 # no token
    with alice.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        assert hello(ws, csrf="forged")["role"] == "view"
    with alice.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        assert hello(ws, csrf=token)["role"] == "control"
    store = SessionStore(settings.db)
    deadline = time.monotonic() + 5
    while store.holder(run_id) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    with carol.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        assert hello(ws, csrf=carol.headers["X-CSRF-Token"])["role"] == "control"


def test_a_cross_site_session_is_refused_at_the_handshake(settings):
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/runs/{run_id}/session",
                                      headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 4403


def test_a_session_starts_with_a_hello(settings):
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    with client.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        ws.send_json({"kind": "op", "op": {"kind": "pause"}})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 4400


def test_ops_are_rate_limited_and_capped(settings, store):
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    with client.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        hello(ws)
        kinds = []
        for _ in range(Limits().live_ops_per_s + 5):
            ws.send_json({"kind": "op", "op": {"kind": "keepalive"}})
            kinds.append(ws.receive_json()["kind"])
        assert kinds.count("refused") >= 4


def test_a_periodic_name_is_used_once_per_session(settings, store):
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    with client.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        hello(ws)
        ws.send_json({"kind": "op", "op": VCU})
        assert ws.receive_json()["kind"] == "queued"
        ws.send_json({"kind": "op", "op": VCU})
        r = ws.receive_json()
        assert r["kind"] == "refused" and "name it anew" in r["detail"]


def test_a_finished_run_has_no_control(settings, store):
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    store.cancel(run_id)
    with client.websocket_connect(f"/api/runs/{run_id}/session") as ws:
        h = hello(ws)
        assert h["role"] == "view" and h["may_control"] is False
        assert ws.receive_json() == {"kind": "end", "state": "cancelled"}


def test_a_sessions_recording_is_a_scenario(settings, store):
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    run = store.get(run_id)
    path = settings.results / str(run_id) / "trace.jsonl"
    path.parent.mkdir(parents=True)
    from vhil.server.runs import trace_header
    recs = [trace_header(run),
            {"kind": "op", "t_us": 0, "op_id": 1, "op": {"kind": "pause"}, "status": "applied"},
            {"kind": "op", "t_us": 2_550_000, "op_id": 2, "op": VCU, "status": "applied"},
            {"kind": "op", "t_us": 2_600_000, "op_id": 3, "status": "refused",
             "op": {"kind": "stop_periodic", "periodic": "x"}},
            {"kind": "op", "t_us": 3_000_500, "op_id": 4, "status": "applied",
             "op": {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True}},
            {"kind": "clock", "t_us": 3_050_000, "rtf": 1.0, "paused": False}]
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    out = client.get(f"/api/runs/{run_id}/session/scenario").json()
    doc = out["scenario"]
    assert out["ops"] == 2 and out["final"] is False
    assert doc["stimuli"] == [{**VCU, "at_ms": 2550.0},
                              {"kind": "gpio", "board": "ams", "pin": "PF9", "level": True,
                               "at_ms": 3000.5}]
    assert doc["virtual_ms"] == 3050 and doc["slice_ms"] == 50 and doc["system"] == "ams"
    r = client.post("/api/systems/ams/scenarios/recorded/preview", json={"scenario": doc})
    assert r.status_code == 200 and not r.json()["errors"], r.json()
    # A run that wasn't live has none.
    other = client.post("/api/runs", json={"system": "ams", "scenario": {"kind": "run"}})
    assert client.get(f"/api/runs/{other.json()['run_id']}/session/scenario").status_code == 422


def test_a_scenario_file_is_never_a_live_session():
    _, _, errors = vscen.parse({"kind": "scenario", "system": "ams", "live": True}, "ams", "x")
    assert any("live" in e for e in errors)


def test_the_worker_and_the_channel_together(settings, store):
    """A live run on the fake Sim, paced to wall time, driven over the
    channel from another thread while the worker runs it."""
    client = TestClient(create_app(settings))
    run_id = live_run(client)
    worker = live_worker(settings)
    t = threading.Thread(target=worker.run_once)
    t.start()
    try:
        with client.websocket_connect(f"/api/runs/{run_id}/session") as ws:
            hello(ws)
            ws.send_json({"kind": "op", "op": {"kind": "gpio", "board": "ams", "pin": "PF9",
                                               "level": True}})
            got = [ws.receive_json()]
            while got[-1]["kind"] != "ack":
                got.append(ws.receive_json())
            assert got[-1]["status"] == "applied" and got[-1]["at_us"] % 50_000 == 0
            ws.send_json({"kind": "op", "op": {"kind": "stop"}})
            while got[-1]["kind"] != "end":
                got.append(ws.receive_json())
            assert got[-1]["state"] == "passed"
    finally:
        t.join(timeout=30)
    trace = read_trace(settings.results / str(run_id) / "trace.jsonl")
    # Paced to wall time: the fake Sim runs far faster than real time, so
    # the real-time factor is about 1 (catching up after a slow slice may
    # take it over for a moment).
    rtfs = sorted(r["rtf"] for r in trace if r["kind"] == "clock")[2:]
    assert rtfs and 0.5 <= rtfs[len(rtfs) // 2] <= 1.5, rtfs
