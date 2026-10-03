"""vhil/elf.py against a minimal hand-built ELF32: two one-byte objects
packed side by side, the case Renode's GetSymbolAddress gets wrong."""
import struct

import pytest

from vhil import elf


def _elf(path, syms):
    strtab = b"\0"
    entries = [bytes(16)]                                   # the null symbol
    for name, value, size, kind in syms:
        entries.append(struct.pack("<IIIBBH", len(strtab), value, size, kind, 0, 1))
        strtab += name.encode() + b"\0"
    symtab = b"".join(entries)
    shstrtab = b"\0.symtab\0.strtab\0"
    body = symtab + strtab + shstrtab
    sym_off, str_off, shs_off = 52, 52 + len(symtab), 52 + len(symtab) + len(strtab)
    sh_off = 52 + len(body)
    header = (b"\x7fELF\x01\x01\x01" + bytes(9)
              + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, sh_off, 0, 52, 0, 0, 40, 4, 3))
    sections = [bytes(40),
                struct.pack("<IIIIIIIIII", 1, 2, 0, 0, sym_off, len(symtab), 2, 1, 4, 16),
                struct.pack("<IIIIIIIIII", 9, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0),
                struct.pack("<IIIIIIIIII", 17, 3, 0, 0, shs_off, len(shstrtab), 0, 0, 1, 0)]
    path.write_bytes(header + body + b"".join(sections))
    return path


def test_packed_one_byte_objects_keep_their_own_addresses(tmp_path):
    f = _elf(tmp_path / "a.elf", [("g_state", 0x2000129C, 1, 1),
                                  ("g_mode", 0x2000129D, 1, 1),
                                  ("main", 0x08020101, 64, 2)])
    assert elf.symbol(f, "g_state") == (0x2000129C, 1)
    assert elf.symbol(f, "g_mode") == (0x2000129D, 1)
    assert elf.symbol(f, "main") == (0x08020101, 64)


def test_an_unknown_symbol_names_the_image(tmp_path):
    f = _elf(tmp_path / "a.elf", [("x", 4, 4, 1)])
    with pytest.raises(KeyError, match="nope"):
        elf.symbol(f, "nope")


def test_not_an_elf(tmp_path):
    (tmp_path / "x").write_bytes(b"hello world, not an elf at all......")
    with pytest.raises(ValueError):
        elf.symbol(tmp_path / "x", "x")
