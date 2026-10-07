"""Symbol table of a 32-bit little-endian ELF (the Arm images), host-only.

Renode's `sysbus GetSymbolAddress` can answer with a neighbour's address for
one-byte objects packed together: in AMS.elf it gives g_mode_locked_telemetry
(0x2000129D) the address of g_state_telemetry (0x2000129C). A test reading a
firmware global must get the linker's address, so it comes from here.

`symbol_at` goes the other way, address to symbol, for failure snapshots and
coverage (vhil/trace.py, vhil/coverage.py). Thumb function symbols carry the
mode in bit 0 of their value; `functions` and `symbol_at` clear it.

`enums` reads the enumerations an image declares from its DWARF debug info
(.debug_info, versions 2 to 5, as Arm GNU 14.2 writes it with -g): each enum
type by its qualified name (`ams::fsm::State`) with its enumerators, and the
enum each global variable is typed with, through typedefs and qualifiers. The
state panel labels a firmware's FSM state with it ("Precharge", not 1;
vhil/stateview.py). An image built without -g has no enums; `has_dwarf` says
so. Parsed once per image and cached, as the symbol table is.
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


# -- DWARF enumerations ------------------------------------------------------------

TAG_ENUM, TAG_ENUMERATOR, TAG_TYPEDEF, TAG_VARIABLE, TAG_SUBPROGRAM = 0x04, 0x28, 0x16, 0x34, 0x2E
# What qualifies a name: class, struct, union, namespace (and the enum itself,
# for its enumerators).
_SCOPES = {0x02, 0x13, 0x17, 0x39}
# Type wrappers followed from a variable to its enum: typedef, const,
# volatile, _Atomic.
_WRAPPERS = {0x16, 0x26, 0x35, 0x47}
AT_NAME, AT_CONST_VALUE, AT_TYPE, AT_SPECIFICATION = 0x03, 0x1C, 0x49, 0x47
_REF_CU = {0x11, 0x12, 0x13, 0x14, 0x15}       # ref1..ref8, ref_udata: unit-relative
# Fixed-size forms: (bytes). strx1..4 / addrx1..4 index tables a non-split
# GCC build doesn't emit; read and dropped.
_FIXED = {0x0B: 1, 0x05: 2, 0x06: 4, 0x07: 8, 0x11: 1, 0x12: 2, 0x13: 4, 0x14: 8, 0x0C: 1,
          0x25: 1, 0x26: 2, 0x27: 3, 0x28: 4, 0x29: 1, 0x2A: 2, 0x2B: 3, 0x2C: 4, 0x1C: 4,
          0x24: 8, 0x20: 8, 0x1E: 16}
_DROPPED = {0x25, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x2C}


class Enums(NamedTuple):
    types: dict[str, dict[int, str]]     # qualified enum name -> {value: enumerator}
    variables: dict[str, str]            # global variable -> the enum its type is


def _uleb(d: bytes, i: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        b = d[i]
        i += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if b < 0x80:
            return v, i


def _sleb(d: bytes, i: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        b = d[i]
        i += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if b < 0x80:
            return (v - (1 << shift) if b & 0x40 else v), i


def _sections(data: bytes) -> dict[str, bytes]:
    """{name: contents} of an ELF32's sections that have contents."""
    e_shoff, = struct.unpack_from("<I", data, 0x20)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x2E)
    heads = [struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize)
             for i in range(e_shnum)]
    if e_shstrndx >= len(heads):
        return {}
    names = heads[e_shstrndx][4]
    out = {}
    for h in heads:
        name = data[names + h[0]:data.find(b"\0", names + h[0])].decode(errors="replace")
        if h[1] != 8:                               # SHT_NOBITS
            out[name] = data[h[4]:h[4] + h[5]]
    return out


def _abbrevs(d: bytes, off: int) -> dict[int, tuple[int, bool, list[tuple[int, int, int]]]]:
    """{code: (tag, has children, [(attribute, form, implicit const)])}."""
    out = {}
    while True:
        code, off = _uleb(d, off)
        if code == 0:
            return out
        tag, off = _uleb(d, off)
        children = d[off] == 1
        off += 1
        attrs = []
        while True:
            at, off = _uleb(d, off)
            form, off = _uleb(d, off)
            implicit = 0
            if form == 0x21:                        # DW_FORM_implicit_const
                implicit, off = _sleb(d, off)
            if at == 0 and form == 0:
                break
            attrs.append((at, form, implicit))
        out[code] = (tag, children, attrs)


class _Unit:
    """Reads one unit's attribute values (DWARF 2-5 forms)."""

    def __init__(self, sec: dict[str, bytes]):
        self.info = sec[".debug_info"]
        self.str = sec.get(".debug_str", b"")
        self.line_str = sec.get(".debug_line_str", b"")
        self.start, self.offset_size, self.addr_size = 0, 4, 4

    @staticmethod
    def _cstr(buf: bytes, off: int) -> Optional[str]:
        return buf[off:buf.find(b"\0", off)].decode(errors="replace") if off < len(buf) else None

    def value(self, form: int, i: int, implicit: int):
        """(value, next offset): a str, an int, or None for what isn't read."""
        d = self.info
        n = _FIXED.get(form)
        if n is not None:
            v = int.from_bytes(d[i:i + n], "little")
            if form in _REF_CU:
                v += self.start
            return (None if form in _DROPPED else v), i + n
        if form == 0x08:                            # string
            end = d.find(b"\0", i)
            return d[i:end].decode(errors="replace"), end + 1
        if form in (0x0E, 0x1F, 0x10, 0x17, 0x1D, 0x1F20, 0x1F21):  # strp, line_strp, ref_addr, ...
            v = int.from_bytes(d[i:i + self.offset_size], "little")
            i += self.offset_size
            if form == 0x0E:
                return self._cstr(self.str, v), i
            if form == 0x1F:
                return self._cstr(self.line_str, v), i
            return v, i
        if form == 0x01:                            # addr
            return int.from_bytes(d[i:i + self.addr_size], "little"), i + self.addr_size
        if form == 0x0D:                            # sdata
            return _sleb(d, i)
        if form in (0x0F, 0x15):                    # udata, ref_udata
            v, i = _uleb(d, i)
            return (v + self.start if form == 0x15 else v), i
        if form in (0x1A, 0x1B, 0x22, 0x23, 0x1F01, 0x1F02):  # strx, addrx, loclistx, rnglistx
            return None, _uleb(d, i)[1]
        if form in (0x09, 0x18):                    # block, exprloc
            n, i = _uleb(d, i)
            return None, i + n
        if form in (0x0A, 0x03, 0x04):              # block1, block2, block4
            w = {0x0A: 1, 0x03: 2, 0x04: 4}[form]
            return None, i + w + int.from_bytes(d[i:i + w], "little")
        if form == 0x19:                            # flag_present
            return 1, i
        if form == 0x21:                            # implicit_const
            return implicit, i
        if form == 0x16:                            # indirect
            real, i = _uleb(d, i)
            return self.value(real, i, implicit)
        raise ValueError(f"DWARF form 0x{form:x} at .debug_info+0x{i:x} is not supported")


def _walk(sec: dict[str, bytes]) -> Enums:
    """Every enumeration, and every global variable's type, in one pass over
    .debug_info."""
    info, abbrev = sec[".debug_info"], sec.get(".debug_abbrev", b"")
    u = _Unit(sec)
    tables: dict[int, dict] = {}
    enums: dict[int, tuple[Optional[str], dict[int, str]]] = {}   # DIE -> (qualified, values)
    wrappers: dict[int, tuple[Optional[str], Optional[int]]] = {}  # DIE -> (typedef name, type)
    variables: dict[int, tuple[Optional[str], Optional[int], Optional[int]]] = {}  # name, type, spec
    off = 0
    while off + 11 <= len(info):
        u.start = off
        length, = struct.unpack_from("<I", info, off)
        off += 4
        u.offset_size = 4
        if length == 0xFFFFFFFF:
            length, = struct.unpack_from("<Q", info, off)
            off += 8
            u.offset_size = 8
        end = off + length
        version, = struct.unpack_from("<H", info, off)
        off += 2
        osz = u.offset_size
        if version >= 5:
            unit_type, u.addr_size = info[off], info[off + 1]
            off += 2
            ab = int.from_bytes(info[off:off + osz], "little")
            off += osz
            # type / split_type: signature + type offset; skeleton / split: dwo id
            off += {2: 8 + osz, 6: 8 + osz, 4: 8, 5: 8}.get(unit_type, 0)
        else:
            ab = int.from_bytes(info[off:off + osz], "little")
            u.addr_size = info[off + osz]
            off += osz + 1
        if ab not in tables:
            tables[ab] = _abbrevs(abbrev, ab)
        table = tables[ab]
        stack: list[tuple[int, Optional[str], int]] = []     # (tag, name, DIE) with children
        in_function = 0
        while off < end:
            die = off
            code, off = _uleb(info, off)
            if code == 0:
                if stack and stack.pop()[0] == TAG_SUBPROGRAM:
                    in_function -= 1
                continue
            tag, children, attrs = table[code]
            vals = {}
            for at, form, implicit in attrs:
                v, off = u.value(form, off, implicit)
                vals[at] = v
            name = vals.get(AT_NAME)
            if tag == TAG_ENUM or (tag == TAG_TYPEDEF and name):
                scope = "::".join(n for t, n, _ in stack if t in _SCOPES and n)
                qual = f"{scope}::{name}" if scope and name else name
                if tag == TAG_ENUM:
                    enums[die] = (qual, {})
                else:
                    wrappers[die] = (qual, vals.get(AT_TYPE))
            elif tag in _WRAPPERS:
                wrappers[die] = (None, vals.get(AT_TYPE))
            elif tag == TAG_ENUMERATOR and stack and stack[-1][0] == TAG_ENUM:
                value = vals.get(AT_CONST_VALUE)
                if isinstance(name, str) and isinstance(value, int):
                    enums[stack[-1][2]][1].setdefault(value, name)
            elif tag == TAG_VARIABLE and not in_function:
                variables[die] = (name, vals.get(AT_TYPE), vals.get(AT_SPECIFICATION))
            if children:
                stack.append((tag, name, die))
                if tag == TAG_SUBPROGRAM:
                    in_function += 1
        off = end

    def enum_of(t: Optional[int]) -> Optional[int]:
        for _ in range(16):
            if t is None or t in enums:
                return t
            t = wrappers.get(t, (None, None))[1]
        return None

    # A typedef names an anonymous enum (C's `typedef enum {...} Foo_t;`).
    named: dict[int, str] = {}
    for tname, target in wrappers.values():
        e = enum_of(target)
        if tname and e is not None and not enums[e][0]:
            named.setdefault(e, tname)
    types: dict[str, dict[int, str]] = {}
    qual_of: dict[int, str] = {}
    for die, (qual, values) in enums.items():
        qual = qual or named.get(die)
        if qual and values:
            types.setdefault(qual, dict(sorted(values.items())))
            qual_of[die] = qual
    out: dict[str, str] = {}
    for name, t, spec in variables.values():
        if spec is not None:                        # a definition of a declaration
            sname, stype, _ = variables.get(spec, (None, None, None))
            name, t = name or sname, t if t is not None else stype
        e = enum_of(t)
        if name and e in qual_of:
            out.setdefault(name, qual_of[e])
    return Enums(types, out)


@lru_cache(maxsize=8)
def _enums(path: str, mtime: float) -> Enums:
    data = Path(path).read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError(f"{path}: not a 32-bit little-endian ELF")
    sec = _sections(data)
    return _walk(sec) if ".debug_info" in sec else Enums({}, {})


def has_dwarf(path: Path | str) -> bool:
    """Whether the image carries DWARF debug info (built with -g)."""
    data = Path(path).read_bytes()
    return data[:4] == b"\x7fELF" and ".debug_info" in _sections(data)


def enums(path: Path | str) -> Enums:
    """The image's enumerations and its enum-typed globals (module doc);
    empty without DWARF."""
    p = Path(path)
    return _enums(str(p.resolve()), p.stat().st_mtime)


def find_enum(found: Enums, name: str) -> Optional[str]:
    """The qualified name of enum `name`: itself, or the one enum whose
    qualified name ends in `::name`; None when none or several do."""
    if name in found.types:
        return name
    hits = [q for q in found.types if q.endswith("::" + name)]
    return hits[0] if len(hits) == 1 else None
