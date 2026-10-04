"""Symbol table of a 32-bit little-endian ELF (the Arm images), host-only.

Renode's `sysbus GetSymbolAddress` can answer with a neighbour's address for
one-byte objects packed together: in AMS.elf it gives g_mode_locked_telemetry
(0x2000129D) the address of g_state_telemetry (0x2000129C). A test reading a
firmware global must get the linker's address, so it comes from here.

`symbol_at` goes the other way, address to symbol, for failure snapshots and
coverage (vhil/trace.py, vhil/coverage.py). Thumb function symbols carry the
mode in bit 0 of their value; `functions` and `symbol_at` clear it.
"""
from __future__ import annotations

import bisect
import struct
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Optional

SHT_SYMTAB = 2
STT_OBJECT, STT_FUNC = 1, 2
SHF_ALLOC, SHF_EXECINSTR = 0x2, 0x4


class Symbol(NamedTuple):
    start: int
    size: int
    name: str
    func: bool

    @property
    def end(self) -> int:
        return self.start + self.size


@lru_cache(maxsize=16)
def _table(path: str, mtime: float) -> tuple[list[tuple[str, int, int, int]], list[tuple[int, int, int]]]:
    """([(name, value, size, type)], [(addr, size, flags)] of each section)."""
    data = Path(path).read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError(f"{path}: not a 32-bit little-endian ELF")
    e_shoff, = struct.unpack_from("<I", data, 0x20)
    e_shentsize, e_shnum = struct.unpack_from("<HH", data, 0x2E)
    sections = [struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize)
                for i in range(e_shnum)]
    syms = []
    for _, sh_type, _, _, offset, size, link, _, _, entsize in sections:
        if sh_type != SHT_SYMTAB:
            continue
        strtab_off = sections[link][4]
        for i in range(size // entsize):
            st_name, st_value, st_size, st_info, _, _ = struct.unpack_from(
                "<IIIBBH", data, offset + i * entsize)
            kind = st_info & 0xF
            if kind not in (STT_OBJECT, STT_FUNC) or not st_name:
                continue
            end = data.index(b"\0", strtab_off + st_name)
            syms.append((data[strtab_off + st_name:end].decode(), st_value, st_size, kind))
    return syms, [(s[3], s[5], s[2]) for s in sections]


def _cached(path: Path | str):
    p = Path(path)
    return _table(str(p.resolve()), p.stat().st_mtime)


def symbols(path: Path | str) -> dict[str, tuple[int, int]]:
    """{name: (address, size)} of every object and function symbol."""
    return {name: (value, size) for name, value, size, _ in _cached(path)[0]}


def symbol(path: Path | str, name: str) -> tuple[int, int]:
    try:
        return symbols(path)[name]
    except KeyError:
        raise KeyError(f"{name} not in {path}") from None


@lru_cache(maxsize=16)
def _sorted(path: str, mtime: float) -> tuple[list[Symbol], list[int]]:
    syms, _ = _table(path, mtime)
    out = {}
    for name, value, size, kind in syms:
        func = kind == STT_FUNC
        start = value & ~1 if func else value
        # Aliases (weak handlers sharing Default_Handler, ...): first name wins.
        out.setdefault((start, func), Symbol(start, size, name, func))
    ordered = sorted(out.values(), key=lambda s: (s.start, not s.func))
    return ordered, [s.start for s in ordered]


def functions(path: Path | str) -> list[Symbol]:
    """Function symbols by address, Thumb bit cleared."""
    p = Path(path)
    return [s for s in _sorted(str(p.resolve()), p.stat().st_mtime)[0] if s.func]


def symbol_at(path: Path | str, address: int) -> Optional[tuple[str, int]]:
    """(name, offset) of the function or object whose extent holds `address`,
    functions first; None when no sized symbol does."""
    p = Path(path)
    ordered, starts = _sorted(str(p.resolve()), p.stat().st_mtime)
    i = bisect.bisect_right(starts, address)
    best = None
    # Walk back over the symbols starting at or below the address: the
    # nearest one need not be the one that contains it (nested objects).
    for s in reversed(ordered[max(0, i - 8):i]):
        if s.start <= address < s.start + max(s.size, 1):
            if s.func:
                return s.name, address - s.start
            best = best or (s.name, address - s.start)
    return best


def exec_ranges(path: Path | str) -> list[tuple[int, int]]:
    """[start, end) of each loaded executable section."""
    return [(addr, addr + size) for addr, size, flags in _cached(path)[1]
            if flags & SHF_ALLOC and flags & SHF_EXECINSTR and size]
