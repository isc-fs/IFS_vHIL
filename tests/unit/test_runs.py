"""vhil.server.runs + vhil.worker (M5.2, #114): the queue, the API, the
scenario executor against a fake Sim. The real Sim end to end is
tests/sim/test_server_runs.py."""
import json
import threading
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.server.runs import RunStore, read_trace  # noqa: E402
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
    ({"system": "ecu", "scenario": {"kind": "run"}}, "virtual_ms"),
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
    ({"system": "ecu", "ref": "0000000", "scenario": RUN}, "not the workspace"),
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


def write_trace(settings, run_id, records=TRACE_RECORDS):
    d = settings.results / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "trace.jsonl", "a") as f:
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
    def __init__(self, sim, name):
        self.sim, self.name = sim, name

    def frames(self, ids=None, since_us=0):
        out = [f for t, b, f in self.sim.scheduled if b == self.name and t <= self.sim.now]
        if self.name == "can_acu":   # a 0x100 heartbeat every 10 ms
            out += [Frame(t, 0x100, False, b"\x01") for t in range(10_000, self.sim.now + 1, 10_000)]
        return sorted((f for f in out if f.t_us >= since_us), key=lambda f: f.t_us)

    def send_at(self, at_us, can_id, data=b"", extended=False):
        self.sim.calls.append(("send_at", self.sim.now, self.name, at_us, can_id, data))
        self.sim.scheduled.append((at_us, self.name, Frame(at_us, can_id, extended, data)))

    def send_periodic(self, key, can_id, data, period_ms, start_us=0, extended=False):
        self.sim.calls.append(("send_periodic", self.sim.now, self.name, key, can_id, period_ms, start_us))

    def stop_periodic(self, key):
        self.sim.calls.append(("stop_periodic", self.sim.now, self.name, key))


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


class FakeSim:
    def __init__(self, system=ECU, quantum_us=500):
        self.system = System(system)
        self.now, self.quantum = 0, quantum_us
        self.calls, self.scheduled, self.edge_log, self.watched = [], [], [], set()
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
    assert summary["frames"] == {"can_inv": 1, "can_dash": 0, "can_acu": 30}
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
    assert [(r["t_us"], r["text"]) for r in trace if r["kind"] == "log"] == [
        (0, "stimulus can_periodic can_dash 0x10 [] every 20 ms"),
        (5_000, "stimulus can_send can_inv 0x360 [aa]"),
        (50_000, "stimulus analog ecu.PF7 = 1.5 V"),
        (120_000, "stimulus gpio ecu.PB5 = 1"),
        (150_000, "stimulus can_periodic can_dash 0x10 [] stopped")]
    calls = {c[0]: c for c in sim.calls if c[0] != "read_symbol"}
    assert calls["send_at"] == ("send_at", 0, "can_inv", 5_000, 0x360, b"\xaa")
    assert calls["send_periodic"] == ("send_periodic", 0, "can_dash", "stim1", 0x10, 20, 0)
    assert calls["stop_periodic"] == ("stop_periodic", 150_000, "can_dash", "stim1")
    assert calls["set_input"] == ("set_input", 120_000, "sysbus.gpioPortB", 5, True)
    assert calls["set_voltage"] == ("set_voltage", 50_000, "PF7", 1.5)


def test_executor_is_flushed_slice_by_slice(tmp_path):
    seen = []

    def cancelled():
        seen.append(len(read_trace(tmp_path / "trace.jsonl")))
        return False

    run_fake(tmp_path, {"kind": "run", "virtual_ms": 300, "slice_ms": 100}, cancelled=cancelled)
    # Checked after the first two slices, with their 10 heartbeats each on disk.
    assert seen == [10, 20]


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


# -- firmware resolution and pytest ---------------------------------------------------

def test_resolver_reuses_an_image_built_at_the_ref(tmp_path):
    system = System(ECU)
    elf = tmp_path / "ecu@feat_x" / "build" / "ECU08.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"\x7fELF")
    (tmp_path / "built.txt").write_text(f"ecu={elf}\n")
    resolver = FirmwareResolver(tmp_path, build=False)
    assert resolver.resolve(system, {"ecu": "feat/x"}) == {"ecu": elf.resolve()}
    # The catalogue's ref (dev) was never built here.
    with pytest.raises(RuntimeError, match="no built image"):
        resolver.resolve(system, {})


def test_image_env_names_bootloaders_by_firmware_id():
    system = System(REPO / "systems" / "ecu-bl.yaml")
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
