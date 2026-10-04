"""M5.3 inspect (#115): a run's CAN contract (vhil/server/decode.py), the
history filters, and the run page's static modules, including the browser
decoder checked against vhil/candef.py under node (tests/js/)."""
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil import candef  # noqa: E402
from vhil.server import create_app, decode  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.server.runs import RunStore  # noqa: E402
from vhil.system import REPO  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "candef"
RUN = {"kind": "run", "virtual_ms": 100}
NODE = shutil.which("node") or next((p for p in ("/opt/node/bin/node",) if os.path.exists(p)), None)


@pytest.fixture
def settings(tmp_path):
    return Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")


@pytest.fixture
def store(settings):
    return RunStore(settings.db)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def _finished(store, system, firmware, state="passed"):
    run_id = store.create(system, "", {}, RUN)
    store.claim("w")
    store.finish(run_id, state, 100_000, {"firmware": {k: str(v) for k, v in firmware.items()}})
    return run_id


ELFS = {"ecu": FIX / "ecu" / "build" / "ECU08.elf", "ams": FIX / "ams" / "build" / "AMS.elf"}


# -- the contract ----------------------------------------------------------------

def test_contract_maps_each_boards_messages_onto_its_buses(client, store):
    run_id = _finished(store, "ecu-ams", ELFS)
    c = client.get(f"/api/runs/{run_id}/contract").json()
    assert set(c["buses"]) == {"can_inv", "can_dash", "can_acu"}
    assert c["boards"]["ecu"]["sender"] == "VCU" and c["boards"]["ecu"]["messages"] == 5
    assert c["boards"]["ams"]["sender"] == "AMS" and c["boards"]["ams"]["messages"] == 3
    acu = c["buses"]["can_acu"]
    # The ECU's own frames, the AMS's own, and 0x4A0 from its sender.
    assert acu[str(0x700)]["name"] == "PitDiag_status" and acu[str(0x700)]["board"] == "ecu"
    assert acu[str(0x135)]["name"] == "ACU_currents" and acu[str(0x135)]["board"] == "ams"
    assert acu[str(0x4A0)]["board"] == "ams" and acu[str(0x4A0)]["sender"] == "AMS"
    # The AMS is only on the ACU bus; the ECU's contract applies to all three.
    assert str(0x135) not in c["buses"]["can_inv"] and str(0x700) in c["buses"]["can_inv"]
    assert c["conflicts"] == []
    f = {x["name"]: x for x in acu[str(0x135)]["fields"]}
    assert f["current_accu_dA"] == {"name": "current_accu_dA", "be": True, "signed": True, "start": 7,
                                    "length": 16, "factor": 0.1, "offset": 0.0, "unit": "A"}


def test_two_boards_declaring_one_id_differently_is_a_conflict_the_sender_wins(tmp_path, store):
    ecu = tmp_path / "ecu"
    shutil.copytree(FIX / "ecu", ecu)
    status = ecu / candef.MESSAGES / "ams_status.def"
    status.write_text(status.read_text().replace("CAN_MSG(AMS_status, 0x4A0, 8,", "CAN_MSG(AMS_status, 0x4A0, 6,"))
    run_id = _finished(store, "ecu-ams", {"ecu": ecu / "build" / "ECU08.elf", "ams": ELFS["ams"]})
    c = decode.run_contract(store.get(run_id), REPO, tmp_path)
    assert c["conflicts"] == [{"bus": "can_acu", "id": 0x4A0, "name": "AMS_status",
                               "kept": "ams", "dropped": "ecu"}]
    assert c["buses"]["can_acu"][str(0x4A0)]["dlc"] == 8
    assert c["buses"]["can_inv"][str(0x4A0)]["dlc"] == 6     # only the ECU there


def test_a_running_run_uses_the_elf_the_worker_will_resolve(tmp_path, store):
    # FirmwareResolver.expected: <fw-dir>/<firmware id>@<ref>/<build.elf>
    shutil.copytree(FIX / "ecu", tmp_path / "ecu@dev")
    run_id = store.create("ecu", "", {}, RUN)
    store.claim("w")
    c = decode.run_contract(store.get(run_id), REPO, tmp_path)
    assert c["boards"]["ecu"]["elf"] == str((tmp_path / "ecu@dev" / "build" / "ECU08.elf").resolve())
    assert c["buses"]["can_acu"][str(0x704)]["name"] == "PitDiag_health"


def test_a_missing_source_is_reported_not_raised(client, store, tmp_path):
    run_id = _finished(store, "ecu", {"ecu": tmp_path / "gone" / "build" / "ECU08.elf"}, state="error")
    r = client.get(f"/api/runs/{run_id}/contract")
    assert r.status_code == 200
    c = r.json()
    assert "error" in c["boards"]["ecu"] and all(not m for m in c["buses"].values())


def test_contract_of_an_unknown_run_is_404(client):
    assert client.get("/api/runs/77/contract").status_code == 404


# -- history -------------------------------------------------------------------------

def test_history_filters_by_system_and_state(client, store):
    a = _finished(store, "ecu", {})
    b = _finished(store, "ams", {}, state="failed")
    c = store.create("ecu", "", {}, RUN)
    ids = lambda q: [r["id"] for r in client.get(f"/api/runs{q}").json()]  # noqa: E731
    assert ids("") == [c, b, a]
    assert ids("?system=ecu") == [c, a]
    assert ids("?system=ecu&state=passed") == [a]
    assert ids("?state=failed") == [b]
    assert ids("?system=nope") == []


# -- static modules ------------------------------------------------------------------

def test_the_run_page_modules_and_vendored_uplot_are_served(client):
    for path in ("inspect.js", "plot.js", "decode.js", "vtable.js", "vendor/uplot/uPlot.esm.js",
                 "vendor/uplot/uPlot.min.css", "vendor/uplot/LICENSE"):
        assert client.get(f"/static/{path}").status_code == 200, path
    assert "./vendor/uplot/uPlot.esm.js" in client.get("/static/plot.js").text
    for path in ("inspect.js", "plot.js", "decode.js", "vtable.js", "app.js", "runs.js"):
        text = client.get(f"/static/{path}").text
        assert "cdn" not in text.lower() and "https://" not in text, f"{path} loads something remote"


def test_every_fetch_goes_through_api(client):
    for path in ("inspect.js", "runs.js"):
        assert "fetch(" not in client.get(f"/static/{path}").text, path


@pytest.mark.skipif(NODE is None, reason="node not installed (the ifs-vhil-editor image has it)")
def test_js_unit_tests_under_node():
    files = sorted(str(p.relative_to(REPO)) for p in (REPO / "tests" / "js").glob("*.test.mjs"))
    assert files
    out = subprocess.run([NODE, "--test", *files], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.skipif(NODE is None, reason="node not installed (the ifs-vhil-editor image has it)")
@pytest.mark.parametrize("src", ["ecu", "ams"])
def test_the_browser_decoder_agrees_with_candef(tmp_path, src):
    """decode.js and vhil/candef.py, on the same contract and random frames
    (every field, every message of the fixture; real sources when built)."""
    sources = [FIX / src] + sorted(p.parents[4] for p in Path(os.environ.get("VHIL_FW_DIR", "/vhil/fw"))
                                   .glob(f"{src}@*/{candef.MESSAGES}/{candef.REGISTRY}"))
    rng = random.Random(1)
    cases = []
    for root in sources:
        for m in candef.load(root).values():
            for _ in range(20):
                data = bytes(rng.randrange(256) for _ in range(m.dlc))
                cases.append({"msg": m.to_json(), "data": data.hex(),
                              "expect": {k: v for k, v in m.decode(data).items()}})
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    out = subprocess.run([NODE, "tests/js/crosscheck.mjs", str(path)], cwd=REPO,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"{len(cases)} frames agree" in out.stdout
