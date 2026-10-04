"""Security headers on every response the app serves itself.

The shell is plain ES modules with no inline script or style (vtable.js and
plot.js set element styles through the CSSOM, which a CSP allows), so the
policy can be strict:

    default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: <avatars>;
    connect-src 'self' ws(s)://<this host>; frame-src 'self' <editor origin>;
    frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'

plus X-Frame-Options, X-Content-Type-Options and Referrer-Policy. The editor
origin comes from VHIL_EDITOR_URL (the iframe the Editor page embeds). A
response that already carries a Content-Security-Policy keeps its own: run
artifacts set `sandbox` (vhil/server/runs.py). FastAPI's /docs and /redoc
pages load Swagger UI from a CDN with inline script, so they get only the
frame and sniffing headers.
"""
from __future__ import annotations

import os
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


def csp(ws_origins: list[str], editor_origin: str | None) -> str:
    frames = " ".join(["'self'"] + ([editor_origin] if editor_origin else []))
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        f"img-src 'self' data: {AVATARS}",
        "font-src 'self'",
        "connect-src " + " ".join(["'self'"] + ws_origins),
        f"frame-src {frames}",
        "frame-ancestors 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ])


class SecurityHeaders:
    """Pure ASGI middleware: adds the headers to every HTTP response."""

    def __init__(self, app, public_url: str = "", editor_url: str | None = None):
        self.app = app
        self.public = origin_of(public_url)
        self.editor = origin_of(editor_url if editor_url is not None
                                else os.environ.get("VHIL_EDITOR_URL", "http://localhost:5050"))

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
        policy = None if docs else csp(self._ws_origins(scope), self.editor)

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
