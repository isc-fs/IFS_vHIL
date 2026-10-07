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


# -- DWARF enumerations ----------------------------------------------------------------

def _uleb(v):
    out = bytearray()
    while True:
        b, v = v & 0x7F, v >> 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _elf_with(path, sections):
    """An ELF32 holding the named sections (contents only) and a .shstrtab."""
    names, body, placed = b"\0", b"", []
    for name, data in sections.items():
        placed.append((len(names), 52 + len(body), len(data)))
        names += name.encode() + b"\0"
        body += data
    shstr_name = len(names)
    names += b".shstrtab\0"
    shstr_off = 52 + len(body)
    body += names
    table = [bytes(40)]
    table += [struct.pack("<IIIIIIIIII", n, 1, 0, 0, off, size, 0, 0, 1, 0)
              for n, off, size in placed]
    table.append(struct.pack("<IIIIIIIIII", shstr_name, 3, 0, 0, shstr_off, len(names),
                             0, 0, 1, 0))
    header = (b"\x7fELF\x01\x01\x01" + bytes(9)
              + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, 52 + len(body), 0, 52, 0, 0, 40,
                            len(table), len(table) - 1))
    path.write_bytes(header + body + b"".join(table))
    return path


# Abbreviations: code -> (tag, children, [(attribute, form[, implicit const])]).
ABBREV = {
    1: (0x11, True, []),                                   # compile_unit
    2: (0x39, True, [(0x03, 0x0E)]),                       # namespace, name strp
    3: (0x04, True, [(0x03, 0x08)]),                       # enumeration_type, name
    4: (0x28, False, [(0x03, 0x08), (0x1C, 0x0B)]),        # enumerator, data1
    5: (0x28, False, [(0x03, 0x08), (0x1C, 0x0D)]),        # enumerator, sdata
    6: (0x16, False, [(0x03, 0x08), (0x49, 0x13)]),        # typedef, ref4
    7: (0x35, False, [(0x49, 0x13)]),                      # volatile_type
    8: (0x34, False, [(0x03, 0x08), (0x3B, 0x21, 7), (0x49, 0x13)]),  # variable, implicit_const
    9: (0x04, True, []),                                   # anonymous enumeration_type
    10: (0x2E, True, [(0x03, 0x08)]),                      # subprogram
    11: (0x34, False, [(0x47, 0x13)]),                     # variable: a definition (specification)
    12: (0x24, False, [(0x03, 0x08), (0x0B, 0x0B)]),       # base_type, byte_size
}


def _abbrev_section():
    out = b""
    for code, (tag, children, attrs) in ABBREV.items():
        out += _uleb(code) + _uleb(tag) + bytes([1 if children else 0])
        for a in attrs:
            out += _uleb(a[0]) + _uleb(a[1])
            if a[1] == 0x21:
                out += bytes([a[2]])               # sleb of a small positive value
        out += b"\0\0"
    return out + b"\0"


def _dwarf_elf(path):
    """One DWARF 5 unit: namespace ams { namespace fsm { enum State {Start,
    Precharge, Error = 5}; } }, a C-style typedef'd anonymous enum, globals
    typed through volatile and typedef, a definition naming its declaration,
    a local (ignored), and an int global (not an enum)."""
    dies, refs, at = bytearray(), [], {}

    def die(label, code, *parts):
        at[label] = 12 + len(dies)          # unit-relative: after the 12-byte header
        dies.extend(_uleb(code))
        for p in parts:
            if isinstance(p, tuple):        # ("ref", label): patched below
                refs.append((len(dies), p[1]))
                dies.extend(bytes(4))
            else:
                dies.extend(p)

    def end():
        dies.extend(b"\0")

    def s(text):
        return text.encode() + b"\0"

    die("cu", 1)
    die("ams", 2, struct.pack("<I", 0))     # .debug_str "ams"
    die("fsm", 2, struct.pack("<I", 4))     # .debug_str "fsm"
    die("State", 3, s("State"))
    die("e0", 4, s("Start"), b"\x00")
    die("e1", 4, s("Precharge"), b"\x01")
    die("e5", 5, s("Error"), _uleb(5))
    end()                                   # State
    end()                                   # fsm
    die("decl", 8, s("g_decl"), ("ref", "vState"))
    end()                                   # ams
    die("anon", 9)
    die("a0", 5, s("LOG_OFF"), b"\x7f")     # sdata -1
    die("a1", 4, s("LOG_ON"), b"\x01")
    end()
    die("Mode_t", 6, s("Mode_t"), ("ref", "anon"))
    die("vState", 7, ("ref", "State"))
    die("vMode", 7, ("ref", "Mode_t"))
    die("g_state", 8, s("g_state"), ("ref", "vState"))
    die("g_mode", 8, s("g_mode"), ("ref", "vMode"))
    die("int", 12, s("int"), b"\x04")
    die("g_int", 8, s("g_int"), ("ref", "int"))
    die("def", 11, ("ref", "decl"))
    die("fn", 10, s("main"))
    die("local", 8, s("g_local"), ("ref", "State"))
    end()                                   # main
    end()                                   # the unit
    for pos, label in refs:
        dies[pos:pos + 4] = struct.pack("<I", at[label])
    unit = struct.pack("<HBBI", 5, 1, 4, 0) + bytes(dies)   # v5, DW_UT_compile, addr 4
    return _elf_with(path, {".debug_info": struct.pack("<I", len(unit)) + unit,
                            ".debug_abbrev": _abbrev_section(),
                            ".debug_str": b"ams\0fsm\0"})


def test_enums_from_dwarf(tmp_path):
    f = _dwarf_elf(tmp_path / "d.elf")
    assert elf.has_dwarf(f)
    found = elf.enums(f)
    assert found.types == {"ams::fsm::State": {0: "Start", 1: "Precharge", 5: "Error"},
                           "Mode_t": {-1: "LOG_OFF", 1: "LOG_ON"}}
    # Through volatile and typedef; a definition takes its declaration's
    # name and type; locals and non-enum globals are left out.
    assert found.variables == {"g_decl": "ams::fsm::State", "g_state": "ams::fsm::State",
                               "g_mode": "Mode_t"}
    assert elf.find_enum(found, "State") == "ams::fsm::State"
    assert elf.find_enum(found, "fsm::State") == "ams::fsm::State"
    assert elf.find_enum(found, "Nope") is None


def test_no_dwarf_no_enums(tmp_path):
    f = _elf(tmp_path / "a.elf", [("g_state", 0x2000129C, 1, 1)])
    assert not elf.has_dwarf(f)
    assert elf.enums(f) == elf.Enums({}, {})
