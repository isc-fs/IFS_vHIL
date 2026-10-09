"""The AMS's core runs its code at the chip's rate (docs/cpu-timing.md, #246).

Window: the LTC6811 wake pulse, LTC6820 CS (PB9, systems/ams.yaml) held
low for delay_us(20), one pulse per IC of the 10-IC chain at boot
(ltc6820.cpp Bus::wakeup, app_init_task.cpp:150; ams_config.hpp
LtcChainLength). delay_us is a volatile NOP loop of 8 instructions an
iteration, 150 iterations a us (ltc6820.cpp:41-47), so 3000 iterations:
24130 instructions in AMS dev 2026-10-09, the loop over two flash words.
"""
import pytest

from cpu_timing import CHIP_TOLERANCE, INSTRUCTION_TOLERANCE, Window, mips, widths
from vhil.system import System, REPO

BOARD = "ams"
WAKE = Window("sysbus.gpioPortB", 9, False, instructions=24130, chip_us=None)
CHAIN = 10                        # ams_config.hpp LtcChainLength


@pytest.fixture(scope="module")
def booted(make_sim):
    sim = make_sim("ams", wait_for_app=False)   # the wake train runs at boot
    pin = sim.io(BOARD).watch(WAKE.port, WAKE.pin)
    sim.wait_for_app()
    sim.run_for(ms=500)
    return sim, sim.io(BOARD).edges(pin)


def test_the_core_runs_at_the_firmware_rate(booted):
    sim, _ = booted
    rate = System(REPO / "systems" / "ams.yaml").boards[BOARD].firmware["cpu"]["mips"]
    assert mips(sim, BOARD) == rate


def test_the_wake_pulse_runs_the_pinned_instructions(booted):
    _, edges = booted
    pulses = widths(edges, WAKE.level)
    wake = [w for w in pulses if w > WAKE.instructions / 2]
    assert len(wake) >= CHAIN, f"no wake train on PB9: {pulses[:20]}"
    assert min(wake) == pytest.approx(WAKE.instructions, rel=INSTRUCTION_TOLERANCE), \
        "delay_us changed: redo the AMS rate bound and the chip measurement (docs/cpu-timing.md)"


def test_virtual_time_follows_the_rate(booted):
    sim, edges = booted
    # The wake train's first fall to its last rise: busy-waits only, no
    # sleep, so virtual time is instructions over the rate, to a quantum.
    train = [e for e in edges if e.t_us >= edges[1].t_us][: 2 * CHAIN]
    first, last = train[0], train[-1]
    expected = (last.instructions - first.instructions) / mips(sim, BOARD)
    assert abs((last.t_us - first.t_us) - expected) <= sim.system.sync_quantum_us() + 1


def test_the_wake_pulse_is_as_long_as_on_the_chip(booted):
    if WAKE.chip_us is None:
        pytest.skip("no chip measurement of the wake pulse yet (#246)")
    sim, _ = booted
    assert WAKE.instructions / mips(sim, BOARD) == pytest.approx(WAKE.chip_us, rel=CHIP_TOLERANCE)
