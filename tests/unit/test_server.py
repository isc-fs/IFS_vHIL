"""vhil.server (M5.1): the API over this checkout's systems and catalogue."""
import hashlib
import logging
import re
from dataclasses import replace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings, check_dev_bind, is_loopback  # noqa: E402
from vhil.server.security import EDITOR_CSP  # noqa: E402
from vhil.system import REPO  # noqa: E402


@pytest.fixture
def client(tmp_path):
    settings = Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth="dev")
    return TestClient(create_app(settings))


def test_health(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["auth"] == "dev" and h["contract"] == 1


def test_catalog_lists_boards_and_models(client):
    cat = client.get("/api/catalog").json()
    assert "mainlite" in {b["id"] for b in cat["boards"]}
    assert {"ltc6811", "sd-card"} <= {m["id"] for m in cat["models"]}


def test_systems_lists_the_checkout(client):
    systems = {s["id"]: s for s in client.get("/api/systems").json()}
    assert {"ams", "ecu", "ecu-ams"} <= set(systems)
    assert systems["ecu-ams"]["boards"] == ["ams", "ecu"]


def test_a_system_comes_back_as_file_and_document(client):
    s = client.get("/api/systems/ams").json()
    assert s["path"] == "systems/ams.yaml" and s["errors"] == []
    assert s["doc"]["boards"]["ams"]["board"] == "mainlite"
    assert s["yaml"] == (REPO / "systems" / "ams.yaml").read_text()


@pytest.mark.parametrize("bad", ["nope", "..%2Fpyproject", "AMS", "a" * 80])
def test_unknown_or_unsafe_ids_are_404(client, bad):
    assert client.get(f"/api/systems/{bad}").status_code == 404


def test_the_shell_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "IFS vHIL" in r.text
    assert client.get("/static/app.js").status_code == 200


# -- auth fails closed --------------------------------------------------------------------

def test_auth_defaults_to_github_when_unset(monkeypatch):
    monkeypatch.delenv("VHIL_AUTH", raising=False)
    assert Settings.from_env().auth == "github"
    monkeypatch.setenv("VHIL_AUTH", "")
    assert Settings.from_env().auth == "github"
    monkeypatch.setenv("VHIL_AUTH", "dev")
    assert Settings.from_env().auth == "dev"


def test_unset_auth_without_github_settings_refuses_to_start(monkeypatch, tmp_path):
    for v in ("VHIL_AUTH", "VHIL_GITHUB_CLIENT_ID", "VHIL_GITHUB_CLIENT_SECRET",
              "VHIL_SESSION_SECRET"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("VHIL_DATA", str(tmp_path))
    with pytest.raises(ValueError, match="VHIL_GITHUB_CLIENT_ID"):
        create_app()


def test_admins_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("VHIL_ADMINS", " Alice, bob ,,")
    s = Settings.from_env()
    assert s.admins == {"alice", "bob"} and s.is_admin("ALICE") and not s.is_admin("carol")
    assert not s.is_admin("") and not s.is_admin(None)


@pytest.mark.parametrize("host, ok", [("127.0.0.1", True), ("localhost", True), ("::1", True),
                                      ("[::1]", True), ("127.0.0.2", True), ("0.0.0.0", False),
                                      ("::", False), ("192.168.1.5", False), ("example.org", False)])
def test_dev_mode_only_on_loopback(monkeypatch, tmp_path, host, ok):
    monkeypatch.delenv("VHIL_ALLOW_DEV_ON_NETWORK", raising=False)
    dev = Settings(workspace=REPO, db=tmp_path / "db", results=tmp_path / "r", auth="dev")
    assert is_loopback(host) is ok
    if ok:
        check_dev_bind(dev, host)
    else:
        with pytest.raises(SystemExit, match="refusing VHIL_AUTH=dev"):
            check_dev_bind(dev, host)
        monkeypatch.setenv("VHIL_ALLOW_DEV_ON_NETWORK", "1")
        check_dev_bind(dev, host)               # explicit override: allowed, with a warning
    check_dev_bind(replace(dev, auth="github"), "0.0.0.0")   # github mode binds anywhere


def test_dev_mode_start_logs_a_warning(caplog, tmp_path):
    dev = Settings(workspace=REPO, db=tmp_path / "db", results=tmp_path / "r", auth="dev")
    with caplog.at_level(logging.WARNING, logger="vhil.server"):
        check_dev_bind(dev, "127.0.0.1")
    assert "NO LOGIN" in caplog.text


# -- security headers ---------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/static/app.js", "/api/health", "/api/systems",
                                  "/api/runs/999"])
def test_every_response_carries_the_security_headers(client, path):
    h = client.get(path).headers
    csp = dict(d.strip().split(" ", 1) for d in h["content-security-policy"].split(";"))
    assert csp["default-src"] == "'self'" and csp["script-src"] == "'self'"
    assert csp["style-src"] == "'self'" and csp["frame-ancestors"] == "'self'"
    assert csp["object-src"] == "'none'" and csp["base-uri"] == "'none'"
    assert "'self'" in csp["connect-src"] and "ws://testserver" in csp["connect-src"]
    assert "unsafe" not in h["content-security-policy"]
    assert h["x-frame-options"] == "SAMEORIGIN"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "same-origin"


def test_the_csp_frames_only_this_origin_and_names_the_public_wss(monkeypatch, tmp_path):
    """The editor is same-origin (/editor/): no other origin may be framed,
    whatever VHIL_EDITOR_URL a deployment still sets."""
    monkeypatch.setenv("VHIL_EDITOR_URL", "https://editor.example.org:5443/x/")
    monkeypatch.setenv("VHIL_AUTH", "dev")
    monkeypatch.setenv("VHIL_PUBLIC_URL", "https://vhil.example.org")
    settings = Settings(workspace=REPO, db=tmp_path / "db", results=tmp_path / "r", auth="dev")
    csp = TestClient(create_app(settings)).get("/").headers["content-security-policy"]
    assert "frame-src 'self';" in csp and "editor.example.org" not in csp
    assert "connect-src 'self' wss://vhil.example.org;" in csp


def test_the_editors_csp_needs_no_unsafe_source():
    csp = dict(d.split(" ", 1) for d in EDITOR_CSP.split("; "))
    for directive in ("default-src", "script-src", "style-src", "connect-src", "font-src"):
        assert csp[directive] == "'self'", directive
    assert csp["frame-ancestors"] == "'self'" and csp["object-src"] == "'none'"
    assert "unsafe" not in EDITOR_CSP and "*" not in EDITOR_CSP


def test_a_hostile_host_header_stays_out_of_the_csp(client):
    csp = client.get("/", headers={"host": "x; script-src *"}).headers["content-security-policy"]
    assert "script-src *" not in csp


def test_the_guards_refusals_carry_the_headers_too(client):
    r = client.post("/api/runs", json={}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and r.headers["x-frame-options"] == "SAMEORIGIN"


STATIC = REPO / "vhil" / "server" / "static"


@pytest.mark.parametrize("path", sorted(p.relative_to(STATIC).as_posix()
                                        for p in STATIC.glob("*") if p.suffix in (".js", ".html")))
def test_the_shell_has_no_inline_script_or_style(path):
    """What the CSP would refuse: inline <script>, style="" attributes and
    on*= handlers in markup (el.style / addEventListener are fine)."""
    text = (STATIC / path).read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), "inline <script>"
    assert not re.search(r"""\sstyle\s*=\s*["'`$]""", text), "style attribute in markup"
    assert not re.search(r"""<[a-z][^>]*\son[a-z]+\s*=\s*["']""", text, re.I), "on*= handler"
    assert "eval(" not in text and "new Function" not in text


def test_the_stylesheets_load_nothing_remote():
    """font-src and style-src are 'self': every url() and @import is a file
    of the shell's own, and the fonts are what fonts/SHA256SUMS pins."""
    for css in STATIC.glob("*.css"):
        text = css.read_text()
        assert "@import" not in text, css.name
        for url in re.findall(r"""url\(\s*["']?([^"')]+)""", text):
            assert "//" not in url and (css.parent / url).is_file(), f"{css.name}: {url}"
    fonts = STATIC / "fonts"
    for line in (fonts / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((fonts / name).read_bytes()).hexdigest() == digest, name


def test_fonts_are_served_as_fonts(client):
    r = client.get("/static/fonts/Inter-Regular.woff2")
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2"


def test_vendored_uplot_needs_no_eval_or_inline_markup():
    text = (STATIC / "vendor" / "uplot" / "uPlot.esm.js").read_text()
    assert "eval(" not in text and "new Function" not in text
    assert "innerHTML" not in text and "setAttribute(\"style\"" not in text
