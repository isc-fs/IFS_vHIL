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

from vhil import canframe  # noqa: E402
from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.sim import Frame, assert_cadence  # noqa: E402
from vhil.system import REPO  # noqa: E402
from vhil.worker import Worker  # noqa: E402

HEARTBEAT = 0x100
CONTROL_PERIOD_US = 10_000
# A run's times count from power-on (vhil.worker.execute_run): the ECU's app
# starts after its bootloader's 2 s auto-jump window, so the scenario starts
# its stimuli after that.
BOOT_MS = 2500


class Pinned:
    """The images under test (--ecu-elf, --can-bootloader-elf), whatever their ref."""

    def __init__(self, images):
        self.images = images

    def commits(self, system, refs, known=None, log=None):
        return {}

    def resolve(self, system, refs, commits=None):
        return self.images


def test_a_queued_run_streams_its_trace_and_passes(tmp_path, images):
    settings = Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")
    client = TestClient(create_app(settings))
    r = client.post("/api/runs", json={"system": "ecu", "scenario": {
        "kind": "run", "virtual_ms": BOOT_MS + 1000,
        "stimuli": [{"kind": "can_send", "at_ms": BOOT_MS + 300, "bus": "can_acu", "id": 0x020,
                     "data": "01"},
                    {"kind": "can_periodic", "at_ms": BOOT_MS + 500, "bus": "can_inv", "id": 0x461,
                     "data": "00", "period_ms": 10, "until_ms": BOOT_MS + 595}],
        "watch": [{"kind": "symbol", "board": "ecu", "name": "g_last_ctrl_state", "period_ms": 10}]}})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    worker = Worker(settings, worker_id="test",
                    resolver=Pinned({k: p.resolve() for k, p in images("ecu").items()}))
    assert worker.run_once() == run_id

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["state"] == "passed", run["summary"]
    assert run["virtual_us"] >= (BOOT_MS + 1000) * 1000 and run["worker"] == "test"

    frames = client.get(f"/api/runs/{run_id}/trace?kinds=frame").json()
    hb = [Frame(f["t_us"], f["id"], f["ext"], bytes.fromhex(f["data"]))
          for f in frames if f["id"] == HEARTBEAT]
    assert {f["bus"] for f in frames if f["id"] == HEARTBEAT} == {"can_acu"}
    # On ControlTask's grid from the second frame on, within a kernel tick
    # (ECU configTICK_RATE_HZ 1000, FreeRTOSConfig.h:67), as without the
    # worker's slicing: the trace doesn't depend on how the run was cut.
    assert_cadence(hb[1:], period_us=CONTROL_PERIOD_US, jitter_us=1000, min_count=90)
    assert run["summary"]["frames"]["can_acu"] >= len(hb)

    # The scenario's own frames are frame records too, at the virtual time
    # the probe's frame went out (its last EOF bit on the bus model; the bus
    # is idle at each of these offers), marked as stimulus and counted
    # apart. (A periodic start is offered up to one 500 us quantum late, as
    # tests/sim/test_probe.py shows; the trace has when it really went.)
    stim = [(f["t_us"], f["bus"], f["id"], f["data"]) for f in frames if f.get("src") == "stimulus"]
    ok_us = canframe.frame_bits(0x020, b"\x01") * 2      # 500 kbit/s: 2 us a bit
    inv_us = canframe.frame_bits(0x461, b"\x00") * 2
    start, boot = stim[1][0] - inv_us, BOOT_MS * 1000
    assert boot + 500_000 <= start <= boot + 500_500, start
    assert stim == [(boot + 300_000 + ok_us, "can_acu", 0x020, "01")] + [
        (t + inv_us, "can_inv", 0x461, "00") for t in range(start, boot + 595_000, 10_000)]
    assert run["summary"]["sent"] == {"can_acu": 1, "can_inv": 10, "can_dash": 0}
    assert all(f["id"] != 0x461 for f in frames if not f.get("src")), "a send seen as received"

    samples = client.get(f"/api/runs/{run_id}/trace?kinds=sample").json()
    assert len(samples) >= 100
    # The scenario's watch, and the ECU's state view (vhil/stateview.py),
    # which every web-app run records.
    names = {s["name"] for s in samples}
    assert "g_last_ctrl_state" in names and {"g_last_t11_8_9", "g_last_torque_pct"} <= names
    assert all(isinstance(s["value"], int) for s in samples)

    # The live socket of a finished run replays the same file, then ends.
    with client.websocket_connect(f"/api/runs/{run_id}/live?kinds=frame,sample") as ws:
        got = []
        while (rec := ws.receive_json())["kind"] != "end":
            got.append(rec)
    assert rec == {"kind": "end", "state": "passed"}
    assert len(got) == len(frames) + len(samples)
    assert client.get(f"/api/runs/{run_id}/artifacts/renode.log").status_code == 200
