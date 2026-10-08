"""The Debug tab's firmware sources (vhil/server/sources.py; docs/debugger.md):
read from the board's image's checkout in the fw volume, and nothing else,
however the (worker-written) checkout is laid out."""
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil.server import create_app  # noqa: E402
from vhil.server import sources  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.server.runs import RunStore  # noqa: E402
from vhil.server.sources import SourceError, checkout_of, read_source  # noqa: E402
from vhil.system import REPO  # noqa: E402


@pytest.fixture
def fw(tmp_path):
    root = tmp_path / "fw"
    co = root / "ecu@dev"
    (co / "Core/Src/app").mkdir(parents=True)
    (co / "build").mkdir()
    (co / "build/ECU08.elf").write_bytes(b"\x7fELF")
    (co / "Core/Src/app/control.cpp").write_text("int a;\nvoid step() {}\n")
    (co / "notes.txt").write_text("not a source")
    secret = tmp_path / "secret.c"
    secret.write_text("the API's secret")
    os.symlink(secret, co / "Core/Src/evil.c")
    os.symlink(tmp_path, co / "Core/Src/up")
    (root / "other@main").mkdir()
    (root / "other@main/x.c").write_text("another checkout")
    return root


def test_a_source_is_read_from_the_images_checkout(fw):
    root = checkout_of(fw / "ecu@dev/build/ECU08.elf", fw)
    assert root.name == "ecu@dev"
    rel, lines = read_source(root, str(fw.resolve() / "ecu@dev/Core/Src/app/control.cpp"))
    assert rel == "Core/Src/app/control.cpp" and lines == ["int a;", "void step() {}"]
    assert read_source(root, "Core/Src/app/control.cpp")[1][1] == "void step() {}"


@pytest.mark.parametrize("path,status", [
    ("Core/Src/evil.c", 404),            # a symlink out of the checkout
    ("Core/Src/up/secret.c", 404),       # through a symlinked directory
    ("../other@main/x.c", 422),
    ("notes.txt", 422),
    ("/etc/passwd", 404),
    ("Core/Src/app/missing.c", 404),
    ("", 422),
])
def test_nothing_else_is(fw, path, status):
    root = checkout_of(fw / "ecu@dev/build/ECU08.elf", fw)
    with pytest.raises(SourceError) as e:
        read_source(root, path)
    assert e.value.status == status, e.value.detail


def test_an_image_outside_the_fw_directory_has_no_sources(fw, tmp_path):
    with pytest.raises(SourceError):
        checkout_of(tmp_path / "elsewhere/ECU08.elf", fw)


def test_the_endpoint_finds_the_runs_image(fw, tmp_path, monkeypatch):
    settings = Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs",
                        auth="dev")
    elf = fw / "ecu@dev/build/ECU08.elf"
    monkeypatch.setattr(sources, "board_elfs", lambda run, system, fw_dir: {"ecu": elf})
    app = create_app(settings)
    app.state.fw_dir = fw
    client = TestClient(app)
    run_id = RunStore(settings.db).create("ecu", "", {}, {"kind": "run", "live": True})
    r = client.get(f"/api/runs/{run_id}/debug/source",
                   params={"board": "ecu", "path": "Core/Src/app/control.cpp"})
    assert r.status_code == 200, r.text
    assert r.json() == {"board": "ecu", "root": "ecu@dev", "path": "Core/Src/app/control.cpp",
                        "lines": ["int a;", "void step() {}"]}
    assert client.get(f"/api/runs/{run_id}/debug/source",
                      params={"board": "ecu", "path": "Core/Src/evil.c"}).status_code == 404
    assert client.get(f"/api/runs/{run_id}/debug/source",
                      params={"board": "nope", "path": "a.c"}).status_code == 404
    assert client.get("/api/runs/999/debug/source",
                      params={"board": "ecu", "path": "a.c"}).status_code == 404
