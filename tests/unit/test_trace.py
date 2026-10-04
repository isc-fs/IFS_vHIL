"""vhil/trace.py: the ring VhilTrace returns, symbolised and collapsed."""
from vhil import trace
from vhil.trace import Block, Step, collapse

from tests.unit.test_elf import _elf


def test_parse_ring_skips_noise():
    text = "0x80389B0 4\n0x80389AC 1\n\nsomething else\n0x8038A54 12\n"
    assert trace.parse_ring(text) == [Block(0x80389B0, 4), Block(0x80389AC, 1),
                                      Block(0x8038A54, 12)]


def test_collapse_runs_and_repeated_groups():
    seq = ["main", "main", "a", "b", "a", "b", "a", "b", "c"]
    assert collapse(seq) == [Step(("main",), 1, 2), Step(("a", "b"), 3, 6), Step(("c",), 1, 1)]


def test_collapse_prefers_the_group_that_swallows_most():
    # idle -> tick -> idle -> tick ... with a one-off in the middle
    seq = ["idle", "tick"] * 4 + ["isr"] + ["idle", "tick", "sw"] * 3
    steps = collapse(seq)
    assert steps == [Step(("idle", "tick"), 4, 8), Step(("isr",), 1, 1),
                     Step(("idle", "tick", "sw"), 3, 9)]


def test_collapse_empty_and_single():
    assert collapse([]) == []
    assert collapse(["x"]) == [Step(("x",), 1, 1)]


def test_step_text():
    assert str(Step(("main",), 1, 1)) == "main (1 block)"
    assert str(Step(("main",), 1, 5)) == "main (5 blocks)"
    assert str(Step(("a", "b"), 3, 6)) == "[a -> b] x 3 (6 blocks)"


def test_symbolizer_over_several_images(tmp_path):
    app = _elf(tmp_path / "app.elf", [("main", 0x08020101, 0x40, 2)])
    bl = _elf(tmp_path / "bl.elf", [("bl_main", 0x08000201, 0x20, 2)])
    name = trace.symbolizer([app, bl])
    assert name(0x08020100) == "main"
    assert name(0x08020110) == "main+0x10"
    assert name(0x08000204) == "bl_main+0x4"
    assert name(0x08100000) == "0x08100000"


def test_collapsed_text_tail(tmp_path):
    app = _elf(tmp_path / "app.elf", [("f", 0x08020101, 0x10, 2), ("g", 0x08020201, 0x10, 2)])
    name = trace.symbolizer([app])
    blocks = [Block(0x08020100, 2), Block(0x08020200, 1)] * 3 + [Block(0x08020104, 1)]
    assert trace.collapsed(blocks, name) == ["[f -> g] x 3 (6 blocks)", "f (1 block)"]
    assert trace.collapsed(blocks, name, tail=1) == ["... 1 earlier steps", "f (1 block)"]
    assert trace.format_ring(blocks[:1], name) == ["0x08020100    2  f"]
