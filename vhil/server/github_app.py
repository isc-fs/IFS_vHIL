"""GitHub App credentials (M5.5, #117): installation tokens for the org.

The web app never uses a person's token for git. Firmware clones and pushes of
system-file branches / PRs go through the isc-fs GitHub App instead:

    app = GitHubApp.from_env("isc-fs")                 # None when not configured
    app.token_for("isc-fs/IFS08-CE-ECU")                    # clone: contents read
    app.token_for("isc-fs/IFS_vHIL", write=True)       # push a branch, open a PR

The App authenticates as itself with a short-lived RS256 JWT (app id + the
private key file GitHub generated), looks up its installation in the org, and
exchanges the JWT for an installation token (1 h) narrowed to one repository
and the access asked for. Tokens are cached per (repo, write) and refreshed
shortly before they expire. Configuration: VHIL_GITHUB_APP_ID and
VHIL_GITHUB_APP_KEY (path to the .pem); setup in docs/development/web-app.md.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

API = "https://api.github.com"
# GitHub rejects app JWTs that live longer than 10 min; iat is backdated to
# absorb clock drift between this host and GitHub.
JWT_LIFETIME_S = 540
JWT_BACKDATE_S = 60
# Refresh an installation token this long before GitHub expires it, so a clone
# that starts with it doesn't die halfway.
TOKEN_MARGIN_S = 300


class GitHubAppError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_key(pem: bytes):
    # Imported lazily: dev mode and the rest of the server don't need it.
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(pem, password=None)


class GitHubApp:
    def __init__(self, app_id: str | int, private_key_pem: bytes, org: str, *,
                 api: str = API, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time):
        self.app_id = str(app_id)
        self.org = org
        self.api = api.rstrip("/")
        self.transport = transport
        self.clock = clock
        self._key = _load_key(private_key_pem)
        self._lock = threading.Lock()
        self._installation: int | None = None
        # (repo, write) -> (token, expires_at epoch)
        self._tokens: dict[tuple[str, bool], tuple[str, float]] = {}

    @classmethod
    def from_env(cls, org: str) -> "GitHubApp | None":
        app_id = os.environ.get("VHIL_GITHUB_APP_ID")
        if not app_id:
            return None
        key = os.environ.get("VHIL_GITHUB_APP_KEY")
        if not key or not Path(key).is_file():
            raise GitHubAppError("VHIL_GITHUB_APP_ID is set but VHIL_GITHUB_APP_KEY "
                                 f"is not a readable private key file ({key!r})")
        return cls(app_id, Path(key).read_bytes(), org)

    # -- the App's own identity ----------------------------------------------

    def jwt(self) -> str:
        """An RS256 JWT for the App itself (GitHub: 'Authenticating as a GitHub App')."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        now = int(self.clock())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {"iat": now - JWT_BACKDATE_S, "exp": now + JWT_LIFETIME_S, "iss": self.app_id}
        signing_input = ".".join(_b64url(json.dumps(p, separators=(",", ":")).encode())
                                 for p in (header, claims))
        sig = self._key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64url(sig)}"

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.api, transport=self.transport, timeout=15,
                            headers={"Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28",
                                     "Authorization": f"Bearer {self.jwt()}"})

    # -- installation tokens -------------------------------------------------

    def installation_id(self) -> int:
        if self._installation is None:
            with self._client() as c:
                r = c.get("/app/installations", params={"per_page": 100})
                if r.status_code != 200:
                    raise GitHubAppError(f"GET /app/installations: {r.status_code} {r.text[:200]}")
                for inst in r.json():
                    if inst.get("account", {}).get("login", "").lower() == self.org.lower():
                        self._installation = int(inst["id"])
                        break
                else:
                    raise GitHubAppError(f"the GitHub App is not installed in '{self.org}'")
        return self._installation

    def installation_token(self, repo: str, write: bool = False) -> str:
        """A token for one repository, from cache while it has > TOKEN_MARGIN_S left.

        A GitHub App's permissions apply to every repository it is installed
        on, so each token is narrowed when it is issued: to `repo` alone, and
        to contents:read unless `write` (contents + pull requests write, for
        system-file branches and PRs). A firmware clone can't push, then, even
        though the App may write to this repository.
        """
        key = (repo.lower(), write)
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] - self.clock() > TOKEN_MARGIN_S:
                return cached[0]
            perms = {"contents": "write", "pull_requests": "write"} if write else {"contents": "read"}
            inst = self.installation_id()
            with self._client() as c:
                r = c.post(f"/app/installations/{inst}/access_tokens",
                           json={"repositories": [repo], "permissions": perms})
            if r.status_code != 201:
                raise GitHubAppError(f"token for {repo}: {r.status_code} {r.text[:200]}")
            body = r.json()
            expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp()
            self._tokens[key] = (body["token"], expires)
            return body["token"]

    def token_for(self, repo: str, *, write: bool = False) -> str:
        """A token for git over HTTPS / the REST API on `repo`.

        `repo` is "owner/name" (owner must be the org) or "name" in the org.
        Use it as https://x-access-token:<token>@github.com/<org>/<name>.git.
        """
        owner, _, name = repo.rpartition("/")
        if not name or (owner and owner.lower() != self.org.lower()):
            raise GitHubAppError(f"'{repo}' is not a repository in '{self.org}'")
        return self.installation_token(name, write)
