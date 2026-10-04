"""vhil/elf.py against a minimal hand-built ELF32: two one-byte objects
packed side by side, the case Renode's GetSymbolAddress gets wrong."""
import struct

import pytest

from vhil import elf


def _elf(path, syms, text=None):
    """syms: (name, value, size, type); text: (addr, size) of a code section."""
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
    if text:   # SHT_NOBITS, SHF_ALLOC | SHF_EXECINSTR: only its extent matters
        sections.append(struct.pack("<IIIIIIIIII", 0, 8, 6, text[0], 0, text[1], 0, 0, 2, 0))
        header = header[:48] + struct.pack("<H", len(sections)) + header[50:]
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


def test_symbol_at_clears_the_thumb_bit_and_prefers_functions(tmp_path):
    f = _elf(tmp_path / "a.elf", [("main", 0x08020101, 0x40, 2),
                                  ("SysTick_Handler", 0x08020141, 0x10, 2),
                                  ("table", 0x08020100, 0x100, 1),
                                  ("g_x", 0x24000010, 4, 1)])
    assert elf.symbol_at(f, 0x08020100) == ("main", 0)
    assert elf.symbol_at(f, 0x0802013E) == ("main", 0x3E)
    assert elf.symbol_at(f, 0x08020142) == ("SysTick_Handler", 2)
    assert elf.symbol_at(f, 0x08020180) == ("table", 0x80)      # no function there
    assert elf.symbol_at(f, 0x24000013) == ("g_x", 3)
    assert elf.symbol_at(f, 0x24000014) is None
    assert elf.symbol_at(f, 0x100) is None


def test_aliases_keep_the_first_name(tmp_path):
    f = _elf(tmp_path / "a.elf", [("Default_Handler", 0x08000201, 2, 2),
                                  ("WWDG_IRQHandler", 0x08000201, 2, 2)])
    assert elf.symbol_at(f, 0x08000200) == ("Default_Handler", 0)
    assert [s.name for s in elf.functions(f)] == ["Default_Handler"]


def test_functions_are_sorted_with_their_extent(tmp_path):
    f = _elf(tmp_path / "a.elf", [("b", 0x08020201, 8, 2), ("a", 0x08020101, 4, 2),
                                  ("obj", 0x24000000, 4, 1)])
    fs = elf.functions(f)
    assert [(s.name, s.start, s.end) for s in fs] == [("a", 0x08020100, 0x08020104),
                                                       ("b", 0x08020200, 0x08020208)]


def test_exec_ranges(tmp_path):
    f = _elf(tmp_path / "a.elf", [("main", 0x08020101, 4, 2)], text=(0x08020000, 0x1000))
    assert elf.exec_ranges(f) == [(0x08020000, 0x08021000)]
