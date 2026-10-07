"""Host-only checks of the Sim helpers: probe output parsing, assertions and
pin lookup."""
from types import SimpleNamespace

import pytest

from vhil.sim import (BoardIO, Edge, Frame, assert_cadence, assert_period, intervals_us,
                      parse_edges, parse_frames)
from vhil.system import REPO, System


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


def test_assert_cadence_accepts_jitter_around_a_fixed_grid():
    assert_cadence(_frames([10_150, 20_154, 30_151, 40_150]), period_us=10_000, jitter_us=10)


def test_assert_cadence_catches_drift_that_each_interval_hides():
    """10 us long each period: within a 100 us tolerance interval by
    interval, but 300 us off the grid after 30 periods."""
    frames = _frames([k * 10_010 for k in range(31)])
    assert_period(frames, period_us=10_000, tolerance_us=100)
    with pytest.raises(AssertionError, match=r"by 0..300 us"):
        assert_cadence(frames, period_us=10_000, jitter_us=100)


def test_assert_cadence_catches_a_missing_item():
    with pytest.raises(AssertionError, match="grid"):
        assert_cadence(_frames([0, 10_000, 30_000, 40_000]), period_us=10_000, jitter_us=1000)


@pytest.mark.parametrize("system, board, pin, want", [
    ("ams", "ams", "PB6", ("sysbus.gpioPortB", 6)),       # AIR-
    ("ams", "ams", "PB7", ("sysbus.gpioPortB", 7)),       # precharge
    ("ams", "ams", "PF10", ("sysbus.gpioPortF", 10)),     # DASH_CHG: the role's GPIO
    ("ecu", "ecu", "PB6", ("sysbus.gpioPortB", 6)),       # DC-link discharge
    ("ecu", "ecu", "PB5", ("sysbus.gpioPortB", 5)),
])
def test_gpio_resolves_a_pin_through_the_catalogue(system, board, pin, want):
    io = BoardIO(SimpleNamespace(system=System(REPO / "systems" / f"{system}.yaml")), board)
    assert io.gpio(pin) == want


def test_gpio_refuses_an_analog_input():
    io = BoardIO(SimpleNamespace(system=System(REPO / "systems" / "ecu.yaml")), "ecu")
    with pytest.raises(ValueError, match="ecu.PF10 is analog, not a GPIO"):
        io.gpio("PF10")
