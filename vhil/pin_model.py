"""A board's published pin model: which of its MCU's pins exist and where each goes.

The MainLite's comes from isc-fs/IFS08-ES-MainLite (`docs/pin-model.md` there),
vendored by `scripts/update-pin-model.sh <tag>` into
`catalog/pin-models/mainlite.pins.yaml`: the release asset, byte for byte,
after a comment header recording its repo, tag and sha256. A board entry names
its model (`pin_model:`), and vhil.system checks the board, its roles and every
system against it: a pin a system wires on the board must leave the module
(class `backplane`) or be one of the board's own on-board peripherals.

Names are the board's: a port pin as firmware names it (`PB4`, `PC2_C`, the
model's `port`), or a peripheral (`FDCAN1`, `SPI1`, `USART10`), which stands for
the pins whose model `role` is one of its signals (`SPI1_SCK`, ...).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

SCHEMA = "isc-fs/mainlite-pins"
SCHEMA_VERSIONS = (1,)
# The header line scripts/update-pin-model.sh writes last: the release asset
# follows it unchanged.
HEADER_END = "# --- end of header: the release asset follows, unchanged ---\n"
TAG = re.compile(r"pin-model-v(\d+\.\d+)")


class PinModelError(ValueError):
    pass


@dataclass(frozen=True)
class Status:
    """Where a pin or peripheral goes: `cls` is the model's class
    (`backplane`, `onboard`, `nc`, ...), `where` the backplane header pins
    (J3.8) or the on-board net, for messages."""
    cls: str
    where: str


def split(text: str) -> tuple[dict[str, str], str]:
    """(header fields, body) of a vendored file: the body is the asset."""
    head, sep, body = text.partition(HEADER_END)
    if not sep:
        raise PinModelError("no vendoring header (scripts/update-pin-model.sh writes one)")
    fields = dict(m.groups() for m in re.finditer(r"^# (repo|tag|sha256|url): (\S+)$", head, re.M))
    for key in ("repo", "tag", "sha256"):
        if key not in fields:
            raise PinModelError(f"its vendoring header records no {key}")
    return fields, body


class PinModel:
    def __init__(self, path: Path):
        self.path = Path(path)
        raw = self.path.read_bytes().decode()
        fields, body = split(raw)
        self.repo, self.tag, self.sha256 = fields["repo"], fields["tag"], fields["sha256"]
        if hashlib.sha256(body.encode()).hexdigest() != self.sha256:
            raise PinModelError(f"{self.path.name} is not the {self.tag} release asset: its "
                                f"sha256 differs from the header's (re-run "
                                f"scripts/update-pin-model.sh {self.tag})")
        doc = yaml.safe_load(body)
        if doc.get("schema") != SCHEMA or doc.get("schema_version") not in SCHEMA_VERSIONS:
            raise PinModelError(f"{self.path.name}: schema {doc.get('schema')!r} version "
                                f"{doc.get('schema_version')!r} is not one vhil reads "
                                f"({SCHEMA} {SCHEMA_VERSIONS})")
        m = TAG.fullmatch(self.tag)
        self.version = f"v{m.group(1)}" if m else self.tag
        self.doc = doc
        self.pins = [p for p in doc["pins"] if p.get("port")]
        self.by_port = {p["port"]: p for p in self.pins}
        self.can = {c["fdcan"]: c for c in (doc.get("interfaces") or {}).get("can") or []}
        self.headers = set((doc.get("board") or {}).get("backplane_connectors") or [])
        self.board_name = (doc.get("board") or {}).get("name") or "board"

    @property
    def label(self) -> str:
        """How messages name it: "pin model v1.0"."""
        return f"pin model {self.version}"

    def pins_of(self, name: str) -> list[dict]:
        """The model pins a board name stands for: the port pin, or every pin
        whose role is one of the peripheral's signals. Empty if none."""
        if name in self.by_port:
            return [self.by_port[name]]
        return [p for p in self.pins if (p.get("role") or "").startswith(f"{name}_")]

    def backplane_pins(self) -> list[dict]:
        """Every I/O pin the model routes to the backplane headers."""
        return [p for p in self.pins if p["type"] == "io" and p["class"] == "backplane"]

    def status(self, name: str) -> Status | None:
        """Where a board name goes, or None when the model has no such pin or
        peripheral. A CAN controller leaves the module through its transceiver,
        whose CANH/CANL reach the headers; any other peripheral takes its pins'
        class if they agree."""
        pins = self.pins_of(name)
        if not pins:
            return None
        can = self.can.get(name)
        if can is not None:
            bus = [can[k] for k in ("canh", "canl") if can.get(k)]
            if bus and all(b["connector"] in self.headers for b in bus):
                return Status("backplane", "/".join(f"{b['connector']}.{b['pin']}" for b in bus))
        classes = {p["class"] for p in pins}
        if len(classes) > 1:
            return Status("mixed", ", ".join(f"{p['port']} {p['class']}" for p in pins))
        (cls,) = classes
        if cls == "backplane":
            where = ", ".join(f"{b['connector']}.{b['pin']}" for p in pins for b in p["backplane"])
        else:
            where = ", ".join(sorted({p["net"] for p in pins if p.get("net")})) or "no net"
        return Status(cls, where)


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> PinModel:
    return PinModel(Path(path))


def load(path: Path) -> PinModel:
    """The pin model at `path`, parsed once per file version."""
    path = Path(path)
    if not path.is_file():
        raise PinModelError(f"no pin model {path}")
    return _load(str(path.resolve()), path.stat().st_mtime)
