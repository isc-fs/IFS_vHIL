"""vhil.server auth (M5.5, #117): OAuth login, enforcement, CSRF, GitHub App.

Every GitHub call goes to an httpx.MockTransport: nothing here touches the
network, and no secret in this file is real.
"""
import base64
import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402
from fastapi import Depends, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from vhil.server import auth, create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.system import REPO  # noqa: E402

SECRET = "test-session-secret-0123456789abcdef"


def github_mock(member_state="active", calls=None):
    """GitHub's OAuth + REST endpoints the callback uses."""
    def handler(req: httpx.Request):
        if calls is not None:
            calls.append(req)
        path = req.url.path
        if path == "/login/oauth/access_token":
            form = parse_qs(req.content.decode())
            if form.get("code") != ["good-code"] or not form.get("code_verifier"):
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "gho_test", "token_type": "bearer"})
        assert req.headers["authorization"] == "Bearer gho_test"
        if path == "/user":
            return httpx.Response(200, json={"login": "octo", "name": "Octo Cat",
                                             "avatar_url": "https://avatars.example/octo"})
        if path == "/user/memberships/orgs/isc-fs":
            if member_state is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"state": member_state, "role": "member"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def make_app(tmp_path, monkeypatch, mode, transport=None):
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_ID", "client-id")
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("VHIL_SESSION_SECRET", SECRET)
    monkeypatch.delenv("VHIL_PUBLIC_URL", raising=False)
    monkeypatch.delenv("VHIL_GITHUB_APP_ID", raising=False)
    settings = Settings(workspace=REPO, db=tmp_path / "vhil.db", results=tmp_path / "runs", auth=mode)
    app = create_app(settings)
    app.state.auth.transport = transport or github_mock()

    # Stand-ins for the routes other modules add later (runs, editor): the
    # guard must cover them without their doing anything.
    @app.post("/api/things")
    def make_thing(user=Depends(auth.current_user)):
        return {"by": user["login"]}

    @app.websocket("/api/live")
    async def live(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"user": ws.state.user["login"]})
        await ws.close()

    return app


@pytest.fixture
def gh(tmp_path, monkeypatch):
    return TestClient(make_app(tmp_path, monkeypatch, "github"))


@pytest.fixture
def dev(tmp_path, monkeypatch):
    return TestClient(make_app(tmp_path, monkeypatch, "dev"))


def login(client):
    r = client.get("/auth/login", params={"next": "/#/runs"}, follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    return client.get("/auth/callback", params={"code": "good-code", "state": state},
                      follow_redirects=False)


# -- login ----------------------------------------------------------------------

def test_login_redirects_to_github_with_state_and_pkce(gh):
    r = gh.get("/auth/login", follow_redirects=False)
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert (url.netloc, url.path) == ("github.com", "/login/oauth/authorize")
    q = parse_qs(url.query)
    assert q["client_id"] == ["client-id"] and q["scope"] == ["read:org"]
    assert len(q["state"][0]) >= 24 and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["http://testserver/auth/callback"]
    cookie = r.headers["set-cookie"]
    assert "vhil_oauth=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()


def test_callback_rejects_a_bad_or_missing_state(gh):
    gh.get("/auth/login", follow_redirects=False)
    r = gh.get("/auth/callback", params={"code": "good-code", "state": "forged"})
    assert r.status_code == 400
    fresh = TestClient(gh.app)       # no oauth cookie at all
    assert fresh.get("/auth/callback", params={"code": "good-code", "state": "x"}).status_code == 400


def test_callback_sends_the_pkce_verifier(tmp_path, monkeypatch):
    calls = []
    client = TestClient(make_app(tmp_path, monkeypatch, "github", github_mock(calls=calls)))
    r = client.get("/auth/login", follow_redirects=False)
    challenge = parse_qs(urlsplit(r.headers["location"]).query)["code_challenge"][0]
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    client.get("/auth/callback", params={"code": "good-code", "state": state}, follow_redirects=False)
    verifier = parse_qs(calls[0].content.decode())["code_verifier"][0]
    assert auth._b64(hashlib.sha256(verifier.encode()).digest()) == challenge


@pytest.mark.parametrize("membership", [None, "pending"])
def test_non_members_get_403(tmp_path, monkeypatch, membership):
    client = TestClient(make_app(tmp_path, monkeypatch, "github", github_mock(membership)))
    r = login(client)
    assert r.status_code == 403 and "isc-fs" in r.json()["detail"]
    assert "vhil_session" not in client.cookies


def test_members_get_a_session(gh):
    r = login(gh)
    assert r.status_code == 303 and r.headers["location"] == "/#/runs"
    assert "vhil_session" in gh.cookies and "vhil_csrf" in gh.cookies
    session_cookie = [c for c in r.headers.get_list("set-cookie") if c.startswith("vhil_session=")][0]
    assert "HttpOnly" in session_cookie and "samesite=lax" in session_cookie.lower()
    me = gh.get("/api/me").json()
    assert me["login"] == "octo" and me["name"] == "Octo Cat" and me["auth"] == "github"
    assert me["csrf"] == gh.cookies["vhil_csrf"]


def test_login_only_returns_to_this_site(gh):
    r = gh.get("/auth/login", params={"next": "//evil.example/x"}, follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = gh.get("/auth/callback", params={"code": "good-code", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_logout_clears_the_session(gh):
    login(gh)
    r = gh.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 303
    assert gh.get("/api/me").status_code == 401


# -- enforcement ------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/me", "/api/systems", "/api/catalog", "/api/systems/ams"])
def test_api_needs_a_session_in_github_mode(gh, path):
    assert gh.get(path).status_code == 401
    login(gh)
    assert gh.get(path).status_code == 200


def test_health_shell_and_static_stay_open(gh):
    assert gh.get("/api/health").status_code == 200
    assert gh.get("/").status_code == 200
    assert gh.get("/static/app.js").status_code == 200


def test_a_route_can_opt_out(gh):
    @gh.app.get("/api/open")
    def open_route():
        return {"ok": True}
    assert gh.get("/api/open").status_code == 401
    auth.allow_anonymous(gh.app, "/api/open")
    assert gh.get("/api/open").status_code == 200


def test_dev_mode_is_open_as_the_local_user(dev):
    assert dev.get("/api/me").json()["login"] == "dev"
    assert dev.get("/api/systems").status_code == 200
    assert dev.post("/api/things").json() == {"by": "dev"}
    with dev.websocket_connect("/api/live") as ws:
        assert ws.receive_json() == {"user": "dev"}


def test_websocket_needs_a_session(gh):
    with pytest.raises(WebSocketDisconnect) as e:
        with gh.websocket_connect("/api/live"):
            pass
    assert e.value.code == 4401
    login(gh)
    with gh.websocket_connect("/api/live") as ws:
        assert ws.receive_json() == {"user": "octo"}


def test_cross_site_websocket_is_refused(gh):
    login(gh)
    with pytest.raises(WebSocketDisconnect) as e:
        with gh.websocket_connect("/api/live", headers={"Origin": "https://evil.example"}):
            pass
    assert e.value.code == 4403


# -- CSRF -------------------------------------------------------------------------

def test_post_needs_the_csrf_token(gh):
    login(gh)
    assert gh.post("/api/things").status_code == 403
    assert gh.post("/api/things", headers={"X-CSRF-Token": "nope"}).status_code == 403
    r = gh.post("/api/things", headers={"X-CSRF-Token": gh.cookies["vhil_csrf"]})
    assert r.status_code == 200 and r.json() == {"by": "octo"}


def test_post_without_a_session_is_401(gh):
    assert gh.post("/api/things", headers={"X-CSRF-Token": "x"}).status_code == 401


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"},
                                     {"Sec-Fetch-Site": "cross-site"}])
def test_cross_site_posts_are_refused_in_both_modes(gh, dev, headers):
    login(gh)
    token = {"X-CSRF-Token": gh.cookies["vhil_csrf"]}
    assert gh.post("/api/things", headers={**token, **headers}).status_code == 403
    assert dev.post("/api/things", headers=headers).status_code == 403
    assert dev.post("/api/things", headers={"Origin": "http://testserver"}).status_code == 200


def test_csrf_token_of_another_session_is_refused(gh, tmp_path, monkeypatch):
    other = TestClient(make_app(tmp_path, monkeypatch, "github"))
    login(other)
    login(gh)
    r = gh.post("/api/things", headers={"X-CSRF-Token": other.cookies["vhil_csrf"]})
    assert r.status_code == 403


# -- session integrity --------------------------------------------------------------

def test_a_tampered_session_is_rejected(gh):
    login(gh)
    body, mac = gh.cookies["vhil_session"].split(".")
    payload = json.loads(auth._unb64(body))
    payload["login"] = "admin"
    forged = auth._b64(json.dumps(payload).encode())
    gh.cookies.set("vhil_session", f"{forged}.{mac}")
    assert gh.get("/api/me").status_code == 401
    gh.cookies.set("vhil_session", "garbage")
    assert gh.get("/api/me").status_code == 401


def test_a_session_signed_with_another_secret_is_rejected(gh):
    other = auth.Auth(mode="github", secret=b"x" * 32)
    cookie, _ = other.new_session({"login": "octo", "name": "o", "avatar_url": ""})
    gh.cookies.set("vhil_session", cookie)
    assert gh.get("/api/me").status_code == 401


def test_an_expired_session_is_rejected(gh, monkeypatch):
    login(gh)
    real = auth.time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + auth.SESSION_TTL_S + 1)
    assert gh.get("/api/me").status_code == 401


def test_github_mode_refuses_to_start_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_ID", "client-id")
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("VHIL_SESSION_SECRET", "short")
    settings = Settings(workspace=REPO, db=tmp_path / "db", results=tmp_path / "r", auth="github")
    with pytest.raises(ValueError, match="VHIL_SESSION_SECRET"):
        create_app(settings)


# -- GitHub App ------------------------------------------------------------------------

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from vhil.server.github_app import GitHubApp, GitHubAppError  # noqa: E402


@pytest.fixture(scope="module")
def rsa_key():
    # A throwaway key: generated per test session, never written to the repo.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def app_api(clock, issued):
    """GitHub's App endpoints; each token lives 1 h from the clock's now."""
    def handler(req: httpx.Request):
        assert req.headers["authorization"].startswith("Bearer ey")
        if req.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 7, "account": {"login": "someone-else"}},
                                             {"id": 42, "account": {"login": "isc-fs"}}])
        if req.method == "POST" and req.url.path == "/app/installations/42/access_tokens":
            issued.append(req)
            exp = datetime.fromtimestamp(clock() + 3600, timezone.utc)
            return httpx.Response(201, json={"token": f"ghs_{len(issued)}",
                                             "expires_at": exp.strftime("%Y-%m-%dT%H:%M:%SZ")})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_app_jwt_is_rs256_with_github_claims(rsa_key):
    clock = Clock()
    app = GitHubApp(12345, pem(rsa_key), "isc-fs", clock=clock)
    header, claims, sig = app.jwt().split(".")
    dec = lambda s: json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))  # noqa: E731
    assert dec(header) == {"alg": "RS256", "typ": "JWT"}
    c = dec(claims)
    assert c["iss"] == "12345"
    assert c["iat"] == int(clock.t) - 60 and c["exp"] == int(clock.t) + 540
    assert c["exp"] - c["iat"] <= 600          # GitHub's 10-minute ceiling
    rsa_key.public_key().verify(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)),
                                f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_installation_tokens_are_cached_and_refreshed(rsa_key):
    clock, issued = Clock(), []
    app = GitHubApp(1, pem(rsa_key), "isc-fs", clock=clock, transport=app_api(clock, issued))
    assert app.installation_id() == 42
    assert app.token_for("isc-fs/IFS08-CE-ECU") == "ghs_1"
    assert app.token_for("IFS08-CE-ECU") == "ghs_1"          # same repo + access: cached
    clock.t += 3600 - 301                                # still > 5 min left
    assert app.token_for("isc-fs/IFS08-CE-ECU") == "ghs_1"
    clock.t += 2                                         # inside the refresh margin
    assert app.token_for("isc-fs/IFS08-CE-ECU") == "ghs_2"
    assert len(issued) == 2


def test_tokens_are_narrowed_to_one_repo_and_the_access_asked_for(rsa_key):
    clock, issued = Clock(), []
    app = GitHubApp(1, pem(rsa_key), "isc-fs", clock=clock, transport=app_api(clock, issued))
    read = app.token_for("isc-fs/IFS08-CE-ECU")
    write = app.token_for("isc-fs/IFS_vHIL", write=True)
    assert read != write and app.token_for("IFS_vHIL") not in (read, write)
    bodies = [json.loads(r.content) for r in issued]
    assert bodies[0] == {"repositories": ["IFS08-CE-ECU"], "permissions": {"contents": "read"}}
    assert bodies[1] == {"repositories": ["IFS_vHIL"],
                         "permissions": {"contents": "write", "pull_requests": "write"}}
    assert bodies[2]["permissions"] == {"contents": "read"}


def test_token_for_refuses_repos_outside_the_org(rsa_key):
    clock = Clock()
    app = GitHubApp(1, pem(rsa_key), "isc-fs", clock=clock, transport=app_api(clock, []))
    with pytest.raises(GitHubAppError):
        app.token_for("torvalds/linux")


def test_app_not_installed_in_the_org(rsa_key):
    clock = Clock()
    app = GitHubApp(1, pem(rsa_key), "other-org", clock=clock, transport=app_api(clock, []))
    with pytest.raises(GitHubAppError, match="not installed"):
        app.token_for("other-org/x")


def test_app_from_env(rsa_key, tmp_path, monkeypatch):
    monkeypatch.delenv("VHIL_GITHUB_APP_ID", raising=False)
    assert GitHubApp.from_env("isc-fs") is None
    monkeypatch.setenv("VHIL_GITHUB_APP_ID", "99")
    monkeypatch.setenv("VHIL_GITHUB_APP_KEY", str(tmp_path / "missing.pem"))
    with pytest.raises(GitHubAppError):
        GitHubApp.from_env("isc-fs")
    (tmp_path / "app.pem").write_bytes(pem(rsa_key))
    monkeypatch.setenv("VHIL_GITHUB_APP_KEY", str(tmp_path / "app.pem"))
    assert GitHubApp.from_env("isc-fs").app_id == "99"
