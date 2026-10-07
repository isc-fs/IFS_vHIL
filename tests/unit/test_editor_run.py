"""The editor's Run is a normal run (step 5 of
docs/architecture/editor-workspace.md): the request the Editor page builds
(vhil/server/static/editor-run.js, under node) from a system's graph is one
POST /api/runs takes, and the editor backend runs nothing itself. The
request builder's own cases are tests/js/editor_run.test.mjs."""
import json
import os
import shutil
import subprocess

import pytest
import yaml

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil.editor import (EditorMethods, _source, from_dataflow, to_dataflow,  # noqa: E402
                         write_system)
from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.server.runs import DEFAULT_VIRTUAL_MS, RunStore  # noqa: E402
from vhil.system import REPO  # noqa: E402

NODE = shutil.which("node") or next((p for p in ("/opt/node/bin/node",) if os.path.exists(p)), None)
EDITOR_RUN = REPO / "vhil" / "server" / "static" / "editor-run.js"
WORKSPACE = REPO / "editor/pipeline-manager/pipeline_manager/frontend/src/vhil/workspace.js"


@pytest.fixture
def settings(tmp_path):
    return Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def graph(system: str) -> dict:
    text = (REPO / "systems" / f"{system}.yaml").read_text()
    return to_dataflow(yaml.safe_load(text), source=text)


def set_prop(dataflow: dict, board: str, name: str, value: str) -> None:
    node = next(n for n in dataflow["graphs"][0]["nodes"] if n["instanceName"] == board)
    next(p for p in node["properties"] if p["name"] == name)["value"] = value


def run_request(tmp_path, dataflow: dict, **kw) -> dict:
    """editor-run.js's runRequest on `dataflow`, under node."""
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(dataflow))
    args = {"system": dataflow["graphs"][0]["name"], "ref": "", "virtualMs": DEFAULT_VIRTUAL_MS, **kw}
    script = (f"import {{ runRequest }} from {json.dumps(EDITOR_RUN.as_uri())};\n"
              f"import {{ readFileSync }} from 'node:fs';\n"
              f"const dataflow = JSON.parse(readFileSync({json.dumps(str(path))}, 'utf8'));\n"
              f"console.log(JSON.stringify(runRequest({{ ...{json.dumps(args)}, dataflow }})));")
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed (the ifs-vhil-editor image has it)")
@pytest.mark.parametrize("system", ["ecu", "ams", "ecu-ams"])
def test_the_editors_run_request_is_queued_as_a_normal_run(tmp_path, client, settings, system):
    dataflow = graph(system)
    body = run_request(tmp_path, dataflow)
    assert body == {"system": system, "firmware": {},
                    "scenario": {"kind": "run", "virtual_ms": DEFAULT_VIRTUAL_MS}}
    r = client.post("/api/runs", json=body)
    assert r.status_code == 201, r.text
    run = RunStore(settings.db).get(r.json()["run_id"])
    assert run["system"] == system and run["state"] == "queued"
    assert run["scenario"]["virtual_ms"] == DEFAULT_VIRTUAL_MS
    # It shows in the history the Runs page lists.
    assert r.json()["run_id"] in [x["id"] for x in client.get("/api/runs").json()]


@pytest.mark.skipif(NODE is None, reason="node not installed (the ifs-vhil-editor image has it)")
def test_the_graphs_firmware_refs_reach_the_run(tmp_path, client, settings):
    dataflow = graph("ecu-ams")
    set_prop(dataflow, "ecu", "firmware_ref", "feat/x")
    set_prop(dataflow, "ams", "bootloader_ref", "v1.6.2")
    body = run_request(tmp_path, dataflow, virtualMs=4000)
    assert body["firmware"] == {"ecu": "feat/x", "ams.bootloader": "v1.6.2"}
    r = client.post("/api/runs", json=body)
    assert r.status_code == 201, r.text
    run = RunStore(settings.db).get(r.json()["run_id"])
    assert run["firmware"] == {"ecu": "feat/x", "ams.bootloader": "v1.6.2"}
    assert run["scenario"]["virtual_ms"] == 4000


@pytest.mark.skipif(NODE is None, reason="node not installed (the ifs-vhil-editor image has it)")
def test_a_run_at_a_ref_the_workspace_lacks_is_refused(tmp_path, client):
    body = run_request(tmp_path, graph("ams"), ref="no-such-branch")
    r = client.post("/api/runs", json=body)
    assert r.status_code == 422 and "no-such-branch" in r.text


def test_an_edit_shows_in_the_preview_run_compares():
    """Run refuses while the graph's preview (POST /api/systems/{id}/preview:
    write_system of from_dataflow) differs from the one it had when opened
    or saved: an unedited graph previews as its file, an edit differently."""
    def preview(dataflow):
        return write_system(from_dataflow(dataflow), _source(dataflow))

    text = (REPO / "systems" / "ams.yaml").read_text()
    dataflow = graph("ams")
    first = preview(dataflow)
    assert first == text
    set_prop(dataflow, "ams", "firmware_ref", "feat/x")
    edited = preview(dataflow)
    assert edited != first and "firmware_ref: feat/x" in edited


def test_a_run_defaults_to_three_seconds_from_one_place(client):
    """Every run counts from power-on through the bootloader's 2 s window."""
    assert DEFAULT_VIRTUAL_MS >= 3000
    assert client.get("/api/config").json()["run_virtual_ms"] == DEFAULT_VIRTUAL_MS
    r = client.post("/api/runs", json={"system": "ecu", "scenario": {"kind": "run"}})
    assert r.status_code == 201, r.text
    assert client.get(f"/api/runs/{r.json()['run_id']}").json()["scenario"]["virtual_ms"] \
        == DEFAULT_VIRTUAL_MS
    # The pages take it from /api/config, not a number of their own: the
    # Runs page and the editor workspace (which the Editor route redirects to).
    assert "run_virtual_ms" in client.get("/static/runs.js").text
    assert "config.run_virtual_ms" in WORKSPACE.read_text()


def test_the_editor_backend_runs_nothing():
    """One run path: no in-process run on $VHIL_*_ELF beside /api/runs."""
    rpc = EditorMethods()
    assert not hasattr(rpc, "dataflow_run")
    assert all(item["procedureName"] != "dataflow_run" for item in rpc.app_capabilities_get())
