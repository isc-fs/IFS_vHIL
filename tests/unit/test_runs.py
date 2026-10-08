"""vhil.server.runs + vhil.worker (M5.2, #114): the queue, the API, the
scenario executor against a fake Sim. The real Sim end to end is
tests/sim/test_server_runs.py."""
import json
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil import canframe  # noqa: E402
from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.server.runs import RunStore, read_trace, trace_header  # noqa: E402
from vhil.sim import Edge, Frame  # noqa: E402
from vhil.system import REPO, System  # noqa: E402
from vhil.worker import (Cancelled, FirmwareResolver, TraceWriter, Worker,  # noqa: E402
                         execute_pytest, execute_run, image_env)

ECU = REPO / "systems" / "ecu.yaml"
RUN = {"kind": "run", "virtual_ms": 100}


@pytest.fixture
def settings(tmp_path):
    return Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")


@pytest.fixture
def store(settings):
    return RunStore(settings.db)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


# -- store -----------------------------------------------------------------------

def test_the_db_is_in_wal_mode(store):
    db = store._connect()
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_runs_are_claimed_oldest_first(store):
    a, b = store.create("ecu", "", {}, RUN), store.create("ecu", "", {}, RUN)
    first = store.claim("w1")
    assert first["id"] == a and first["state"] == "running" and first["worker"] == "w1"
    assert first["started"] and first["scenario"] == RUN
    assert store.claim("w2")["id"] == b
    assert store.claim("w3") is None


def test_two_connections_never_claim_the_same_run(settings):
    seed = RunStore(settings.db)
    ids = {seed.create("ecu", "", {}, RUN) for _ in range(40)}
    claimed: list[tuple[str, int]] = []
    start = threading.Barrier(4)

    def worker(name):
        own = RunStore(settings.db)   # its own connections, like another process
        start.wait()
        while (run := own.claim(name)) is not None:
            claimed.append((name, run["id"]))

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = [run_id for _, run_id in claimed]
    assert sorted(got) == sorted(ids), "a run claimed twice or not at all"
    assert {r["worker"] for r in seed.list(100)} == {name for name, _ in claimed}


def test_finish_sets_the_final_state_once(store):
    run_id = store.create("ecu", "abc", {"ecu": "dev"}, RUN)
    store.claim("w")
    store.progress(run_id, 50_000)
    assert store.get(run_id)["virtual_us"] == 50_000
    assert store.finish(run_id, "passed", 100_000, {"frames": {"can_acu": 10}}) == "passed"
    run = store.get(run_id)
    assert run["state"] == "passed" and run["finished"] and run["virtual_us"] == 100_000
    assert run["summary"] == {"frames": {"can_acu": 10}} and run["firmware"] == {"ecu": "dev"}
    # A second finish (or one for a run never claimed) changes nothing.
    assert store.finish(run_id, "error", 0, {}) == "passed"
    with pytest.raises(ValueError):
        store.finish(run_id, "running", 0, {})


def test_cancel_a_queued_run_takes_it_off_the_queue(store):
    run_id = store.create("ecu", "", {}, RUN)
    assert store.cancel(run_id)["state"] == "cancelled"
    assert store.claim("w") is None


def test_a_running_run_cancelled_stays_cancelled_with_its_results(store):
    run_id = store.create("ecu", "", {}, RUN)
    store.claim("w")
    store.cancel(run_id)
    assert store.finish(run_id, "passed", 30_000, {"x": 1}) == "cancelled"
    run = store.get(run_id)
    assert run["state"] == "cancelled" and run["virtual_us"] == 30_000 and run["summary"] == {"x": 1}


def test_cancel_leaves_a_finished_run_alone(store):
    run_id = store.create("ecu", "", {}, RUN)
    store.claim("w")
    store.finish(run_id, "failed", 1, {})
    assert store.cancel(run_id)["state"] == "failed"


# -- API -------------------------------------------------------------------------

def post(client, scenario=RUN, **kw):
    return client.post("/api/runs", json={"system": "ecu", "scenario": scenario, **kw})


def test_post_queues_a_run_and_it_shows_in_the_history(client):
    r = post(client, {"kind": "run", "virtual_ms": 500,
                      "stimuli": [{"kind": "can_send", "at_ms": 5, "bus": "can_acu", "id": 0x20,
                                   "data": "01FF"},
                                  {"kind": "can_periodic", "bus": "can_inv", "id": 0x461,
                                   "data": "00", "period_ms": 10, "until_ms": 200},
                                  {"kind": "gpio", "at_ms": 100, "board": "ecu", "pin": "PB5",
                                   "level": True},
                                  {"kind": "analog", "board": "ecu", "pin": "PF7", "volts": 1.2}],
                      "watch": [{"kind": "symbol", "board": "ecu", "name": "g_last_ctrl_state"},
                                {"kind": "pin", "board": "ecu", "pin": "PB4"}]},
             firmware={"ecu": "feat/x"})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["state"] == "queued" and run["system"] == "ecu" and run["firmware"] == {"ecu": "feat/x"}
    assert run["scenario"]["stimuli"][0]["data"] == "01ff"
    assert run["scenario"]["slice_ms"] == 100
    assert [r["id"] for r in client.get("/api/runs").json()] == [run_id]
    assert client.get("/api/runs?state=passed").json() == []


def test_a_pytest_scenario_is_accepted(client):
    r = post(client, {"kind": "pytest", "select": "tests/sim/test_ecu_boot.py::test_heartbeat_only_on_acu_bus"})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("body, needle", [
    ({"system": "nope", "scenario": RUN}, "no system"),
    ({"system": "ecu", "scenario": {"kind": "run", "virtual_ms": 0}}, "virtual_ms"),
    ({"system": "ecu", "scenario": {"kind": "bogus"}}, "kind"),
    ({"system": "ecu", "scenario": {**RUN, "extra": 1}}, "extra"),
    ({"system": "ecu", "scenario": {**RUN, "stimuli": [
        {"kind": "can_send", "bus": "can_nope", "id": 1}]}}, "no bus 'can_nope'"),
    ({"system": "ecu", "scenario": {**RUN, "stimuli": [
        {"kind": "can_send", "bus": "can_acu", "id": 0x800}]}}, "ext"),
    ({"system": "ecu", "scenario": {**RUN, "stimuli": [
        {"kind": "can_send", "bus": "can_acu", "id": 1, "data": "xyz"}]}}, "hex"),
    ({"system": "ecu", "scenario": {**RUN, "stimuli": [
        {"kind": "gpio", "board": "ecu", "pin": "PF7", "level": True}]}}, "not gpio"),
    ({"system": "ecu", "scenario": {**RUN, "stimuli": [
        {"kind": "analog", "board": "ams", "pin": "PF7", "volts": 1}]}}, "no board 'ams'"),
    ({"system": "ecu", "scenario": {**RUN, "watch": [
        {"kind": "pin", "board": "ecu", "pin": "PZ9"}]}}, "PZ9"),
    ({"system": "ecu", "scenario": {**RUN, "watch": [
        {"kind": "symbol", "board": "ecu", "name": "a; rm"}]}}, "name"),
    ({"system": "ecu", "firmware": {"ams": "dev"}, "scenario": RUN}, "not an image"),
    ({"system": "ecu", "firmware": {"ecu": "--upload-pack=x"}, "scenario": RUN}, "plain git ref"),
    ({"system": "ecu", "firmware": {"ecu": "a/../../b"}, "scenario": RUN}, "plain git ref"),
    ({"system": "ecu", "scenario": {"kind": "pytest", "select": "tests/../etc/x.py"}}, "select"),
    ({"system": "ecu", "scenario": {"kind": "pytest", "select": "tests/sim/test_nope.py"}}, "no file"),
    ({"system": "ecu", "ref": "0000000", "scenario": RUN}, "no ref"),
])
def test_bad_requests_are_422_and_say_why(client, body, needle):
    r = client.post("/api/runs", json=body)
    assert r.status_code == 422, r.text
    assert needle in r.text


def test_unknown_runs_are_404(client):
    for path in ("/api/runs/9", "/api/runs/9/trace", "/api/runs/9/artifacts/trace.jsonl"):
        assert client.get(path).status_code == 404
    assert client.post("/api/runs/9/cancel").status_code == 404


def test_cancel_endpoint(client):
    run_id = post(client).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/cancel").json()["state"] == "cancelled"


TRACE_RECORDS = [
    {"kind": "log", "t_us": 0, "text": "start"},
    {"kind": "frame", "t_us": 10_000, "bus": "can_acu", "id": 256, "ext": False, "data": "00"},
    {"kind": "sample", "t_us": 10_000, "board": "ecu", "name": "g_last_ctrl_state", "value": 3},
    {"kind": "edge", "t_us": 15_000, "board": "ecu", "pin": "PB4", "level": 1},
    {"kind": "frame", "t_us": 20_000, "bus": "can_inv", "id": 0x360, "ext": False, "data": ""},
]


def write_trace(settings, run_id, records=TRACE_RECORDS, header=True):
    """Append records to a run's trace as the worker does: a new file starts
    with the run's header (trace_header), unless header=False."""
    d = settings.results / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "trace.jsonl", "a") as f:
        if header and f.tell() == 0:
            run = RunStore(settings.db).get(run_id)
            if run is not None:
                f.write(json.dumps(trace_header(run)) + "\n")
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_trace_filters_by_time_and_kind(client, settings):
    run_id = post(client).json()["run_id"]
    write_trace(settings, run_id)
    assert client.get(f"/api/runs/{run_id}/trace").json() == TRACE_RECORDS
    assert [r["t_us"] for r in client.get(f"/api/runs/{run_id}/trace?since_us=10000").json()] \
        == [10_000, 10_000, 15_000, 20_000]
    got = client.get(f"/api/runs/{run_id}/trace?since_us=10001&kinds=frame,edge").json()
    assert got == TRACE_RECORDS[3:]
    assert client.get(f"/api/runs/{run_id}/trace?kinds=frame,nope").status_code == 422


def pages(client, run_id, limit, query=""):
    """Every page of a run's trace, following the cursor: [(records, cursor)]."""
    out, cursor = [], ""
    while True:
        r = client.get(f"/api/runs/{run_id}/trace?limit={limit}{query}"
                       + (f"&cursor={cursor}" if cursor else ""))
        assert r.status_code == 200, r.text
        out.append((r.json(), r.headers["x-trace-cursor"]))
        if len(out[-1][0]) < limit:
            return out
        cursor = out[-1][1]


def test_trace_pages_follow_the_cursor(client, settings):
    run_id = post(client).json()["run_id"]
    write_trace(settings, run_id)
    got = pages(client, run_id, 2)
    assert [recs for recs, _ in got] == [TRACE_RECORDS[0:2], TRACE_RECORDS[2:4], TRACE_RECORDS[4:]]
    size = (settings.results / str(run_id) / "trace.jsonl").stat().st_size
    assert got[-1][1] == str(size)
    # At the end: nothing more, and the same cursor back.
    r = client.get(f"/api/runs/{run_id}/trace?cursor={size}")
    assert r.json() == [] and r.headers["x-trace-cursor"] == str(size)
    # since_us and kinds still filter, with or without a cursor.
    got = pages(client, run_id, 1, "&since_us=10001&kinds=frame,edge")
    assert [r for recs, _ in got for r in recs] == TRACE_RECORDS[3:]


def test_trace_cursor_resumes_a_trace_still_being_written(client, settings):
    run_id = post(client).json()["run_id"]
    write_trace(settings, run_id, TRACE_RECORDS[:2])
    path = settings.results / str(run_id) / "trace.jsonl"
    half = json.dumps(TRACE_RECORDS[2])
    with open(path, "a") as f:
        f.write(half[:10])
    r = client.get(f"/api/runs/{run_id}/trace")
    assert r.json() == TRACE_RECORDS[:2]
    cursor = r.headers["x-trace-cursor"]
    with open(path, "a") as f:
        f.write(half[10:] + "\n")
    write_trace(settings, run_id, TRACE_RECORDS[3:])
    assert client.get(f"/api/runs/{run_id}/trace?cursor={cursor}").json() == TRACE_RECORDS[2:]


def test_a_bad_trace_cursor_is_422(client, settings):
    run_id = post(client).json()["run_id"]
    write_trace(settings, run_id)
    size = (settings.results / str(run_id) / "trace.jsonl").stat().st_size
    for bad, needle in (("abc", "bad trace cursor"), ("-1", "bad trace cursor"),
                        (str(size + 1), "past the end"), ("5", "record boundary")):
        r = client.get(f"/api/runs/{run_id}/trace?cursor={bad}")
        assert r.status_code == 422 and needle in r.text, (bad, r.text)


def big_trace(path, n, header=None):
    """n frame records, 10 per virtual ms, after `header` if given; returns
    the records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    recs = [{"kind": "frame", "t_us": i * 100, "bus": "can_acu", "id": 0x100 + i % 7, "ext": False,
             "data": f"{i % 65536:04x}0000"} for i in range(n)]
    with open(path, "w") as f:
        if header is not None:
            f.write(json.dumps(header) + "\n")
        f.writelines(json.dumps(r, separators=(",", ":")) + "\n" for r in recs)
    return recs


def test_a_page_reads_from_its_cursor_not_from_the_start(tmp_path):
    from vhil.server.runs import trace_page
    path = tmp_path / "trace.jsonl"
    recs = big_trace(path, 1000)
    first, cursor = trace_page(path, 0, limit=500)
    assert [json.loads(x) for x in first] == recs[:500]
    # Spoil everything before the cursor: a page that re-read the start would choke.
    with open(path, "r+b") as f:
        f.write(b"x" * (cursor - 1))
    rest, end = trace_page(path, cursor)
    assert [json.loads(x) for x in rest] == recs[500:] and end == path.stat().st_size


def test_paging_a_large_trace_is_exact_and_each_page_costs_its_size(client, settings,
                                                                    monkeypatch):
    """362k records took ~6 s to page through when every page re-read the
    file from the start. With the cursor, the last page parses what the
    first does (counted, not timed: a wall-clock bound flaked on slow CI),
    and the pages add up to exactly the file."""
    from vhil.server import runs as vruns
    run_id = post(client).json()["run_id"]
    n, limit = 300_000, 50_000
    recs = big_trace(settings.results / str(run_id) / "trace.jsonl", n,
                     trace_header(RunStore(settings.db).get(run_id)))
    parsed = [0]

    class CountingJson:
        def __getattr__(self, name):
            return getattr(json, name)

        def loads(self, *a, **kw):
            parsed[0] += 1
            return json.loads(*a, **kw)

    monkeypatch.setattr(vruns, "json", CountingJson())
    costs, got, cursor = [], [], ""
    while True:
        parsed[0] = 0
        r = client.get(f"/api/runs/{run_id}/trace?limit={limit}" + (f"&cursor={cursor}" if cursor else ""))
        page = r.json()
        costs.append(parsed[0])
        got.extend(page)
        if len(page) < limit:
            break
        cursor = r.headers["x-trace-cursor"]
    assert got == recs
    assert len(costs) == n // limit + 1
    # Each page parses its own records (plus the header and the run's row),
    # never the records before its cursor.
    assert all(c <= limit + 20 for c in costs), costs


def test_trace_skips_a_half_written_line(settings):
    write_trace(settings, 1)
    with open(settings.results / "1" / "trace.jsonl", "a") as f:
        f.write('{"kind": "frame", "t_us": 3')
    assert len(read_trace(settings.results / "1" / "trace.jsonl")) == len(TRACE_RECORDS)


def test_artifacts_are_served_from_the_run_directory_only(client, settings):
    run_id = post(client).json()["run_id"]
    write_trace(settings, run_id)
    (settings.results / str(run_id) / "sim-logs").mkdir()
    (settings.results / str(run_id) / "sim-logs" / "renode.log").write_text("hello")
    (settings.results / "secret.txt").write_text("no")
    assert client.get(f"/api/runs/{run_id}/artifacts").json() == ["sim-logs/renode.log", "trace.jsonl"]
    assert client.get(f"/api/runs/{run_id}/artifacts/sim-logs/renode.log").text == "hello"
    for bad in ("../secret.txt", "..%2Fsecret.txt", "%2Fetc%2Fpasswd", "sim-logs", "nope"):
        assert client.get(f"/api/runs/{run_id}/artifacts/{bad}").status_code == 404, bad


def test_live_replays_a_finished_run_then_ends(client, settings, store):
    run_id = post(client).json()["run_id"]
    write_trace(settings, run_id)
    store.claim("w")
    store.finish(run_id, "passed", 20_000, {})
    with client.websocket_connect(f"/api/runs/{run_id}/live") as ws:
        got = [ws.receive_json() for _ in range(len(TRACE_RECORDS) + 1)]
    assert got[:-1] == TRACE_RECORDS
    assert got[-1] == {"kind": "end", "state": "passed"}


def test_live_streams_records_as_they_are_written(client, settings, store):
    run_id = post(client).json()["run_id"]
    store.claim("w")

    def worker():
        for rec in TRACE_RECORDS:
            time.sleep(0.05)
            write_trace(settings, run_id, [rec])
        time.sleep(0.3)
        store.finish(run_id, "failed", 20_000, {})

    t = threading.Thread(target=worker)
    t.start()
    with client.websocket_connect(f"/api/runs/{run_id}/live?kinds=frame") as ws:
        got = []
        while True:
            rec = ws.receive_json()
            got.append(rec)
            if rec["kind"] == "end":
                break
    t.join()
    assert got == [r for r in TRACE_RECORDS if r["kind"] == "frame"] + [{"kind": "end", "state": "failed"}]


def test_live_on_an_unknown_run_closes(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/runs/42/live") as ws:
            ws.receive_json()
    assert e.value.code == 4404


# -- the scenario executor against a fake Sim ----------------------------------------

class FakeBus:
    arbitrated = False      # Renode's hub: the worker estimates the load

    def __init__(self, sim, name):
        self.sim, self.name = sim, name

    def frames(self, ids=None, since_us=0):
        """What the bus delivered: as the probe, never its own sends."""
        out = []
        if self.name == "can_acu":   # a 0x100 heartbeat every 10 ms
            out += [Frame(t, 0x100, False, b"\x01") for t in range(10_000, self.sim.now + 1, 10_000)]
        return sorted((f for f in out if f.t_us >= since_us), key=lambda f: f.t_us)

    def sent(self, ids=None, since_us=0):
        out = [f for t, b, f in self.sim.scheduled if b == self.name and t <= self.sim.now]
        for (bus, _), job in self.sim.periodic.items():
            if bus == self.name:
                end = min(self.sim.now + 1, job["stop"])
                out += [Frame(t, job["id"], job["ext"], job["data"])
                        for t in range(job["start"], end, job["period"])]
        return sorted((f for f in out if f.t_us >= since_us), key=lambda f: f.t_us)

    def send_at(self, at_us, can_id, data=b"", extended=False):
        self.sim.calls.append(("send_at", self.sim.now, self.name, at_us, can_id, data))
        self.sim.scheduled.append((at_us, self.name, Frame(at_us, can_id, extended, data)))

    def send_periodic(self, key, can_id, data, period_ms, start_us=0, extended=False):
        self.sim.calls.append(("send_periodic", self.sim.now, self.name, key, can_id, period_ms, start_us))
        self.sim.periodic[(self.name, key)] = {
            "id": can_id, "data": data, "ext": extended, "period": int(period_ms * 1000),
            "start": start_us or self.sim.now, "stop": 1 << 62}

    def stop_periodic(self, key):
        self.sim.calls.append(("stop_periodic", self.sim.now, self.name, key))
        self.sim.periodic[(self.name, key)]["stop"] = self.sim.now


class FakeIO:
    def __init__(self, sim, board):
        self.sim, self.board = sim, board

    def set_input(self, port, pin, level):
        self.sim.calls.append(("set_input", self.sim.now, port, pin, level))
        name = f"{port}:{pin}"
        if name in self.sim.watched:   # loop the input back as an edge
            self.sim.edge_log.append(Edge(self.sim.now, name, level))

    def set_voltage(self, pin, volts):
        self.sim.calls.append(("set_voltage", self.sim.now, pin, volts))

    def watch(self, port, pin):
        self.sim.watched.add(f"{port}:{pin}")
        return f"{port}:{pin}"

    def edges(self, pin="", since_us=0):
        return [e for e in self.sim.edge_log if e.t_us >= since_us]

    def level(self, pin):
        return self.sim.levels.get(pin, False)

    def sample(self, symbol, size, period_us, first_us, now_us):
        """As VhilGpioProbe.Sample: at first_us and every period_us after it,
        a first time already past taken at once (at now_us)."""
        s = [first_us, period_us, symbol, size]
        self.sim.samplers.setdefault(self.board, []).append(s)
        if s[0] <= now_us:
            self._take(s, now_us)
            s[0] += period_us * ((now_us - s[0]) // period_us + 1)

    def samples(self, now_us):
        """What the emulation sampled up to now_us, each at its time."""
        for s in self.sim.samplers.get(self.board, []):
            while s[0] <= now_us:
                self._take(s, s[0])
                s[0] += s[1]
        out, self.sim.taken[self.board] = self.sim.taken.get(self.board, []), []
        return out

    def _take(self, s, t):
        self.sim.calls.append(("read_symbol", t, self.board, s[2], s[3]))
        self.sim.taken.setdefault(self.board, []).append((t, s[2], t // 1000))


class FakeSim:
    def __init__(self, system=ECU, quantum_us=500):
        self.system = System(system)
        self.now, self.quantum = 0, quantum_us
        self.calls, self.scheduled, self.edge_log, self.watched = [], [], [], set()
        self.periodic = {}
        self.levels = {}
        self.samplers, self.taken = {}, {}     # board -> FakeIO.sample's
        self.started = self.stopped = False

    def __enter__(self):
        self.started = True
        return self

    def __exit__(self, *exc):
        self.stopped = True

    def now_us(self):
        return self.now

    def run_for(self, ms=0, us=0):
        step = int(ms * 1000) + us
        # Time moves in whole quanta, as Renode's sync quantum does.
        self.now += max(self.quantum, -(-step // self.quantum) * self.quantum)
        return self.now

    def can(self, bus):
        return FakeBus(self, bus)

    def io(self, board):
        return FakeIO(self, board)

    def read_symbol(self, board, name, size=1):
        self.calls.append(("read_symbol", self.now, board, name, size))
        return self.now // 1000


def run_fake(tmp_path, scenario, **kw):
    sim = FakeSim()
    trace = TraceWriter(tmp_path / "trace.jsonl")
    summary = execute_run(sim, scenario, trace, **kw)
    trace.close()
    return sim, summary, read_trace(tmp_path / "trace.jsonl")


def test_executor_streams_frames_samples_and_edges_in_time_order(tmp_path):
    scenario = {"kind": "run", "virtual_ms": 300, "slice_ms": 100,
                "stimuli": [{"kind": "can_send", "at_ms": 5, "bus": "can_inv", "id": 0x360, "data": "aa"},
                            {"kind": "can_periodic", "at_ms": 0, "bus": "can_dash", "id": 0x10,
                             "data": "", "period_ms": 20, "until_ms": 150},
                            {"kind": "gpio", "at_ms": 120, "board": "ecu", "pin": "PB5", "level": True},
                            {"kind": "analog", "at_ms": 50, "board": "ecu", "pin": "PF7", "volts": 1.5}],
                "watch": [{"kind": "symbol", "board": "ecu", "name": "g_x", "size": 1, "period_ms": 10},
                          {"kind": "pin", "board": "ecu", "pin": "PB5"}]}
    progress = []
    sim, summary, trace = run_fake(tmp_path, scenario, progress=progress.append)
    assert sim.now == 300_000 and progress == [100_000, 200_000, 300_000]
    assert summary["frames"] == {"can_inv": 0, "can_dash": 0, "can_acu": 30}
    assert summary["sent"] == {"can_inv": 1, "can_dash": 8, "can_acu": 0}
    assert summary["samples"] == 31 and summary["edges"] == 1
    assert [r["t_us"] for r in trace] == sorted(r["t_us"] for r in trace)
    hb = [r for r in trace if r["kind"] == "frame" and r["id"] == 0x100]
    assert [r["t_us"] for r in hb] == list(range(10_000, 300_001, 10_000))
    assert all(r["bus"] == "can_acu" and r["data"] == "01" and r["ext"] is False for r in hb)
    samples = [r for r in trace if r["kind"] == "sample"]
    assert [r["t_us"] for r in samples] == list(range(0, 300_001, 10_000))
    assert samples[5] == {"kind": "sample", "t_us": 50_000, "board": "ecu", "name": "g_x", "value": 50}
    assert [r for r in trace if r["kind"] == "edge"] == [
        {"kind": "edge", "t_us": 120_000, "board": "ecu", "pin": "PB5", "level": 1}]
    # The scenario's own frames, as the probe sent them, marked as stimulus.
    assert [r for r in trace if r.get("src")] == [
        {"kind": "frame", "t_us": t, "bus": bus, "id": i, "ext": False, "data": d, "src": "stimulus"}
        for t, bus, i, d in sorted([(5_000, "can_inv", 0x360, "aa")]
                                   + [(t, "can_dash", 0x10, "") for t in range(0, 150_000, 20_000)])]
    assert [(r["t_us"], r["text"]) for r in trace if r["kind"] == "log"] == [
        (0, "stimulus can_periodic can_dash 0x10 [] every 20 ms"),
        (50_000, "stimulus analog ecu.PF7 = 1.5 V"),
        (120_000, "stimulus gpio ecu.PB5 = 1"),
        (150_000, "stimulus can_periodic can_dash 0x10 [] stopped")]
    calls = {c[0]: c for c in sim.calls if c[0] != "read_symbol"}
    assert calls["send_at"] == ("send_at", 0, "can_inv", 5_000, 0x360, b"\xaa")
    assert calls["send_periodic"] == ("send_periodic", 0, "can_dash", "stim1", 0x10, 20, 0)
    assert calls["stop_periodic"] == ("stop_periodic", 150_000, "can_dash", "stim1")
    assert calls["set_input"] == ("set_input", 120_000, "sysbus.gpioPortB", 5, True)
    assert calls["set_voltage"] == ("set_voltage", 50_000, "PF7", 1.5)


class ExactSim(FakeSim):
    """RunFor stops where it is asked to, as Renode's does: a stop between two
    sync points starts the next quantum there, moving every later sync point."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stops = []

    def run_for(self, ms=0, us=0):
        self.now += int(ms * 1000) + us
        self.stops.append(self.now)
        return self.now


def test_a_stimulus_between_sync_points_runs_at_the_next(tmp_path):
    """docs/scenarios.md, "Times": the ECU system syncs every 500 us. A pin
    set at 120.2 ms stopped the run there and shifted every later sync point
    (the observer effect #183 fixed for samples); it runs at 120.5 ms now,
    and the trace and the summary say so. A frame's time is moved the same
    way (the probe sends it at the next sync point anyway, #130)."""
    scenario = {"kind": "run", "virtual_ms": 300, "slice_ms": 100,
                "stimuli": [{"kind": "gpio", "at_ms": 120.2, "board": "ecu", "pin": "PB5",
                             "level": True},
                            {"kind": "can_periodic", "at_ms": 0, "bus": "can_dash", "id": 0x10,
                             "data": "", "period_ms": 20, "until_ms": 150.25},
                            {"kind": "analog", "at_ms": 50.5, "board": "ecu", "pin": "PF7",
                             "volts": 1.5}]}
    sim = ExactSim()
    trace = TraceWriter(tmp_path / "trace.jsonl")
    summary = execute_run(sim, scenario, trace)
    trace.close()
    recs = read_trace(tmp_path / "trace.jsonl")
    assert all(t % 500 == 0 for t in sim.stops), sim.stops
    calls = {c[0]: c for c in sim.calls if c[0] != "read_symbol"}
    assert calls["set_input"] == ("set_input", 120_500, "sysbus.gpioPortB", 5, True)
    assert calls["stop_periodic"] == ("stop_periodic", 150_500, "can_dash", "stim1")
    assert calls["set_voltage"] == ("set_voltage", 50_500, "PF7", 1.5)   # on the grid already
    assert summary["aligned"] == [
        {"row": "stimuli[0].at_ms", "at_us": 120_200, "applied_us": 120_500},
        {"row": "stimuli[1].until_ms", "at_us": 150_250, "applied_us": 150_500}]
    logs = [(r["t_us"], r["text"]) for r in recs if r["kind"] == "log"]
    assert (120_500, "stimuli[0].at_ms: 120.2 ms is between sync points (every 0.5 ms): "
                     "applied at 120.5 ms") in logs
    assert (120_500, "stimulus gpio ecu.PB5 = 1") in logs


def test_a_scenario_on_the_grid_is_not_aligned(tmp_path):
    _, summary, _ = run_fake(tmp_path, {"kind": "run", "virtual_ms": 200, "slice_ms": 100,
                                        "stimuli": [{"kind": "gpio", "at_ms": 120.5,
                                                     "board": "ecu", "pin": "PB5",
                                                     "level": True}]})
    assert "aligned" not in summary


def test_executor_is_flushed_slice_by_slice(tmp_path):
    seen = []

    def cancelled():
        seen.append(len(read_trace(tmp_path / "trace.jsonl")))
        return False

    run_fake(tmp_path, {"kind": "run", "virtual_ms": 300, "slice_ms": 100}, cancelled=cancelled)
    # Checked after the first two slices, with their 10 heartbeats and a
    # bus_load record per bus (3) each on disk.
    assert seen == [13, 26]


def test_executor_reports_each_bus_load_per_slice(tmp_path):
    """#174: a bus_load record per bus and slice. On Renode's hub it is an
    estimate from the frames seen: ten 0x100 [01] heartbeats per 100 ms at
    500 kbit/s, each 1 + 11 + 3 + 4 + 8 + 15 bits with their stuff bits, the
    tail (10) and intermission (3)."""
    _, summary, trace = run_fake(tmp_path, {"kind": "run", "virtual_ms": 200, "slice_ms": 100})
    loads = [r for r in trace if r["kind"] == "bus_load"]
    assert [(r["t_us"], r["bus"]) for r in loads] == [
        (t, b) for t in (100_000, 200_000) for b in ("can_inv", "can_dash", "can_acu")]
    hb = canframe.busy_bits(0x100, b"\x01")
    acu = [r for r in loads if r["bus"] == "can_acu"]
    # The first slice runs from power-on (0) to 100 ms inclusive.
    assert acu[0] == {"kind": "bus_load", "t_us": 100_000, "bus": "can_acu",
                      "load": round(10 * hb / (100_001e-6 * 500_000), 4), "window_us": 100_001,
                      "exact": False}
    assert all(r["load"] == 0 for r in loads if r["bus"] != "can_acu")
    assert summary["bus_load"]["can_acu"]["peak"] == max(r["load"] for r in acu)
    assert summary["bus_load"]["can_inv"] == {"mean": 0, "peak": 0}


def test_executor_stops_at_the_slice_after_a_cancel(tmp_path):
    checks = iter([False, True])
    with pytest.raises(Cancelled):
        sim, _, _ = run_fake(tmp_path, {"kind": "run", "virtual_ms": 1000, "slice_ms": 100},
                             cancelled=lambda: next(checks))
    trace = read_trace(tmp_path / "trace.jsonl")
    assert max(r["t_us"] for r in trace) == 200_000


def test_executor_rejects_a_symbol_on_the_wrong_kind_of_pin(tmp_path):
    with pytest.raises(ValueError, match="not gpio"):
        run_fake(tmp_path, {"kind": "run", "virtual_ms": 10,
                            "watch": [{"kind": "pin", "board": "ecu", "pin": "PF7"}]})


def test_executor_stops_a_named_periodic(tmp_path):
    scenario = {"kind": "run", "virtual_ms": 200, "stimuli": [
        {"kind": "stop_periodic", "at_ms": 100, "periodic": "hb"},
        {"kind": "can_periodic", "name": "hb", "at_ms": 0, "bus": "can_dash", "id": 0x10,
         "data": "", "period_ms": 20}]}
    sim, summary, trace = run_fake(tmp_path, scenario)
    assert ("stop_periodic", 100_000, "can_dash", "stim1") in sim.calls
    assert summary["sent"]["can_dash"] == 5
    assert (100_000, "stimulus stop_periodic hb (can_dash 0x10)") in [
        (r["t_us"], r["text"]) for r in trace if r["kind"] == "log"]


def test_executor_watches_what_the_expects_read(tmp_path):
    """An expect's pin is watched from the start, its level then written as
    an initial edge; its symbol is sampled every 10 ms."""
    scenario = {"kind": "run", "virtual_ms": 100, "stimuli": [
        {"kind": "gpio", "at_ms": 50, "board": "ecu", "pin": "PB5", "level": True}],
        "expect": [{"check": "eventually", "signal": "pin:ecu.PB5", "value": 1},
                   {"check": "eventually", "signal": "symbol:ecu.g_x", "value": 5}]}
    sim = FakeSim()
    sim.levels["sysbus.gpioPortB:5"] = False
    trace = TraceWriter(tmp_path / "trace.jsonl")
    summary = execute_run(sim, scenario, trace)
    trace.close()
    records = read_trace(tmp_path / "trace.jsonl")
    assert [r for r in records if r["kind"] == "edge"] == [
        {"kind": "edge", "t_us": 0, "board": "ecu", "pin": "PB5", "level": 0, "initial": True},
        {"kind": "edge", "t_us": 50_000, "board": "ecu", "pin": "PB5", "level": 1}]
    assert summary["samples"] == 11
    assert ("read_symbol", 50_000, "ecu", "g_x", 1) in sim.calls


def test_a_watch_op_starts_recording_mid_run(tmp_path):
    """`watch` as a stimulus (a live session's op): a symbol sampled from
    its at_ms, a pin's edges from its at_ms with its level then."""
    scenario = {"kind": "run", "virtual_ms": 100, "stimuli": [
        {"kind": "watch", "at_ms": 40, "board": "ecu", "symbol": "g_x", "period_ms": 20},
        {"kind": "watch", "at_ms": 30, "board": "ecu", "pin": "PB5"},
        {"kind": "gpio", "at_ms": 60, "board": "ecu", "pin": "PB5", "level": True}]}
    sim = FakeSim()
    trace = TraceWriter(tmp_path / "trace.jsonl")
    summary = execute_run(sim, scenario, trace)
    trace.close()
    records = read_trace(tmp_path / "trace.jsonl")
    assert [r["t_us"] for r in records if r["kind"] == "sample"] == [40_000, 60_000, 80_000,
                                                                     100_000]
    assert summary["samples"] == 4
    assert [r for r in records if r["kind"] == "edge"] == [
        {"kind": "edge", "t_us": 30_000, "board": "ecu", "pin": "PB5", "level": 0, "initial": True},
        {"kind": "edge", "t_us": 60_000, "board": "ecu", "pin": "PB5", "level": 1}]
    assert [(r["t_us"], r["text"]) for r in records if r["kind"] == "log"][:2] == [
        (30_000, "stimulus watch ecu.PB5"), (40_000, "stimulus watch ecu.g_x every 20 ms")]




def _stops(scenario, tmp_path):
    """The times a fake run of the scenario stopped at, and its trace."""
    sim, stops = FakeSim(), []
    run_for = sim.run_for
    sim.run_for = lambda ms=0, us=0: stops.append(run_for(ms, us)) or stops[-1]
    trace = TraceWriter(tmp_path / "trace.jsonl")
    execute_run(sim, scenario, trace)
    trace.close()
    return sim, stops, read_trace(tmp_path / "trace.jsonl")


def test_symbols_are_sampled_without_stopping_the_run(tmp_path):
    """The emulation samples a watched symbol itself (BoardIO.sample): the
    run stops only at its slices, each collecting what was sampled in it."""
    sim, stops, trace = _stops({"kind": "run", "virtual_ms": 20, "slice_ms": 10, "watch": [
        {"kind": "symbol", "board": "ecu", "name": "g_x", "size": 1, "period_ms": 5}]}, tmp_path)
    assert stops == [10_000, 20_000]
    assert [(r["t_us"], r["value"]) for r in trace if r["kind"] == "sample"] == [
        (0, 0), (5_000, 5), (10_000, 10), (15_000, 15), (20_000, 20)]


def test_a_watch_op_is_one_symbol_or_one_pin():
    from pydantic import ValidationError
    from vhil.server.runs import RunScenario
    ok = RunScenario.model_validate({"kind": "run", "stimuli": [
        {"kind": "watch", "at_ms": 5, "board": "ecu", "symbol": "g_x", "size": 2}]})
    assert ok.stimuli[0].symbol == "g_x"
    for bad in ({"board": "ecu"}, {"board": "ecu", "symbol": "g", "pin": "PB5"},
                {"board": "ecu", "pin": "PB5", "period_ms": 10},
                {"board": "ecu; quit", "symbol": "g"}, {"board": "ecu", "symbol": "g x"}):
        with pytest.raises(ValidationError):
            RunScenario.model_validate({"kind": "run", "stimuli": [{"kind": "watch", **bad}]})


def test_with_the_state_view_a_run_records_what_the_panel_shows(tmp_path):
    """Every board's state view (catalog/firmware/<id>.yaml state_view): its
    pins from power-on with their level; its symbols at their period, those
    the image lacks left out and logged."""
    sim = FakeSim()
    sim.levels["sysbus.gpioPortB:4"] = True
    trace = TraceWriter(tmp_path / "trace.jsonl")
    execute_run(sim, {"kind": "run", "virtual_ms": 30}, trace, state_view=True)
    trace.close()
    records = read_trace(tmp_path / "trace.jsonl")
    assert [(r["pin"], r["level"]) for r in records if r.get("initial")] == [
        ("PB4", 1), ("PB6", 0)]
    assert not [r for r in records if r["kind"] == "sample"]     # FakeSim has no image
    log = [r["text"] for r in records if r["kind"] == "log"]
    assert log == ["state view: not in the firmware, not recorded: ecu.g_last_ctrl_state, "
                   "ecu.g_last_start_button, "
                   "ecu.g_last_t11_8_9, ecu.g_discharge_fault, ecu.g_last_torque_pct, "
                   "ecu.g_last_apps1_raw, ecu.g_last_apps2_raw, ecu.g_last_brake_raw"]


def test_with_the_state_view_its_symbols_are_sampled_at_their_period(tmp_path, monkeypatch):
    import vhil.worker as vw
    monkeypatch.setattr(vw, "has_symbol", lambda sim, board, name: True)
    sim = FakeSim()
    trace = TraceWriter(tmp_path / "trace.jsonl")
    execute_run(sim, {"kind": "run", "virtual_ms": 100}, trace, state_view=True)
    trace.close()
    times = {}
    for r in read_trace(tmp_path / "trace.jsonl"):
        if r["kind"] == "sample":
            times.setdefault(r["name"], []).append(r["t_us"])
    assert times["g_last_ctrl_state"] == list(range(0, 100_001, 10_000))
    assert times["g_last_brake_raw"] == [0, 50_000, 100_000]


# -- the worker --------------------------------------------------------------------

class FixedResolver:
    def __init__(self, images=None, error=None):
        self.images, self.error, self.asked = images or {"ecu": Path("/fw/ECU08.elf")}, error, []

    def resolve(self, system, refs):
        self.asked.append((system.id, refs))
        if self.error:
            raise self.error
        return self.images


def test_worker_runs_a_queued_run_to_passed(settings, store):
    run_id = store.create("ecu", "", {"ecu": "dev"}, {"kind": "run", "virtual_ms": 200, "slice_ms": 100})
    sims = []

    def factory(path, firmware, log_path):
        assert path == REPO / "systems" / "ecu.yaml" and firmware == {"ecu": Path("/fw/ECU08.elf")}
        sims.append(FakeSim())
        return sims[0]

    resolver = FixedResolver()
    worker = Worker(settings, worker_id="w1", sim_factory=factory, resolver=resolver)
    assert worker.run_once() == run_id
    assert worker.run_once() is None
    assert resolver.asked == [("ecu", {"ecu": "dev"})]
    assert sims[0].started and sims[0].stopped
    run = store.get(run_id)
    assert run["state"] == "passed" and run["worker"] == "w1" and run["virtual_us"] == 200_000
    assert run["summary"]["frames"]["can_acu"] == 20
    assert run["summary"]["firmware"] == {"ecu": "/fw/ECU08.elf"}
    trace = read_trace(settings.results / str(run_id) / "trace.jsonl")
    assert trace[0]["kind"] == "log" and "ECU08.elf" in trace[0]["text"]


def test_worker_honours_a_cancel_from_the_api(settings, store):
    run_id = store.create("ecu", "", {}, {"kind": "run", "virtual_ms": 10_000, "slice_ms": 100})

    class CancelledMidway(FakeSim):
        def run_for(self, ms=0, us=0):
            if self.now >= 300_000:
                store.cancel(run_id)
            return super().run_for(ms, us)

    worker = Worker(settings, sim_factory=lambda *a: CancelledMidway(), resolver=FixedResolver())
    worker.run_once()
    run = store.get(run_id)
    assert run["state"] == "cancelled" and 300_000 <= run["virtual_us"] < 1_000_000
    assert run["summary"]["frames"]["can_acu"] == run["virtual_us"] // 10_000
    assert run["summary"]["cancelled_at_us"] == run["virtual_us"]
    assert read_trace(settings.results / str(run_id) / "trace.jsonl")[-1]["text"] == "cancelled"


def test_worker_turns_a_failure_into_an_error_run(settings, store):
    run_id = store.create("ecu", "", {}, RUN)
    worker = Worker(settings, sim_factory=lambda *a: FakeSim(),
                    resolver=FixedResolver(error=RuntimeError("no built image")))
    worker.run_once()
    run = store.get(run_id)
    assert run["state"] == "error" and "no built image" in run["summary"]["error"]
    assert (settings.results / str(run_id) / "worker-error.txt").is_file()


def contract_firmware(tmp_path) -> dict:
    """An ECU image whose source declares 0x100 with a value table: what the
    worker reads the run's contract from (vhil/server/decode.py)."""
    messages = tmp_path / "ecu@dev" / "Core" / "Inc" / "can" / "messages"
    messages.mkdir(parents=True)
    (messages / "hb.def").write_text(
        'CAN_MSG(VCU_heartbeat, 0x100, 1, "VCU", 10)\n'
        '    FIELD_LE (state, uint8_t, 0, 8, 1, 0, "enum")\n'
        'CAN_MSG_END(VCU_heartbeat)\n'
        'CAN_VAL(VCU_heartbeat, state, 1, "One")\n')
    return {"ecu": tmp_path / "ecu@dev" / "build" / "ECU08.elf"}


def expect_run(store, *expects, virtual_ms=200):
    return store.create("ecu", "", {}, {"kind": "run", "virtual_ms": virtual_ms, "slice_ms": 100,
                                        "name": "hb", "expect": list(expects)})


def test_worker_passes_a_run_whose_expects_hold(settings, store, tmp_path):
    run_id = expect_run(
        store, {"check": "eventually", "name": "one", "signal": "frame:can_acu.VCU_heartbeat.state",
                "value": "One", "until_ms": 50},
        {"check": "period", "signal": "frame:can_acu.0x100", "min_ms": 9, "max_ms": 11})
    worker = Worker(settings, sim_factory=lambda *a: FakeSim(),
                    resolver=FixedResolver(contract_firmware(tmp_path)))
    worker.run_once()
    run = store.get(run_id)
    assert run["state"] == "passed", run["summary"]
    s = run["summary"]
    assert s["expects_passed"] == 2 and s["expects_failed"] == 0
    assert s["expects"][0] | {"detail": ""} == {
        "index": 0, "name": "one", "check": "eventually",
        "signal": "frame:can_acu.VCU_heartbeat.state", "at_us": 0, "until_us": 50_000,
        "passed": True, "t_us": 10_000, "value": "One", "detail": ""}
    logs = [r["text"] for r in read_trace(settings.results / str(run_id) / "trace.jsonl")
            if r["kind"] == "log"]
    assert any(t.startswith("expect passed one frame:can_acu.VCU_heartbeat.state") for t in logs)


def test_worker_fails_a_run_whose_expect_does_not_hold(settings, store, tmp_path):
    run_id = expect_run(
        store, {"check": "never", "signal": "frame:can_acu.VCU_heartbeat.state", "value": 1},
        {"check": "count", "signal": "frame:can_acu.0x100", "max": 0, "at_ms": 150})
    worker = Worker(settings, sim_factory=lambda *a: FakeSim(),
                    resolver=FixedResolver(contract_firmware(tmp_path)))
    worker.run_once()
    run = store.get(run_id)
    assert run["state"] == "failed"
    s = run["summary"]
    assert s["expects_failed"] == 2 and s["frames"]["can_acu"] == 20
    assert [(r["passed"], r["t_us"], r["value"]) for r in s["expects"]] == [
        (False, 10_000, 1), (False, 150_000, 6)]


def test_worker_fails_an_expect_the_contract_cannot_read(settings, store):
    """No .def next to the image: a field expect fails and says why; a raw
    id needs no contract."""
    run_id = expect_run(
        store, {"check": "eventually", "signal": "frame:can_acu.VCU_heartbeat.state", "value": 1},
        {"check": "count", "signal": "frame:can_acu.0x100", "min": 1})
    Worker(settings, sim_factory=lambda *a: FakeSim(), resolver=FixedResolver()).run_once()
    s = store.get(run_id)["summary"]
    assert [r["passed"] for r in s["expects"]] == [False, True]
    assert "no message VCU_heartbeat on can_acu" in s["expects"][0]["detail"]


def test_last_run_per_scenario(store):
    first = expect_run(store)
    store.create("ecu", "", {}, RUN)
    second = expect_run(store)
    other = store.create("ecu", "", {}, {**RUN, "name": "other"})
    store.create("ams", "", {}, {**RUN, "name": "hb"})
    last = store.last_by_scenario("ecu")
    assert {k: v["id"] for k, v in last.items()} == {"hb": second, "other": other}
    assert first < second


# -- firmware resolution and pytest ---------------------------------------------------

def test_resolver_reuses_an_image_built_at_the_ref(tmp_path):
    system = System(ECU)
    elf = tmp_path / "ecu@feat_x" / "build" / "ECU08.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"\x7fELF")
    bl = tmp_path / "can-bootloader@v1.7.0" / "build" / "Release" / "CAN_BL.elf"
    bl.parent.mkdir(parents=True)
    bl.write_bytes(b"\x7fELF")
    (tmp_path / "built.txt").write_text(f"ecu={elf}\necu.bootloader={bl}\n")
    resolver = FirmwareResolver(tmp_path, build=False)
    assert resolver.resolve(system, {"ecu": "feat/x"}) == {"ecu": elf.resolve(),
                                                           "ecu.bootloader": bl.resolve()}
    # The catalogue's ref (dev) was never built here.
    with pytest.raises(RuntimeError, match="no built image"):
        resolver.resolve(system, {})


def test_image_env_names_bootloaders_by_firmware_id():
    system = System(REPO / "systems" / "ecu.yaml")
    env = image_env(system, {k: Path(f"/fw/{k}.elf") for k in system.images()})
    assert env["VHIL_ECU_ELF"] == "/fw/ecu.elf"
    assert env["VHIL_CAN_BOOTLOADER_ELF"] == "/fw/ecu.bootloader.elf"


@pytest.mark.parametrize("body, state", [("assert True", "passed"), ("assert False", "failed")])
def test_pytest_scenario_maps_the_outcome(tmp_path, body, state):
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "conftest.py").write_text(
        "def pytest_addoption(parser):\n    parser.addoption('--sim-log-dir')\n")
    (ws / "tests" / "test_x.py").write_text(f"def test_x():\n    {body}\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    trace = TraceWriter(run_dir / "trace.jsonl")
    got, summary = execute_pytest({"select": "tests/test_x.py::test_x"}, run_dir, ws, {}, trace,
                                  cancelled=lambda: False)
    trace.close()
    assert got == state and summary["tests"] == 1
    assert (run_dir / "junit.xml").is_file() and (run_dir / "pytest.txt").is_file()


# -- the pytest picker -----------------------------------------------------------------

def test_workspace(tmp_path, body="def test_a():\n    pass\n"):
    ws = tmp_path / "ws"
    (ws / "tests" / "sim").mkdir(parents=True)
    (ws / "tests" / "sim" / "test_one.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('n', [1, 2])\ndef test_p(n):\n    pass\n\n"
        "class TestK:\n    def test_m(self):\n        pass\n")
    (ws / "tests" / "sim" / "test_two.py").write_text(body)
    (ws / "tests" / "sim" / "helper.py").write_text("X = 1\n")
    return ws


test_workspace.__test__ = False   # a helper, not a test


def test_the_catalog_lists_test_files_and_collected_ids(tmp_path):
    from vhil.server.runs import TestCatalog
    got = TestCatalog(test_workspace(tmp_path)).collect()
    assert got["files"] == ["tests/sim/test_one.py", "tests/sim/test_two.py"]
    assert got["tests"] == ["tests/sim/test_one.py::test_p[1]", "tests/sim/test_one.py::test_p[2]",
                            "tests/sim/test_one.py::TestK::test_m", "tests/sim/test_two.py::test_a"]
    assert "error" not in got


def test_the_catalog_is_cached_until_a_test_file_changes(tmp_path, monkeypatch):
    from vhil.server import runs
    ws = test_workspace(tmp_path)
    catalog = runs.TestCatalog(ws)
    calls = []
    real = runs.subprocess.run
    monkeypatch.setattr(runs.subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    first = catalog.collect()
    assert catalog.collect() is first and len(calls) == 1
    (ws / "tests" / "sim" / "test_two.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    assert catalog.collect()["tests"][-1] == "tests/sim/test_two.py::test_b" and len(calls) == 2


def test_a_tree_that_does_not_collect_still_lists_its_files(tmp_path):
    from vhil.server.runs import TestCatalog
    got = TestCatalog(test_workspace(tmp_path, body="def test_a(:\n")).collect()
    assert got["files"] == ["tests/sim/test_one.py", "tests/sim/test_two.py"]
    assert "exited 2" in got["error"]


def test_api_tests_lists_the_repos_sim_tests_and_they_can_be_started(client):
    got = client.get("/api/tests").json()
    assert got["root"] == "tests/sim" and "error" not in got, got.get("error")
    assert "tests/sim/test_probe.py" in got["files"]
    node = "tests/sim/test_probe.py::test_send_periodic_with_a_future_start"
    assert node in got["tests"]
    r = post(client, {"kind": "pytest", "select": node, "timeout_s": 600})
    assert r.status_code == 201, r.text
    assert client.get(f"/api/runs/{r.json()['run_id']}").json()["scenario"]["timeout_s"] == 600


# -- heartbeats and reclaim ------------------------------------------------------------

def test_claim_beats_and_counts_attempts(store):
    run_id = store.create("ecu", "", {}, RUN)
    run = store.claim("w1", now=1000.0)
    assert run["heartbeat"] == 1000.0 and run["attempts"] == 1
    assert store.heartbeat(run_id, "w1", now=1010.0)
    assert store.get(run_id)["heartbeat"] == 1010.0
    assert store.progress(run_id, 5_000, worker="w1", now=1020.0)
    assert store.get(run_id)["heartbeat"] == 1020.0
    # Another worker's beat or progress doesn't count, nor does a finished run's.
    assert not store.heartbeat(run_id, "w2") and not store.progress(run_id, 1, worker="w2")
    store.finish(run_id, "passed", 5_000, {}, worker="w1")
    assert not store.heartbeat(run_id, "w1")


def test_a_live_heartbeat_is_never_reclaimed(store):
    run_id = store.create("ecu", "", {}, RUN)
    store.claim("w1", now=1000.0)
    store.heartbeat(run_id, "w1", now=1050.0)
    assert store.reclaim(60, now=1100.0) == []
    assert store.get(run_id)["state"] == "running"


def test_a_dead_workers_run_is_requeued_then_errors_after_its_attempts(store):
    run_id = store.create("ecu", "", {}, RUN)
    store.claim("dead1", now=1000.0)
    store.progress(run_id, 300_000, worker="dead1", now=1000.0)
    assert store.reclaim(60, max_attempts=2, now=1061.0) == [
        {"id": run_id, "state": "queued", "worker": "dead1", "attempts": 1}]
    run = store.get(run_id)
    assert run["state"] == "queued" and run["worker"] is None and run["heartbeat"] is None
    assert run["virtual_us"] == 0 and run["summary"] == {"reclaimed_from": "dead1", "attempt": 1}
    # The dead worker coming back finds it is not its run any more.
    assert store.finish(run_id, "passed", 1, {}, worker="dead1") == "queued"
    run = store.claim("dead2", now=2000.0)
    assert run["id"] == run_id and run["attempts"] == 2
    assert store.reclaim(60, max_attempts=2, now=2061.0) == [
        {"id": run_id, "state": "error", "worker": "dead2", "attempts": 2}]
    run = store.get(run_id)
    assert run["state"] == "error" and run["finished"]
    assert run["summary"]["error"] == "worker dead2 stopped heartbeating; attempt 2 of 2, not retried"
    assert store.claim("w3") is None


def test_reclaim_leaves_queued_and_finished_runs_alone(store):
    first = store.create("ecu", "", {}, RUN)
    second = store.create("ecu", "", {}, RUN)
    store.create("ecu", "", {}, RUN)   # stays queued
    store.claim("w", now=0.0)          # first (oldest first)
    store.finish(first, "failed", 0, {})
    store.claim("w", now=0.0)
    store.cancel(second)
    assert store.reclaim(1, now=1e9) == []


def test_a_running_row_from_before_heartbeats_counts_as_stale(settings):
    import sqlite3
    db = sqlite3.connect(settings.db)
    db.executescript("""CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, state TEXT NOT NULL, system TEXT NOT NULL,
        ref TEXT NOT NULL DEFAULT '', firmware TEXT NOT NULL DEFAULT '{}', scenario TEXT NOT NULL,
        created TEXT NOT NULL, started TEXT, finished TEXT, virtual_us INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '{}', worker TEXT);
        INSERT INTO runs (state, system, scenario, created, worker)
        VALUES ('running', 'ecu', '{"kind": "run", "virtual_ms": 100}', 'x', 'old');""")
    db.commit()
    db.close()
    store = RunStore(settings.db)        # adds the columns
    run = store.get(1)
    assert run["heartbeat"] is None and run["attempts"] == 0
    assert [r["state"] for r in store.reclaim(60)] == ["queued"]


def test_two_workers_reclaiming_at_once_move_each_run_once(settings):
    seed = RunStore(settings.db)
    ids = [seed.create("ecu", "", {}, RUN) for _ in range(20)]
    for _ in ids:
        seed.claim("dead", now=0.0)
    moved: list[dict] = []
    start = threading.Barrier(2)

    def poll():
        own = RunStore(settings.db)
        start.wait()
        moved.extend(own.reclaim(60))

    threads = [threading.Thread(target=poll) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["id"] for r in moved) == ids
    assert {r["state"] for r in seed.list(100)} == {"queued"}


class SlowSim(FakeSim):
    """Virtual time advances, but every step takes wall time."""

    def __init__(self, step_s=0.01, hook=None):
        super().__init__()
        self.step_s, self.hook = step_s, hook

    def run_for(self, ms=0, us=0):
        time.sleep(self.step_s)
        if self.hook:
            self.hook(self)
        return super().run_for(ms, us)


def test_a_worker_reclaims_and_reruns_a_dead_workers_run(settings, store):
    run_id = store.create("ecu", "", {}, {"kind": "run", "virtual_ms": 200, "slice_ms": 100})
    store.claim("dead", now=time.time() - 3600)
    d = settings.results / str(run_id)
    d.mkdir(parents=True)
    (d / "trace.jsonl").write_text('{"kind": "log", "t_us": 0, "text": "first attempt"}\n')
    worker = Worker(settings, worker_id="w2", sim_factory=lambda *a: FakeSim(),
                    resolver=FixedResolver())
    assert worker.run_once() == run_id
    run = store.get(run_id)
    assert run["state"] == "passed" and run["worker"] == "w2" and run["attempts"] == 2
    assert run["summary"]["frames"]["can_acu"] == 20
    # The first attempt's trace is kept aside; the new one starts afresh.
    assert read_trace(d / "trace.attempt1.jsonl")[0]["text"] == "first attempt"
    trace = read_trace(d / "trace.jsonl")
    assert trace[0]["text"] == "attempt 2 of 2: reclaimed from dead, whose heartbeat stopped"
    assert not any(r.get("text") == "first attempt" for r in trace)


def test_a_first_attempt_sets_aside_results_left_by_a_rolled_back_run(settings, store):
    """A database restored to a snapshot reuses the ids of the runs it rolled
    back; their results are still on the runs volume and must not be mixed
    into the new run's."""
    run_id = store.create("ecu", "", {}, {"kind": "run", "virtual_ms": 200, "slice_ms": 100})
    d = settings.results / str(run_id)
    (d / "system").mkdir(parents=True)
    (d / "trace.jsonl").write_text('{"kind": "log", "t_us": 0, "text": "rolled back run"}\n')
    (d / "system" / "ecu.yaml").write_text("old")
    worker = Worker(settings, worker_id="w1", sim_factory=lambda *a: FakeSim(),
                    resolver=FixedResolver())
    assert worker.run_once() == run_id
    assert store.get(run_id)["state"] == "passed"
    assert not any(r.get("text") == "rolled back run" for r in read_trace(d / "trace.jsonl"))
    assert not (d / "system" / "ecu.yaml").exists()
    [orphan] = (settings.results / ".orphaned").iterdir()
    assert orphan.name.startswith(f"{run_id}-")
    assert read_trace(orphan / "trace.jsonl")[0]["text"] == "rolled back run"


def test_a_live_worker_keeps_its_run_while_another_polls(settings, store):
    """Worker A runs a slow run (about 3 s), beating every 20 ms; worker B
    polls with a 1 s reclaim timeout the whole time and never takes it. The
    run outlasts the timeout three times over, so a missing heartbeat would
    be caught; the timeout leaves room for a loaded CI runner delaying a
    beat's SQLite write (0.3 s did not)."""
    run_id = store.create("ecu", "", {}, {"kind": "run", "virtual_ms": 1500, "slice_ms": 10})
    a = Worker(settings, worker_id="a", sim_factory=lambda *x: SlowSim(0.02),
               resolver=FixedResolver(), heartbeat_s=0.02, reclaim_after_s=1.0)
    b = Worker(settings, worker_id="b", sim_factory=lambda *x: FakeSim(),
               resolver=FixedResolver(), heartbeat_s=0.02, reclaim_after_s=1.0)
    t = threading.Thread(target=a.run_once)
    t.start()
    while store.get(run_id)["state"] == "queued":
        time.sleep(0.001)
    polls = 0
    while t.is_alive():
        assert b.run_once() is None
        polls += 1
        time.sleep(0.02)
    t.join()
    run = store.get(run_id)
    assert polls > 10, "the run was too quick to show anything"
    assert run["state"] == "passed" and run["worker"] == "a" and run["attempts"] == 1


def test_the_heartbeat_thread_keeps_a_run_with_no_slices_claimed(settings, store):
    """A firmware build (no slices) longer than the reclaim timeout: 2.5 s
    against 0.8 s, which leaves room for a loaded CI runner delaying a beat
    (0.5 s against 0.2 s did not)."""
    run_id = store.create("ecu", "", {}, RUN)

    class SlowBuild(FixedResolver):
        def resolve(self, system, refs):
            time.sleep(2.5)
            return super().resolve(system, refs)

    a = Worker(settings, worker_id="a", sim_factory=lambda *x: FakeSim(), resolver=SlowBuild(),
               heartbeat_s=0.02, reclaim_after_s=0.8)
    t = threading.Thread(target=a.run_once)
    t.start()
    while t.is_alive():
        assert store.reclaim(0.8) == []
        time.sleep(0.02)
    t.join()
    assert store.get(run_id)["state"] == "passed"


def test_a_worker_whose_run_was_reclaimed_lets_it_go(settings, store):
    """A worker that stalled past the timeout (its run reclaimed and taken by
    another) stops at its next slice and writes no final state."""
    run_id = store.create("ecu", "", {}, {"kind": "run", "virtual_ms": 10_000, "slice_ms": 100})

    def steal(sim):
        if sim.now >= 300_000 and store.get(run_id)["worker"] == "a":
            assert store.reclaim(60, now=time.time() + 3600)[0]["state"] == "queued"
            store.claim("b")

    a = Worker(settings, worker_id="a", sim_factory=lambda *x: SlowSim(0, steal),
               resolver=FixedResolver())
    assert a.run_once() == run_id
    run = store.get(run_id)
    assert run["state"] == "running" and run["worker"] == "b" and run["attempts"] == 2
    texts = [r.get("text") for r in read_trace(settings.results / str(run_id) / "trace.jsonl")]
    assert texts[-1] == "worker a lost the run: it was reclaimed"


def test_a_pytest_run_reclaimed_from_its_worker_is_stopped(tmp_path):
    from vhil.worker import Lost
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "conftest.py").write_text(
        "def pytest_addoption(parser):\n    parser.addoption('--sim-log-dir')\n")
    (ws / "tests" / "test_x.py").write_text("import time\ndef test_x():\n    time.sleep(60)\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    trace = TraceWriter(run_dir / "trace.jsonl")

    def lost():
        raise Lost()

    t0 = time.monotonic()
    with pytest.raises(Lost):
        execute_pytest({"select": "tests/test_x.py::test_x"}, run_dir, ws, {}, trace, cancelled=lost)
    trace.close()
    assert time.monotonic() - t0 < 30


def test_worker_rejects_a_reclaim_timeout_within_two_heartbeats(settings):
    with pytest.raises(ValueError):
        Worker(settings, heartbeat_s=10, reclaim_after_s=15)


def test_live_restarts_when_a_new_attempt_replaces_the_trace(client, settings, store):
    run_id = post(client).json()["run_id"]
    store.claim("w")
    path = settings.results / str(run_id) / "trace.jsonl"

    def worker():
        write_trace(settings, run_id, TRACE_RECORDS)
        time.sleep(0.5)
        path.rename(path.with_name("trace.attempt1.jsonl"))
        write_trace(settings, run_id, TRACE_RECORDS[:1])
        time.sleep(0.5)
        store.finish(run_id, "passed", 0, {})

    t = threading.Thread(target=worker)
    t.start()
    with client.websocket_connect(f"/api/runs/{run_id}/live") as ws:
        got = []
        while (rec := ws.receive_json())["kind"] != "end":
            got.append(rec)
    t.join()
    assert got == TRACE_RECORDS + TRACE_RECORDS[:1]


# -- a reused id's stale results (a database restored to a snapshot, #136) ----------

STALE = [{"kind": "frame", "t_us": 1, "bus": "can_acu", "id": 0x666, "ext": False, "data": "ff"},
         {"kind": "log", "t_us": 2, "text": "rolled back run"}]


def stale_results(settings, run_id, token="0" * 32):
    """What a rolled-back run with this id left: a trace (with its own run's
    header, or with none, from before headers) and an artifact."""
    d = settings.results / str(run_id)
    d.mkdir(parents=True)
    with open(d / "trace.jsonl", "w") as f:
        if token is not None:
            f.write(json.dumps({"kind": "run", "t_us": 0, "run": run_id, "token": token,
                                "attempt": 1}) + "\n")
        f.writelines(json.dumps(r) + "\n" for r in STALE)
    (d / "old.log").write_text("old run")
    return d


def test_each_run_gets_its_own_token(store):
    a, b = store.create("ecu", "", {}, RUN), store.create("ecu", "", {}, RUN)
    ta, tb = store.get(a)["token"], store.get(b)["token"]
    assert len(ta) == 32 and ta != tb


def test_an_old_database_gets_the_token_column(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, state TEXT NOT NULL, "
               "system TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '', firmware TEXT NOT NULL "
               "DEFAULT '{}', scenario TEXT NOT NULL, created TEXT NOT NULL, started TEXT, "
               "finished TEXT, virtual_us INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL "
               "DEFAULT '{}', worker TEXT)")
    db.execute("INSERT INTO runs (state, system, scenario, created) VALUES "
               "('passed', 'ecu', '{}', 'x')")
    db.commit()
    db.close()
    store = RunStore(tmp_path / "old.db")
    assert store.get(1)["token"] == ""
    assert store.get(store.create("ecu", "", {}, RUN))["token"]


@pytest.mark.parametrize("token", ["0" * 32, None], ids=["other-runs-header", "no-header"])
def test_a_reused_ids_stale_trace_is_never_served(client, settings, token):
    run_id = post(client).json()["run_id"]
    stale_results(settings, run_id, token)
    r = client.get(f"/api/runs/{run_id}/trace")
    assert r.json() == [] and r.headers["x-trace-cursor"] == "0"
    assert client.get(f"/api/runs/{run_id}/artifacts").json() == []
    assert client.get(f"/api/runs/{run_id}/artifacts/old.log").status_code == 404


def test_a_runs_own_trace_is_served_without_its_header(client, settings):
    run_id = post(client).json()["run_id"]
    write_trace(settings, run_id)
    path = settings.results / str(run_id) / "trace.jsonl"
    assert json.loads(path.read_text().splitlines()[0])["kind"] == "run"
    assert client.get(f"/api/runs/{run_id}/trace").json() == TRACE_RECORDS
    assert read_trace(path) == TRACE_RECORDS


def test_a_run_from_before_tokens_is_served_as_before(client, settings, store):
    run_id = post(client).json()["run_id"]
    db = store._connect()
    db.execute("UPDATE runs SET token = '' WHERE id = ?", (run_id,))
    db.close()
    write_trace(settings, run_id, header=False)
    assert client.get(f"/api/runs/{run_id}/trace").json() == TRACE_RECORDS
    assert client.get(f"/api/runs/{run_id}/artifacts").json() == ["trace.jsonl"]


def test_a_client_tailing_a_queued_run_never_sees_the_stale_trace(client, settings, store):
    """The run page or the editor opens /live while the run is queued, with a
    rolled-back run's trace still at its path. The client gets nothing of it:
    only the run's own records, once the worker has set the stale directory
    aside and started its trace."""
    run_id = store.create("ecu", "", {}, {"kind": "run", "virtual_ms": 200, "slice_ms": 100})
    stale_results(settings, run_id)
    worker = Worker(settings, worker_id="w1", sim_factory=lambda *a: FakeSim(),
                    resolver=FixedResolver())

    def claim_later():
        time.sleep(0.6)          # several of the live loop's polls on the stale trace
        assert worker.run_once() == run_id

    t = threading.Thread(target=claim_later)
    t.start()
    with client.websocket_connect(f"/api/runs/{run_id}/live") as ws:
        got = []
        while (rec := ws.receive_json())["kind"] != "end":
            got.append(rec)
    t.join()
    assert rec == {"kind": "end", "state": "passed"}
    assert got and not any(r in STALE for r in got), got
    assert all(r["kind"] != "run" for r in got)
    assert sum(r["kind"] == "frame" and r["bus"] == "can_acu" for r in got) == 20
    assert got == client.get(f"/api/runs/{run_id}/trace").json()
    # The stale results are kept aside; the run's directory is its own.
    [orphan] = (settings.results / ".orphaned").iterdir()
    assert read_trace(orphan / "trace.jsonl") == STALE
    assert "old.log" not in client.get(f"/api/runs/{run_id}/artifacts").json()


def test_a_queued_run_cancelled_before_its_claim_shows_nothing_stale(client, settings, store):
    run_id = post(client).json()["run_id"]
    stale_results(settings, run_id)
    store.cancel(run_id)
    with client.websocket_connect(f"/api/runs/{run_id}/live") as ws:
        assert ws.receive_json() == {"kind": "end", "state": "cancelled"}


def test_the_worker_writes_the_runs_header_first(settings, store):
    run_id = store.create("ecu", "", {}, {"kind": "run", "virtual_ms": 200, "slice_ms": 100})
    worker = Worker(settings, worker_id="w1", sim_factory=lambda *a: FakeSim(),
                    resolver=FixedResolver())
    assert worker.run_once() == run_id
    path = settings.results / str(run_id) / "trace.jsonl"
    token = store.get(run_id)["token"]
    header = json.loads(path.read_text().splitlines()[0])
    assert header == {"kind": "run", "t_us": 0, "run": run_id, "token": token, "attempt": 1,
                      "contract": 1}
    assert read_trace(path, token=token) == read_trace(path) != []
    assert read_trace(path, token="f" * 32) == []


# -- ownership ------------------------------------------------------------------------

def github_app(settings, monkeypatch, admins=()):
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_ID", "client-id")
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("VHIL_SESSION_SECRET", "test-session-secret-0123456789abcdef")
    monkeypatch.delenv("VHIL_PUBLIC_URL", raising=False)
    monkeypatch.delenv("VHIL_GITHUB_APP_ID", raising=False)
    return create_app(replace(settings, auth="github", admins=frozenset(admins)))


def as_user(app, login):
    """A client signed in as `login` (a session as the OAuth callback makes it)."""
    c = TestClient(app)
    cookie, session = app.state.auth.new_session({"login": login, "name": login, "avatar_url": ""})
    c.cookies.set("vhil_session", cookie)
    c.headers["X-CSRF-Token"] = app.state.auth.csrf_token(session)
    return c


def test_a_run_records_its_owner_and_only_they_or_an_admin_may_cancel_it(settings, monkeypatch):
    app = github_app(settings, monkeypatch, admins={"carol"})
    alice, bob, carol = (as_user(app, n) for n in ("alice", "bob", "Carol"))
    first = post(alice).json()["run_id"]
    second = post(alice).json()["run_id"]
    assert alice.get(f"/api/runs/{first}").json()["owner"] == "alice"
    assert alice.get(f"/api/runs/{first}").json()["can_cancel"] is True
    assert {r["id"]: r["can_cancel"] for r in bob.get("/api/runs").json()} == {first: False,
                                                                               second: False}
    r = bob.post(f"/api/runs/{first}/cancel")
    assert r.status_code == 403 and "alice" in r.text
    assert alice.get(f"/api/runs/{first}").json()["state"] == "queued"
    assert alice.post(f"/api/runs/{first}/cancel").json()["state"] == "cancelled"
    # An admin (VHIL_ADMINS, case-insensitive) may cancel anyone's run.
    assert carol.get(f"/api/runs/{second}").json()["can_cancel"] is True
    assert carol.post(f"/api/runs/{second}/cancel").json()["state"] == "cancelled"


def test_a_run_from_before_owners_is_an_admins_to_cancel(settings, monkeypatch, store):
    app = github_app(settings, monkeypatch, admins={"carol"})
    run_id = store.create("ecu", "", {}, RUN)       # owner ''
    assert as_user(app, "alice").post(f"/api/runs/{run_id}/cancel").status_code == 403
    assert as_user(app, "carol").post(f"/api/runs/{run_id}/cancel").status_code == 200


def test_dev_mode_runs_belong_to_dev_and_anyone_cancels(client):
    run_id = post(client).json()["run_id"]
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["owner"] == "dev" and run["can_cancel"] is True


def test_an_old_database_gets_the_owner_column(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, state TEXT NOT NULL, "
               "system TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '', firmware TEXT NOT NULL DEFAULT "
               "'{}', scenario TEXT NOT NULL, created TEXT NOT NULL, started TEXT, finished TEXT, "
               "virtual_us INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '{}', "
               "worker TEXT)")
    db.execute("INSERT INTO runs (state, system, scenario, created) VALUES ('queued', 'ecu', '{}', 'x')")
    db.commit()
    db.close()
    assert RunStore(tmp_path / "old.db").get(1)["owner"] == ""


@pytest.mark.parametrize("system", ["../ecu", "ECU", "ecu/x", "-ecu", "ecu\n", "e" * 65])
def test_the_run_requests_system_is_a_system_id(client, system):
    r = client.post("/api/runs", json={"system": system, "scenario": RUN})
    assert r.status_code == 422 and "system id" in r.text


@pytest.mark.parametrize("ref", ["feаt/x", "a\nb", "dev\n", "-x"])
def test_refs_are_plain_ascii(client, ref):
    assert post(client, ref=ref).status_code == 422
    assert post(client, firmware={"ecu": ref}).status_code == 422


# -- artifacts are never rendered (stored XSS) ---------------------------------------------

def test_artifacts_are_plain_text_or_attachments_never_markup(client, settings):
    run_id = post(client).json()["run_id"]
    d = settings.results / str(run_id)
    write_trace(settings, run_id, [])      # the run's own directory
    (d / "x.html").write_text("<script>alert(1)</script>")
    (d / "x.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>')
    (d / "blob.bin").write_bytes(b"\x00\x01<html>\xff")
    for name in ("x.html", "x.svg"):
        r = client.get(f"/api/runs/{run_id}/artifacts/{name}")
        assert r.status_code == 200 and r.text.startswith("<")
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"].startswith("sandbox")
    r = client.get(f"/api/runs/{run_id}/artifacts/blob.bin")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-security-policy"].startswith("sandbox")
    assert r.content == b"\x00\x01<html>\xff"
