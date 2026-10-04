"""The web app's run path end to end (M5.2, #114): a `run` queued through the
API, executed by one worker iteration on the real Sim, its trace read back
through the API and the live WebSocket.

Firmware facts (IFS08-CE-ECU, as tests/sim/test_ecu_boot.py):
  0x100 on can_acu every ControlTask tick (10 ms)    control_task.cpp:305-323
  g_last_ctrl_state mirrors the control FSM          tests/sim/test_ecu_ams_gate.py
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.sim import Frame, assert_period  # noqa: E402
from vhil.system import REPO  # noqa: E402
from vhil.worker import Worker  # noqa: E402

HEARTBEAT = 0x100
CONTROL_PERIOD_US = 10_000


class Pinned:
    """The image under test (--ecu-elf / VHIL_ECU_ELF), whatever its ref."""

    def __init__(self, elf):
        self.elf = elf

    def resolve(self, system, refs):
        return {"ecu": self.elf}


def test_a_queued_run_streams_its_trace_and_passes(tmp_path, firmware):
    settings = Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")
    client = TestClient(create_app(settings))
    r = client.post("/api/runs", json={"system": "ecu", "scenario": {
        "kind": "run", "virtual_ms": 1000,
        "watch": [{"kind": "symbol", "board": "ecu", "name": "g_last_ctrl_state", "period_ms": 10}]}})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    worker = Worker(settings, worker_id="test", resolver=Pinned(firmware("ecu").resolve()))
    assert worker.run_once() == run_id

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["state"] == "passed", run["summary"]
    assert run["virtual_us"] >= 1_000_000 and run["worker"] == "test"

    frames = client.get(f"/api/runs/{run_id}/trace?kinds=frame").json()
    hb = [Frame(f["t_us"], f["id"], f["ext"], bytes.fromhex(f["data"]))
          for f in frames if f["id"] == HEARTBEAT]
    assert {f["bus"] for f in frames if f["id"] == HEARTBEAT} == {"can_acu"}
    # Exact from the second frame on, as without the worker's slicing: the
    # trace doesn't depend on how the run was cut into slices.
    assert_period(hb[1:], period_us=CONTROL_PERIOD_US, tolerance_us=0, min_count=90)
    assert run["summary"]["frames"]["can_acu"] >= len(hb)

    samples = client.get(f"/api/runs/{run_id}/trace?kinds=sample").json()
    assert len(samples) >= 100
    assert {s["name"] for s in samples} == {"g_last_ctrl_state"}
    assert all(isinstance(s["value"], int) for s in samples)

    # The live socket of a finished run replays the same file, then ends.
    with client.websocket_connect(f"/api/runs/{run_id}/live?kinds=frame,sample") as ws:
        got = []
        while (rec := ws.receive_json())["kind"] != "end":
            got.append(rec)
    assert rec == {"kind": "end", "state": "passed"}
    assert len(got) == len(frames) + len(samples)
    assert client.get(f"/api/runs/{run_id}/artifacts/renode.log").status_code == 200
