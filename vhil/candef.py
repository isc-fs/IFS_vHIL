"""The firmware's own CAN contract: its code-first DSL .def files, parsed.

The ECU and the AMS (IFS08-CE-ECU / IFS08-CE-AMS) declare every frame they
own or consume once, in Core/Inc/can/messages/*.def, and list the .def files
that make up the contract in all_messages.inc; can_codecs.hpp expands that
list into the encoders and decoders, and the DBC is generated from it. So
the .def files of the source an image was built from are the contract that
image speaks, and reading them here keeps tests and the web app in step with
the firmware version under test.

    CAN_MSG(name, id, dlc, "sender", period_ms)          period 0 = event-only
        FIELD_LE / FIELD_LE_S (name, ctype, byte, len, factor, offset, "unit")
        FIELD_BE / FIELD_BE_S (name, ctype, byte, len, factor, offset, "unit")
        FIELD_LE_BITS / FIELD_BE_BITS (name, ctype, start_bit, len, factor, offset, "unit")
    CAN_MSG_END(name)
    CAN_VAL(message, field, raw, "label")                 DBC VAL_ tables

Bit numbering follows Core/Inc/can/can_dsl.hpp (identical in both repos):
an LE field's value bit i is frame bit start + i, start = 8*byte; a BE field
starts at its MSB, start = 8*byte + 7 (Motorola "sawtooth": down within a
byte, then bit 7 of the next byte); a _BITS field gives its start bit
directly. Physical value = raw * factor + offset, as the DBC reads it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterator, NamedTuple, Optional

MESSAGES = Path("Core") / "Inc" / "can" / "messages"
REGISTRY = "all_messages.inc"

_NUM = r"(0x[0-9A-Fa-f]+|\d+)"
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
CAN_MSG = re.compile(r'\bCAN_MSG\(\s*(\w+)\s*,\s*' + _NUM + r'\s*,\s*(\d+)\s*,\s*"(\w*)"\s*,'
                     r'\s*(\d+)\s*\)')
CAN_MSG_END = re.compile(r"\bCAN_MSG_END\(\s*(\w+)\s*\)")
FIELD = re.compile(r'\bFIELD_(LE|BE)(_S|_BITS)?\s*\(\s*(\w+)\s*,\s*\w+\s*,\s*' + _NUM + r'\s*,'
                   r'\s*(\d+)\s*,\s*([^,()]+?)\s*,\s*([^,()]+?)\s*,\s*"([^"]*)"\s*\)')
CAN_VAL = re.compile(r'\bCAN_VAL\(\s*(\w+)\s*,\s*(\w+)\s*,\s*' + _NUM + r'\s*,\s*"([^"]*)"\s*\)')
INCLUDE = re.compile(r'^\s*#\s*include\s+"([\w.-]+\.def)"', re.M)


class Field(NamedTuple):
    big_endian: bool
    signed: bool
    start: int          # frame bit: LE = LSB, BE = MSB (can_dsl.hpp)
    length: int
    factor: float
    offset: float
    unit: str = ""


def le_bits(start: int, length: int) -> Iterator[int]:
    return iter(range(start, start + length))


def be_bits(start: int, length: int) -> Iterator[int]:
    """Frame bits of a Motorola field, MSB first (can_dsl.hpp get_be)."""
    bit = start
    for _ in range(length):
        yield bit
        bit = bit + 15 if bit & 7 == 0 else bit - 1


def get_le(data: bytes, start: int, length: int) -> int:
    return sum(((data[b >> 3] >> (b & 7)) & 1) << i for i, b in enumerate(le_bits(start, length)))


def get_be(data: bytes, start: int, length: int) -> int:
    v = 0
    for b in be_bits(start, length):
        v = (v << 1) | ((data[b >> 3] >> (b & 7)) & 1)
    return v


def _number(text: str) -> float:
    return float(text.rstrip("fF"))


@dataclass
class Message:
    name: str
    id: int
    dlc: int
    sender: str
    period_ms: int
    fields: dict[str, Field] = field(default_factory=dict)
    values: dict[str, dict[int, str]] = field(default_factory=dict)   # field -> raw -> label
    source: str = ""                                                  # the .def it came from

    @property
    def extended(self) -> bool:
        return self.id > 0x7FF

    def bits(self, name: str) -> set[int]:
        f = self.fields[name]
        return set((be_bits if f.big_endian else le_bits)(f.start, f.length))

    def raw(self, data: bytes, name: str) -> int:
        f = self.fields[name]
        v = (get_be if f.big_endian else get_le)(data, f.start, f.length)
        return v - (1 << f.length) if f.signed and v >> (f.length - 1) else v

    def decode(self, data: bytes) -> dict[str, float]:
        """{field: physical value}; a field the frame is too short for is left out."""
        out = {}
        for name, f in self.fields.items():
            if max(self.bits(name)) < 8 * len(data):
                out[name] = self.raw(data, name) * f.factor + f.offset
        return out

    def spare_bits(self, data: bytes) -> list[int]:
        """Bits set in the frame that no field of the .def claims."""
        claimed = set().union(*(self.bits(f) for f in self.fields))
        return [b for b in range(8 * len(data)) if data[b >> 3] >> (b & 7) & 1 and b not in claimed]

    def to_json(self) -> dict:
        return {"name": self.name, "id": self.id, "ext": self.extended, "dlc": self.dlc,
                "sender": self.sender, "period_ms": self.period_ms, "source": self.source,
                "fields": [{"name": n, "be": f.big_endian, "signed": f.signed, "start": f.start,
                            "length": f.length, "factor": f.factor, "offset": f.offset,
                            "unit": f.unit,
                            **({"values": {str(k): v for k, v in sorted(self.values[n].items())}}
                               if n in self.values else {})}
                           for n, f in self.fields.items()]}


def parse_def(text: str, source: str = "") -> list[Message]:
    """The messages one .def declares, with their fields and value tables."""
    text = _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    heads = list(CAN_MSG.finditer(text))
    out = []
    for m, nxt in zip(heads, heads[1:] + [None]):
        stop = nxt.start() if nxt else len(text)
        end = next((e for e in CAN_MSG_END.finditer(text, m.end(), stop)
                    if e.group(1) == m.group(1)), None)
        block = text[m.end():end.start() if end else stop]
        msg = Message(m.group(1), int(m.group(2), 0), int(m.group(3)), m.group(4),
                      int(m.group(5)), source=source)
        for endian, kind, name, at, length, factor, offset, unit in FIELD.findall(block):
            be = endian == "BE"
            start = int(at, 0) if kind == "_BITS" else 8 * int(at, 0) + (7 if be else 0)
            msg.fields[name] = Field(be, kind == "_S", start, int(length),
                                     _number(factor), _number(offset), unit)
        out.append(msg)
    by_name = {m.name: m for m in out}
    for owner, name, raw, label in CAN_VAL.findall(text):
        if owner in by_name:
            by_name[owner].values.setdefault(name, {})[int(raw, 0)] = label
    return out


def parse_dir(messages: Path) -> dict[int, Message]:
    """{id: Message} of every .def all_messages.inc includes (what the
    firmware compiles); every .def in the directory if there is no registry."""
    messages = Path(messages)
    registry = messages / REGISTRY
    if registry.is_file():
        names = INCLUDE.findall(_COMMENT.sub("", registry.read_text()))
    else:
        names = sorted(p.name for p in messages.glob("*.def"))
    out: dict[int, Message] = {}
    for name in names:
        path = messages / name
        if not path.is_file():
            continue
        for msg in parse_def(path.read_text(), name):
            out[msg.id] = msg
    return out


def messages_dir(path: Path, levels: int = 4) -> Optional[Path]:
    """Core/Inc/can/messages of the firmware source holding `path` (an ELF
    under its build tree, or the source root): the nearest ancestor with one."""
    path = Path(path)
    for d in [path, *path.parents][:levels + 1]:
        cand = d / MESSAGES
        if (cand / REGISTRY).is_file() or (cand.is_dir() and any(cand.glob("*.def"))):
            return cand
    return None


def _signature(messages: Path) -> tuple:
    return tuple((p.name, st.st_mtime_ns, st.st_size) for p in sorted(messages.iterdir())
                 if p.suffix in (".def", ".inc") for st in (p.stat(),))


@lru_cache(maxsize=16)
def _cached(messages: Path, signature: tuple) -> dict[int, Message]:
    return parse_dir(messages)


def load(path: Path) -> dict[int, Message]:
    """The contract of the firmware source holding `path`, cached per source
    and re-read when a .def changes (a rebuild replaces the tree)."""
    messages = messages_dir(path)
    if messages is None:
        raise FileNotFoundError(f"no {MESSAGES} at or above {path}")
    messages = messages.resolve()
    return _cached(messages, _signature(messages))
