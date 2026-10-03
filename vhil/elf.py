"""Symbol table of a 32-bit little-endian ELF (the Arm images), host-only.

Renode's `sysbus GetSymbolAddress` can answer with a neighbour's address for
one-byte objects packed together: in AMS.elf it gives g_mode_locked_telemetry
(0x2000129D) the address of g_state_telemetry (0x2000129C). A test reading a
firmware global must get the linker's address, so it comes from here.
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

SHT_SYMTAB = 2
STT_OBJECT, STT_FUNC = 1, 2


@lru_cache(maxsize=16)
def _symbols(path: str, mtime: float) -> dict[str, tuple[int, int]]:
    data = Path(path).read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError(f"{path}: not a 32-bit little-endian ELF")
    e_shoff, = struct.unpack_from("<I", data, 0x20)
    e_shentsize, e_shnum = struct.unpack_from("<HH", data, 0x2E)
    sections = [struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize)
                for i in range(e_shnum)]
    out: dict[str, tuple[int, int]] = {}
    for _, sh_type, _, _, offset, size, link, _, _, entsize in sections:
        if sh_type != SHT_SYMTAB:
            continue
        strtab_off = sections[link][4]
        for i in range(size // entsize):
            st_name, st_value, st_size, st_info, _, _ = struct.unpack_from(
                "<IIIBBH", data, offset + i * entsize)
            if st_info & 0xF not in (STT_OBJECT, STT_FUNC) or not st_name:
                continue
            end = data.index(b"\0", strtab_off + st_name)
            out[data[strtab_off + st_name:end].decode()] = (st_value, st_size)
    return out


def symbols(path: Path | str) -> dict[str, tuple[int, int]]:
    """{name: (address, size)} of every object and function symbol."""
    p = Path(path)
    return _symbols(str(p.resolve()), p.stat().st_mtime)


def symbol(path: Path | str, name: str) -> tuple[int, int]:
    try:
        return symbols(path)[name]
    except KeyError:
        raise KeyError(f"{name} not in {path}") from None
