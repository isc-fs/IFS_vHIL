"""Security headers on every response the app serves itself.

The shell is plain ES modules with no inline script or style (vtable.js and
plot.js set element styles through the CSSOM, which a CSP allows), so the
policy can be strict:

    default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: <avatars>;
    connect-src 'self' ws(s)://<this host>; frame-src 'self';
    frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'

plus X-Frame-Options, X-Content-Type-Options and Referrer-Policy. The editor
is on this origin, under /editor/ (the proxy routes it to Pipeline Manager:
deploy/Caddyfile), and no shell page embeds it any more: it is the vHIL
workspace, a page of its own, which the shell's Editor route redirects to
(static/editor.js). frame-src 'self' names no other origin. A response that
already carries a Content-Security-Policy keeps its own: run artifacts set
`sandbox` (vhil/server/runs.py). FastAPI's /docs and
/redoc pages load Swagger UI from a CDN with inline script, so they get only
the frame and sniffing headers.

The editor's own policy, EDITOR_CSP, is the same with no avatars or frames;
Pipeline Manager sends it (PM_CSP, scripts/editor.sh). Its validators are
precompiled and its log has no inline styles, so it needs neither
'unsafe-eval' nor 'unsafe-inline' (editor/pipeline-manager/CHANGELOG-VHIL.md).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# GitHub profile pictures in the header (auth.py's avatar_url).
AVATARS = "https://avatars.githubusercontent.com"
_HOST = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")
DOCS = ("/docs", "/redoc", "/openapi.json")


def origin_of(url: str) -> str | None:
    """scheme://host[:port] of an absolute http(s) URL, else None (a relative
    editor URL is this site, covered by 'self')."""
    p = urlsplit(url or "")
    if p.scheme not in ("http", "https") or not p.netloc or not _HOST.match(p.netloc):
        return None
    return f"{p.scheme}://{p.netloc}"


# The editor (Pipeline Manager under /editor/). connect-src 'self' covers its
# same-origin socket.io WebSocket in current browsers.
EDITOR_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "worker-src 'self'",
    "frame-src 'none'",
    "frame-ancestors 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])


def csp(ws_origins: list[str]) -> str:
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        f"img-src 'self' data: {AVATARS}",
        "font-src 'self'",
        "connect-src " + " ".join(["'self'"] + ws_origins),
        "frame-src 'self'",
        "frame-ancestors 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ])


class SecurityHeaders:
    """Pure ASGI middleware: adds the headers to every HTTP response."""

    def __init__(self, app, public_url: str = ""):
        self.app = app
        self.public = origin_of(public_url)

    def _ws_origins(self, scope) -> list[str]:
        # 'self' covers same-origin ws(s) in current browsers; name it too for
        # the ones that read 'self' as http(s) only.
        if self.public:
            p = urlsplit(self.public)
            return [f"{'wss' if p.scheme == 'https' else 'ws'}://{p.netloc}"]
        host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
        if not _HOST.match(host):
            return []
        return [f"ws://{host}", f"wss://{host}"]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        docs = path in DOCS or path.startswith("/docs/")
        policy = None if docs else csp(self._ws_origins(scope))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {k.lower() for k, _ in headers}
                extra = [(b"x-frame-options", b"SAMEORIGIN"),
                         (b"x-content-type-options", b"nosniff"),
                         (b"referrer-policy", b"same-origin")]
                if policy is not None:
                    extra.append((b"content-security-policy", policy.encode()))
                else:
                    extra.append((b"content-security-policy", b"frame-ancestors 'self'"))
                headers += [(k, v) for k, v in extra if k not in have]
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_with_headers)
