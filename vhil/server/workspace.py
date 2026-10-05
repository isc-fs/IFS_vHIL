"""The git workspace the app works on: its system files and catalogue."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from vhil.system import System, SystemError

SYSTEM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")  # \Z: no trailing newline


class NotFound(Exception):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)

    def ref(self) -> str:
        """The checked-out commit, or "" outside git."""
        out = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                             capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""

    def system_path(self, system_id: str) -> Path:
        if not SYSTEM_ID.match(system_id):
            raise NotFound(system_id)
        path = self.root / "systems" / f"{system_id}.yaml"
        if not path.is_file():
            raise NotFound(system_id)
        return path

    def systems(self) -> list[dict]:
        out = []
        for path in sorted((self.root / "systems").glob("*.yaml")):
            doc = yaml.safe_load(path.read_text()) or {}
            out.append({"id": path.stem, "path": str(path.relative_to(self.root)),
                        "description": " ".join(str(doc.get("description", "")).split()),
                        "boards": sorted((doc.get("boards") or {}).keys())})
        return out

    def system(self, system_id: str) -> dict:
        path = self.system_path(system_id)
        text = path.read_text()
        try:
            System(path)
            errors = []
        except SystemError as e:
            errors = [str(e)]
        return {"id": system_id, "path": str(path.relative_to(self.root)), "ref": self.ref(),
                "yaml": text, "doc": yaml.safe_load(text), "errors": errors}

    def catalog(self) -> dict:
        cat = self.root / "catalog"
        out = {}
        for kind in ("boards", "backplanes", "models", "platforms", "firmware"):
            entries = []
            for path in sorted((cat / kind).glob("*.yaml")):
                doc = yaml.safe_load(path.read_text()) or {}
                entries.append({"id": doc.get("id", path.stem),
                                "description": " ".join(str(doc.get("description", "")).split())})
            out[kind] = entries
        return out
