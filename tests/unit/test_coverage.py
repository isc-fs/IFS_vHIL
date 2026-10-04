"""vhil/coverage.py: translated-block logs to function and line coverage,
on hand-written tool output and tiny ELFs (no Renode, no toolchain)."""
import json

from vhil import coverage, elf
from vhil.coverage import LineTable

from tests.unit.test_elf import _elf

TBLOG = """-------------------------
IN: Reset_Handler (entry) (address: 0x0803a220)
0x0803a220:   f8dfd038       \tldr.w\tsp, [pc, #56]
0x0803a224:   f7e7fdd0       \tbl\t#-99424


-------------------------
IN: main (address: 0x08020100)
0x08020100:   4b04           \tldr\tr3, [pc, #16]
0x08020102:   68da           \tldr\tr2, [r3, #12]
"""

DECODED = """
/x/ECU08.elf:     file format elf32-littlearm

Contents of the .debug_line section:

main.c:
File name                        Line number    Starting address    View    Stmt
main.c                                    10           0x8020100               x
main.c                                    11           0x8020102               x
main.c                                    12           0x8020106               x
main.c                                     -           0x802010a

/src/Core/Inc/util.h:
util.h                                     5           0x8020200               x
util.h                                     6           0x8020204       1       x
util.h                                     -           0x8020208
"""


def test_parse_tblog():
    assert coverage.parse_tblog(TBLOG) == {0x0803A220, 0x0803A224, 0x08020100, 0x08020102}


def test_parse_decodedline():
    seqs = coverage.parse_decodedline(DECODED)
    assert seqs == [[(0x8020100, 10), (0x8020102, 11), (0x8020106, 12), (0x802010A, None)],
                    [(0x8020200, 5), (0x8020204, 6), (0x8020208, None)]]


def test_parse_addr2line():
    out = coverage.parse_addr2line("/src/main.c:10\n??:0\n/src/a.h:7 (discriminator 2)\n??:?\n")
    assert out == [("/src/main.c", 10), None, ("/src/a.h", 7), None]


def _table():
    files = {0x8020100: "/src/Core/Src/main.c", 0x8020102: "/src/Core/Src/main.c",
             0x8020106: "/src/Core/Src/main.c", 0x8020200: "/src/Core/Inc/util.h",
             0x8020204: "/src/Core/Inc/util.h"}
    return LineTable.build(coverage.parse_decodedline(DECODED),
                           lambda addrs: [(files[a], 0) for a in addrs])


def test_line_table():
    t = _table()
    assert t.line_of(0x8020100) == ("/src/Core/Src/main.c", 10)
    assert t.line_of(0x8020104) == ("/src/Core/Src/main.c", 11)
    assert t.line_of(0x8020109) == ("/src/Core/Src/main.c", 12)
    assert t.line_of(0x802010A) is None                  # end of the sequence
    assert t.line_of(0x80200FF) is None
    assert t.lines() == {"/src/Core/Src/main.c": {10, 11, 12}, "/src/Core/Inc/util.h": {5, 6}}


def _image(tmp_path):
    return _elf(tmp_path / "ECU08.elf", [("main", 0x08020101, 0x0A, 2),
                                         ("helper", 0x08020201, 0x08, 2),
                                         ("libc_memcpy", 0x08030001, 0x10, 2),
                                         ("g_x", 0x24000000, 4, 1)],
                text=(0x08020000, 0x11000))


def test_image_coverage_lcov_and_summary(tmp_path):
    image = _image(tmp_path)
    cov = coverage.image_coverage(image, {0x8020100, 0x8020102}, elf.functions(image), _table())
    assert [(f.name, f.hit, f.path, f.line) for f in cov.functions] == [
        ("main", True, "/src/Core/Src/main.c", 10),
        ("helper", False, "/src/Core/Inc/util.h", 5),
        ("libc_memcpy", False, None, None)]
    assert cov.lines_hit == {"/src/Core/Src/main.c": {10, 11}}
    info = coverage.lcov(cov, "sim")
    assert "SF:/src/Core/Src/main.c\nFN:10,main\nFNDA:1,main\nFNF:1\nFNH:1\n" in info
    assert "DA:10,1\nDA:11,1\nDA:12,0\nLF:3\nLH:2\nend_of_record" in info
    assert "FNDA:0,helper" in info and "libc_memcpy" not in info
    tsv = coverage.functions_tsv(cov).splitlines()
    assert tsv[1] == "1\tmain\t0x08020100\t10\t/src/Core/Src/main.c\t10"
    assert tsv[3] == "0\tlibc_memcpy\t0x08030000\t16\t\t"
    rows = {r.module: (r.funcs_hit, r.funcs, r.lines_hit, r.lines) for r in coverage.summary_rows(cov)}
    assert rows == {"Src/main.c": (1, 1, 2, 3), "Inc/util.h": (0, 1, 0, 2),
                    "(no line info)": (0, 1, 0, 0)}
    text = coverage.summary_text([cov])
    assert text.startswith("ECU08.elf: functions 1/3 (33.3%), lines 2/5 (40.0%)")
    assert coverage.summary_text([cov], files=False).count("\n") == 1
    assert "| Src/main.c | 1/1 | 100.0% | 2/3 | 66.7% |" in coverage.summary_text([cov], markdown=True)


def test_function_coverage_without_line_info(tmp_path):
    image = _image(tmp_path)
    cov = coverage.image_coverage(image, {0x8030004}, elf.functions(image), LineTable())
    assert [f.name for f in cov.functions if f.hit] == ["libc_memcpy"]
    assert coverage.lcov(cov) == "\n"          # nothing with line info


def test_source_root_ignores_toolchain_headers():
    files = ["/vhil/fw/ams@main/Core/Src/main.c", "/vhil/fw/ams@main/Core/Inc/a.hpp",
             "/opt/arm-gnu/arm-none-eabi/include/c++/14.2.1/array"]
    root = coverage._common_dir(files)
    assert root == "/vhil/fw/ams@main/Core"
    assert coverage._relative(files[0], root) == "Src/main.c"
    assert coverage._relative(files[2], root) == files[2]


def test_collect_credits_each_address_to_its_image(tmp_path):
    app = _image(tmp_path)
    bl = _elf(tmp_path / "CAN_BL.elf", [("bl_main", 0x08000301, 8, 2)], text=(0x08000000, 0x8000))
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "s-1-0-ecu.tblog").write_text(
        "0x08000300:   4b04  \tldr\n0x08020100:   4b04  \tldr\n0x0803a220:  x\n")
    (raw / "s-1-0-ecu.json").write_text(json.dumps(
        {"system": "s", "board": "ecu", "log": "s-1-0-ecu.tblog", "images": [str(app), str(bl)]}))
    got = coverage.collect([raw])
    # 0x0803a220 is outside both images' code: dropped
    assert got == {app: {0x08020100}, bl: {0x08000300}}
