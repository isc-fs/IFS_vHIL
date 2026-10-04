"""Settings from the environment, with defaults that work in this checkout."""
from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from vhil.system import REPO

log = logging.getLogger("vhil.server")


@dataclass(frozen=True)
class Settings:
    # A git checkout of this repository: its systems/ and catalog/ are what
    # the app shows and edits.
    workspace: Path
    # Run history and the queue (M5.2).
    db: Path
    # Per-run traces and artifacts.
    results: Path
    # "github": OAuth + org check (M5.5), the default. "dev": no login, for a
    # local checkout only, and only when VHIL_AUTH=dev says so explicitly.
    auth: str
    # GitHub logins (lowercase) that may cancel anyone's run and move anyone's
    # saved branch (VHIL_ADMINS, comma-separated).
    admins: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "Settings":
        workspace = Path(os.environ.get("VHIL_WORKSPACE", REPO)).resolve()
        data = Path(os.environ.get("VHIL_DATA", workspace / "results" / "server")).resolve()
        return cls(workspace=workspace,
                   db=Path(os.environ.get("VHIL_DB", data / "vhil.db")),
                   results=Path(os.environ.get("VHIL_RESULTS", data / "runs")),
                   # Fail closed: no VHIL_AUTH means login required.
                   auth=os.environ.get("VHIL_AUTH", "").strip() or "github",
                   admins=parse_admins(os.environ.get("VHIL_ADMINS", "")))

    def is_admin(self, login: str | None) -> bool:
        return bool(login) and login.lower() in self.admins


def parse_admins(text: str) -> frozenset[str]:
    return frozenset(x.strip().lower() for x in text.split(",") if x.strip())


def is_loopback(host: str) -> bool:
    """True for a bind address only this machine can reach."""
    host = host.strip().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_dev_bind(settings: Settings, host: str) -> None:
    """Refuse dev mode (no login) on an address other machines can reach,
    unless VHIL_ALLOW_DEV_ON_NETWORK=1 says the network side is closed some
    other way (a container whose port is published on 127.0.0.1 only, tests).
    Raises SystemExit; logs a loud warning whenever dev mode starts."""
    if settings.auth != "dev":
        return
    if is_loopback(host):
        log.warning("VHIL_AUTH=dev: NO LOGIN. Anyone who reaches %s acts as user 'dev'.", host)
        return
    if os.environ.get("VHIL_ALLOW_DEV_ON_NETWORK", "") == "1":
        log.warning("VHIL_AUTH=dev on %s with VHIL_ALLOW_DEV_ON_NETWORK=1: NO LOGIN, and the "
                    "server listens beyond loopback. Anyone who reaches this port acts as "
                    "user 'dev'. Publish it on 127.0.0.1 only.", host)
        return
    raise SystemExit(f"refusing VHIL_AUTH=dev (no login) on {host}: bind to 127.0.0.1, use "
                     f"VHIL_AUTH=github, or set VHIL_ALLOW_DEV_ON_NETWORK=1 if the port is "
                     f"reachable from this machine only (docs/development/web-app.md)")
