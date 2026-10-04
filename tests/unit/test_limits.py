"""Per-run and queue limits (vhil.server.runs.Limits; docs/deploy.md, "Limits"):
what the API refuses to queue and where a worker stops a run."""
import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.server.runs import Limits, QueueFull, RunStore  # noqa: E402
from vhil.system import REPO  # noqa: E402
from vhil.worker import TraceLimit, TraceWriter, execute_pytest  # noqa: E402

RUN = {"kind": "run", "virtual_ms": 100}


@pytest.fixture
def settings(tmp_path):
    return Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")


def client(settings, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return TestClient(create_app(settings))


def post(c, scenario=RUN):
    return c.post("/api/runs", json={"system": "ecu", "scenario": scenario})


def test_limits_come_from_the_environment(monkeypatch):
    assert Limits.from_env() == Limits()
    monkeypatch.setenv("VHIL_MAX_VIRTUAL_MS", "1000")
    monkeypatch.setenv("VHIL_MAX_TRACE_MB", "3")
    lim = Limits.from_env()
    assert lim.max_virtual_ms == 1000 and lim.max_trace_bytes == 3 << 20


def test_a_run_longer_than_the_limit_is_422(settings, monkeypatch):
    c = client(settings, monkeypatch, VHIL_MAX_VIRTUAL_MS=1000)
    assert post(c, {"kind": "run", "virtual_ms": 1000}).status_code == 201
    r = post(c, {"kind": "run", "virtual_ms": 1001})
    assert r.status_code == 422 and "virtual_ms" in json.dumps(r.json())


def test_too_many_stimuli_or_watches_are_422(settings, monkeypatch):
    c = client(settings, monkeypatch, VHIL_MAX_STIMULI=2, VHIL_MAX_WATCHES=1)
    stim = {"kind": "can_send", "bus": "acu", "id": 0x100, "data": "00"}
    r = post(c, {**RUN, "stimuli": [stim] * 3})
    assert r.status_code == 422 and "stimuli" in json.dumps(r.json())
    watch = {"kind": "symbol", "board": "ecu", "name": "x"}
    r = post(c, {**RUN, "watch": [watch] * 2})
    assert r.status_code == 422 and "watch" in json.dumps(r.json())


def test_a_user_with_too_many_active_runs_gets_429(settings, monkeypatch):
    c = client(settings, monkeypatch, VHIL_MAX_QUEUED_PER_USER=2)
    ids = [post(c).json()["run_id"] for _ in range(2)]
    r = post(c)
    assert r.status_code == 429 and "dev already has 2" in r.json()["detail"]
    # A finished (here: cancelled) run frees its slot.
    c.post(f"/api/runs/{ids[0]}/cancel")
    assert post(c).status_code == 201
    assert RunStore(settings.db).get(ids[1])["owner"] == "dev"


def test_the_whole_queue_is_bounded(settings, monkeypatch):
    c = client(settings, monkeypatch, VHIL_MAX_QUEUED=1)
    assert post(c).status_code == 201
    r = post(c)
    assert r.status_code == 429 and "queue is full" in r.json()["detail"]


def test_the_store_counts_running_runs_and_other_users_separately(tmp_path):
    store = RunStore(tmp_path / "vhil.db")
    store.create("ecu", "", {}, RUN, owner="a", max_active_per_user=1)
    store.claim("w")                                    # running still counts
    with pytest.raises(QueueFull):
        store.create("ecu", "", {}, RUN, owner="a", max_active_per_user=1)
    store.create("ecu", "", {}, RUN, owner="b", max_active_per_user=1)
    with pytest.raises(QueueFull):
        store.create("ecu", "", {}, RUN, owner="c", max_active=2)


def test_a_trace_stops_at_its_limit_and_still_takes_the_closing_log(tmp_path):
    path = tmp_path / "trace.jsonl"
    trace = TraceWriter(path, max_bytes=300)
    frame = {"kind": "frame", "t_us": 1, "bus": "acu", "id": 1, "ext": False, "data": "00" * 8}
    trace.write([frame])
    with pytest.raises(TraceLimit):
        trace.write([dict(frame, t_us=t) for t in range(2, 10)])
    trace.write([dict(frame, t_us=20)])               # dropped: the trace is full
    trace.log(20, "error: the end")
    trace.close()
    recs = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["kind"] for r in recs] == ["frame", "log", "log"]
    assert "trace limit" in recs[1]["text"] and recs[2]["text"] == "error: the end"


def test_a_pytest_run_that_floods_its_output_is_stopped(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "conftest.py").write_text(
        "def pytest_addoption(parser):\n    parser.addoption('--sim-log-dir')\n")
    # pytest holds a test's own stdout until the test ends, so the test
    # appends to the run's output file directly, as an endless log would.
    (tmp_path / "tests" / "test_flood.py").write_text(
        "import time\n"
        "def test_flood():\n"
        f"    with open({str(tmp_path / 'pytest.txt')!r}, 'a') as f:\n"
        "        while True:\n"
        "            f.write('x' * 65536 + '\\n'); f.flush(); time.sleep(0.01)\n")
    trace = TraceWriter(tmp_path / "trace.jsonl")
    state, summary = execute_pytest(
        {"select": "tests/test_flood.py", "timeout_s": 30}, tmp_path, tmp_path,
        {"PYTHONPATH": str(REPO)}, trace, lambda: False, max_output_bytes=1 << 20)
    trace.close()
    assert state == "error" and "VHIL_MAX_OUTPUT_MB" in summary["error"], summary
