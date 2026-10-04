"""Login and access control for the web app (M5.5, #117).

Two modes, from Settings.auth (VHIL_AUTH):

- "dev": no login. Every request runs as DEV_USER. For a local checkout only.
- "github": GitHub OAuth web flow, restricted to members of VHIL_GITHUB_ORG
  (default isc-fs). The session is a signed cookie (HMAC-SHA256, stdlib), so
  the server keeps no session store.

Enforcement is one ASGI middleware over the whole app, so routes other modules
add are covered without doing anything:

- every HTTP request under /api/ and every WebSocket needs a session, except
  the paths in `Auth.public` (by default /api/health). A route opts out with
  `auth.allow_anonymous(app, "/api/thing")` (exact path, or a prefix ending
  in "/"). Pages and /static stay open: the shell loads, calls /api/me, and
  goes to /auth/login on a 401.
- mutating requests (POST/PUT/PATCH/DELETE) under /api/ must come from this
  site: a cross-site Origin / Sec-Fetch-Site is refused in both modes, and in
  github mode they also carry X-CSRF-Token, a token bound to the session
  (served in the vhil_csrf cookie and by /api/me; static/app.js sends it).

A route reads the user with `Depends(current_user)`: {"login", "name",
"avatar_url"}. WebSockets are checked the same way at the handshake (closed
with 4401 / 4403 before accept).

The GitHub App (vhil.server.github_app) is wired here too: app.state.github_app
is a GitHubApp when VHIL_GITHUB_APP_ID is set, else None.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection

from vhil.server.config import env_secret
from vhil.server.github_app import GitHubApp

SESSION_COOKIE = "vhil_session"
CSRF_COOKIE = "vhil_csrf"
OAUTH_COOKIE = "vhil_oauth"
CSRF_HEADER = "x-csrf-token"
SESSION_TTL_S = 12 * 3600       # re-check org membership at least this often
OAUTH_TTL_S = 600               # the round trip to github.com must finish in this
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
DEV_USER = {"login": "dev", "name": "Local developer", "avatar_url": ""}

GITHUB = "https://github.com"
GITHUB_API = "https://api.github.com"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class Auth:
    mode: str                               # "dev" | "github"
    org: str = "isc-fs"
    client_id: str = ""
    client_secret: str = ""
    secret: bytes = b""                     # signs sessions, OAuth state, CSRF tokens
    public_url: str = ""                    # e.g. https://vhil.example.org (no trailing /)
    public: set[str] = field(default_factory=lambda: {"/api/health"})
    # Tests swap in an httpx.MockTransport; None = the real network.
    transport: httpx.BaseTransport | None = None
    github: str = GITHUB
    github_api: str = GITHUB_API

    @classmethod
    def from_env(cls, mode: str) -> "Auth":
        if mode not in ("dev", "github"):
            raise ValueError(f"VHIL_AUTH must be 'dev' or 'github', not {mode!r}")
        a = cls(mode=mode,
                org=os.environ.get("VHIL_GITHUB_ORG", "isc-fs"),
                client_id=os.environ.get("VHIL_GITHUB_CLIENT_ID", ""),
                # Each also as <name>_FILE (a compose secret), config.env_secret.
                client_secret=env_secret("VHIL_GITHUB_CLIENT_SECRET"),
                secret=env_secret("VHIL_SESSION_SECRET").encode(),
                public_url=os.environ.get("VHIL_PUBLIC_URL", "").rstrip("/"))
        if mode == "github":
            missing = [n for n, v in (("VHIL_GITHUB_CLIENT_ID", a.client_id),
                                      ("VHIL_GITHUB_CLIENT_SECRET", a.client_secret)) if not v]
            if missing:
                raise ValueError(f"VHIL_AUTH=github needs {', '.join(missing)} (or <name>_FILE)")
            if len(a.secret) < 32:
                raise ValueError("VHIL_AUTH=github needs VHIL_SESSION_SECRET "
                                 "(or VHIL_SESSION_SECRET_FILE), >= 32 chars")
        else:
            a.secret = a.secret or secrets.token_bytes(32)
        return a

    # -- signed values ---------------------------------------------------------

    def sign(self, purpose: str, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        mac = hmac.new(self.secret, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        return f"{body}.{_b64(mac)}"

    def unsign(self, purpose: str, value: str | None, max_age: int) -> dict | None:
        """The payload of a value from sign(), or None if forged, mangled or stale."""
        if not value or value.count(".") != 1:
            return None
        body, mac = value.split(".")
        want = hmac.new(self.secret, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_unb64(mac), want):
                return None
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or time.time() - payload.get("iat", 0) > max_age:
            return None
        return payload

    def new_session(self, user: dict) -> tuple[str, dict]:
        """(cookie value, session payload) for a user who just logged in."""
        payload = {**user, "iat": int(time.time()), "sid": secrets.token_urlsafe(16)}
        return self.sign("session", payload), payload

    def session(self, conn: HTTPConnection) -> dict | None:
        return self.unsign("session", conn.cookies.get(SESSION_COOKIE), SESSION_TTL_S)

    def csrf_token(self, session: dict) -> str:
        return _b64(hmac.new(self.secret, f"csrf.{session['sid']}".encode(), hashlib.sha256).digest())

    # -- origin ------------------------------------------------------------------

    def same_origin(self, conn: HTTPConnection) -> bool:
        """False for a request a browser marked as coming from another site."""
        if conn.headers.get("sec-fetch-site") == "cross-site":
            return False
        origin = conn.headers.get("origin")
        if not origin:
            return True             # not a browser cross-origin request
        if self.public_url:
            p = urlsplit(self.public_url)
            return origin == f"{p.scheme}://{p.netloc}"
        return urlsplit(origin).netloc == conn.headers.get("host")

    def is_public(self, path: str) -> bool:
        return any(path == p or (p.endswith("/") and path.startswith(p)) for p in self.public)


def allow_anonymous(app: FastAPI, *paths: str) -> None:
    """Exempt paths from the session check ("/api/x" exact, "/api/x/" prefix)."""
    app.state.auth.public.update(paths)


def current_user(conn: HTTPConnection) -> dict:
    """FastAPI dependency: the logged-in user (DEV_USER in dev mode)."""
    user = conn.scope.get("state", {}).get("user")
    if user is None:
        raise HTTPException(401, "login required")
    return user


class AuthMiddleware:
    """Pure ASGI, so it sees WebSocket handshakes as well as HTTP requests."""

    def __init__(self, app, auth: Auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        auth, path, ws = self.auth, scope["path"], scope["type"] == "websocket"
        guarded = ws or path.startswith("/api/")
        method = scope.get("method", "GET")

        session = None
        if auth.mode == "dev":
            user = DEV_USER
        else:
            session = auth.session(conn)
            user = {k: session[k] for k in DEV_USER} if session else None
        scope.setdefault("state", {})["user"] = user

        if not guarded:
            return await self.app(scope, receive, send)
        if (ws or method in MUTATING) and not auth.same_origin(conn):
            return await self._deny(scope, receive, send, 403, "cross-site request refused")
        if auth.is_public(path):
            return await self.app(scope, receive, send)
        if user is None:
            return await self._deny(scope, receive, send, 401, "login required")
        if session is not None and method in MUTATING and not ws:
            token = conn.headers.get(CSRF_HEADER, "")
            if not hmac.compare_digest(token, auth.csrf_token(session)):
                return await self._deny(scope, receive, send, 403, "missing or bad CSRF token")
        return await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, receive, send, status: int, detail: str):
        if scope["type"] == "websocket":
            # Closing before accept fails the handshake (HTTP 403 to the browser).
            await send({"type": "websocket.close", "code": 4000 + status, "reason": detail})
        else:
            await JSONResponse({"detail": detail}, status)(scope, receive, send)


def _router(auth: Auth) -> APIRouter:
    r = APIRouter()

    def base_url(request: Request) -> str:
        return auth.public_url or str(request.base_url).rstrip("/")

    def set_cookie(resp, name, value, max_age, request, http_only=True):
        resp.set_cookie(name, value, max_age=max_age, httponly=http_only, samesite="lax",
                        secure=base_url(request).startswith("https://"), path="/")

    def safe_next(n: str | None) -> str:
        # Only paths on this site: no scheme, no //host, no backslash tricks.
        return n if n and n.startswith("/") and not n.startswith(("//", "/\\")) else "/"

    @r.get("/api/me")
    def me(request: Request):
        user = current_user(request)
        out = dict(user, auth=auth.mode)
        if (s := auth.session(request)) is not None:
            out["csrf"] = auth.csrf_token(s)
        return out

    @r.get("/auth/login")
    def login(request: Request, next: str = "/"):
        if auth.mode == "dev":
            return RedirectResponse(safe_next(next), 303)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        query = urlencode({"client_id": auth.client_id, "state": state, "scope": "read:org",
                           "redirect_uri": f"{base_url(request)}/auth/callback",
                           "code_challenge": challenge, "code_challenge_method": "S256",
                           "allow_signup": "false"})
        resp = RedirectResponse(f"{auth.github}/login/oauth/authorize?{query}", 303)
        set_cookie(resp, OAUTH_COOKIE,
                   auth.sign("oauth", {"state": state, "verifier": verifier,
                                       "next": safe_next(next), "iat": int(time.time())}),
                   OAUTH_TTL_S, request)
        return resp

    @r.get("/auth/callback")
    def callback(request: Request, code: str = "", state: str = ""):
        if auth.mode == "dev":
            return RedirectResponse("/", 303)
        pending = auth.unsign("oauth", request.cookies.get(OAUTH_COOKIE), OAUTH_TTL_S)
        if not pending or not code or not hmac.compare_digest(state, pending["state"]):
            raise HTTPException(400, "login expired or state mismatch: start again at /auth/login")
        with httpx.Client(transport=auth.transport, timeout=15,
                          headers={"Accept": "application/json"}) as c:
            t = c.post(f"{auth.github}/login/oauth/access_token",
                       data={"client_id": auth.client_id, "client_secret": auth.client_secret,
                             "code": code, "code_verifier": pending["verifier"],
                             "redirect_uri": f"{base_url(request)}/auth/callback"})
            token = t.json().get("access_token") if t.status_code == 200 else None
            if not token:
                raise HTTPException(400, "GitHub refused the login code")
            h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"}
            u = c.get(f"{auth.github_api}/user", headers=h)
            if u.status_code != 200:
                raise HTTPException(502, "GitHub /user failed")
            gh = u.json()
            m = c.get(f"{auth.github_api}/user/memberships/orgs/{auth.org}", headers=h)
        # The user's token is used for this check only and never stored.
        if m.status_code != 200 or m.json().get("state") != "active":
            raise HTTPException(403, f"{gh.get('login')} is not a member of {auth.org}")
        user = {"login": gh["login"], "name": gh.get("name") or gh["login"],
                "avatar_url": gh.get("avatar_url", "")}
        cookie, session = auth.new_session(user)
        resp = RedirectResponse(pending["next"], 303)
        set_cookie(resp, SESSION_COOKIE, cookie, SESSION_TTL_S, request)
        set_cookie(resp, CSRF_COOKIE, auth.csrf_token(session), SESSION_TTL_S, request,
                   http_only=False)
        resp.delete_cookie(OAUTH_COOKIE, path="/")
        return resp

    @r.get("/auth/logout")
    def logout():
        resp = RedirectResponse("/?logged_out=1", 303)
        for name in (SESSION_COOKIE, CSRF_COOKIE, OAUTH_COOKIE):
            resp.delete_cookie(name, path="/")
        return resp

    return r


def install(app: FastAPI, settings) -> Auth:
    """Wire login, enforcement and the GitHub App into `app` (call once, in create_app)."""
    auth = Auth.from_env(settings.auth)
    app.state.auth = auth
    app.state.github_app = GitHubApp.from_env(auth.org)
    app.include_router(_router(auth))
    app.add_middleware(AuthMiddleware, auth=auth)
    return auth
