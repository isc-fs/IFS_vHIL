"""Settings from the environment, with defaults that work in this checkout."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from vhil.system import REPO


@dataclass(frozen=True)
class Settings:
    # A git checkout of this repository: its systems/ and catalog/ are what
    # the app shows and edits.
    workspace: Path
    # Run history and the queue (M5.2).
    db: Path
    # Per-run traces and artifacts.
    results: Path
    # "dev": no login (local use). "github": OAuth + org check (M5.5).
    auth: str

    @classmethod
    def from_env(cls) -> "Settings":
        workspace = Path(os.environ.get("VHIL_WORKSPACE", REPO)).resolve()
        data = Path(os.environ.get("VHIL_DATA", workspace / "results" / "server")).resolve()
        return cls(workspace=workspace,
                   db=Path(os.environ.get("VHIL_DB", data / "vhil.db")),
                   results=Path(os.environ.get("VHIL_RESULTS", data / "runs")),
                   auth=os.environ.get("VHIL_AUTH", "dev"))
