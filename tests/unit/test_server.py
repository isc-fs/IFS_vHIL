"""vhil.server (M5.1): the API over this checkout's systems and catalogue."""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.system import REPO  # noqa: E402


@pytest.fixture
def client(tmp_path):
    settings = Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")
    return TestClient(create_app(settings))


def test_health(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["auth"] == "dev"


def test_catalog_lists_boards_and_models(client):
    cat = client.get("/api/catalog").json()
    assert "mlc-carrier" in {b["id"] for b in cat["boards"]}
    assert {"ltc6811", "sd-card"} <= {m["id"] for m in cat["models"]}


def test_systems_lists_the_checkout(client):
    systems = {s["id"]: s for s in client.get("/api/systems").json()}
    assert {"ams", "ecu", "ecu-ams"} <= set(systems)
    assert systems["ecu-ams"]["boards"] == ["ams", "ecu"]


def test_a_system_comes_back_as_file_and_document(client):
    s = client.get("/api/systems/ams").json()
    assert s["path"] == "systems/ams.yaml" and s["errors"] == []
    assert s["doc"]["boards"]["ams"]["board"] == "mlc-carrier"
    assert s["yaml"] == (REPO / "systems" / "ams.yaml").read_text()


@pytest.mark.parametrize("bad", ["nope", "..%2Fpyproject", "AMS", "a" * 80])
def test_unknown_or_unsafe_ids_are_404(client, bad):
    assert client.get(f"/api/systems/{bad}").status_code == 404


def test_the_shell_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "IFS vHIL" in r.text
    assert client.get("/static/app.js").status_code == 200
