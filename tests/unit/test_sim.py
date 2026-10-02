"""Host-only checks of the Sim helpers: probe output parsing and assertions."""
import pytest

from vhil.sim import Edge, Frame, assert_period, intervals_us, parse_edges, parse_frames


def test_parse_frames_reads_probe_lines():
    text = "15000 0x100 0 0102\n25000 0x18FF50E5 1 \n\n(monitor) \n"
    assert parse_frames(text) == [
        Frame(15000, 0x100, False, b"\x01\x02"),
        Frame(25000, 0x18FF50E5, True, b""),
    ]


def test_parse_edges_reads_probe_lines():
    text = "14743 sysbus.gpioPortD:14 1\n31000 sysbus.gpioPortD:14 0\n"
    assert parse_edges(text) == [
        Edge(14743, "sysbus.gpioPortD:14", True),
        Edge(31000, "sysbus.gpioPortD:14", False),
    ]


def _frames(times):
    return [Frame(t, 0x100, False, b"") for t in times]


def test_assert_period_accepts_an_exact_cadence():
    frames = _frames([10_000, 20_000, 30_000, 40_000])
    assert intervals_us(frames) == [10_000] * 3
    assert_period(frames, period_us=10_000, tolerance_us=0)


def test_assert_period_names_the_first_bad_interval():
    with pytest.raises(AssertionError, match=r"#1 = 10500 us"):
        assert_period(_frames([0, 10_000, 20_500, 30_500]), period_us=10_000, tolerance_us=100)


def test_assert_period_needs_enough_items():
    with pytest.raises(AssertionError, match="only 2 items"):
        assert_period(_frames([0, 10_000]), period_us=10_000, tolerance_us=0)
