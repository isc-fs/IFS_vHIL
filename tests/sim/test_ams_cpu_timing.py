"""The AMS's core runs its code at the chip's rate (docs/cpu-timing.md, #246).

Window: the LTC6811 wake pulse, LTC6820 CS (PB9, systems/ams.yaml) held
low for delay_us(20), one pulse per IC of the 10-IC chain at boot
(ltc6820.cpp Bus::wakeup, app_init_task.cpp:150; ams_config.hpp
LtcChainLength). Up to AMS dev 2026-10-09, delay_us is a volatile NOP loop of
8 instructions an iteration, 150 iterations a us (ltc6820.cpp:41-47), so 3000
iterations: 24130 instructions, the loop over two flash words.

IFS08-CE-AMS#637 times delay_us on the DWT cycle counter and spaces the
pulses 500 us apart (LTC6811 "Waking a Daisy Chain, Method 2"): the window
then lasts 20 us whatever the rate, so it no longer measures the core's
speed. On such a build the wake tests check the firmware's intent instead,
20 us low and 500 us high in virtual time, which is also a check of the
cycle counter's model (platforms/cpus/stm32h733.repl `dwt`). That build is
told by its out-of-line delay_us (the old one is always_inline).
"""
import pytest

from cpu_timing import CHIP_TOLERANCE, INSTRUCTION_TOLERANCE, Window, mips, widths
from vhil import elf
from vhil.system import System, REPO

BOARD = "ams"
WAKE = Window("sysbus.gpioPortB", 9, False, instructions=24130, chip_us=None)
CHAIN = 10                        # ams_config.hpp LtcChainLength
# IFS08-CE-AMS#637 ltc6820.cpp: WakePulseUs, WakeGapUs, delay_us.
PULSE_US, GAP_US = 20, 500
CYCCNT_DELAY = "_ZN3ams7ltc682012_GLOBAL__N_18delay_usEm"
# What the window spends besides the wait: the GPIO write's return, the
# cycle-counter check (a 16-pass loop) and the poll loop's last pass. Well
# under a us at any rate the catalogue gives.
OVERHEAD_US = 1.0


@pytest.fixture(scope="module")
def booted(make_sim):
    sim = make_sim("ams", wait_for_app=False)   # the wake train runs at boot
    pin = sim.io(BOARD).watch(WAKE.port, WAKE.pin)
    sim.wait_for_app()
    sim.run_for(ms=500)
    cyccnt = CYCCNT_DELAY in elf.symbols(sim.firmware[BOARD])
    return sim, sim.io(BOARD).edges(pin), cyccnt


def _train(edges):
    """The boot wake train: CHAIN pulses from the first fall, as
    (low, high) widths in executed instructions, the last high excluded."""
    first = next(i for i, e in enumerate(edges) if not e.level and i > 0)
    train = edges[first:first + 2 * CHAIN]
    lows = [b.instructions - a.instructions for a, b in zip(train[0::2], train[1::2])]
    highs = [b.instructions - a.instructions for a, b in zip(train[1::2], train[2::2])]
    return lows, highs


def test_the_core_runs_at_the_firmware_rate(booted):
    sim, _, _ = booted
    rate = System(REPO / "systems" / "ams.yaml").boards[BOARD].firmware["cpu"]["mips"]
    assert mips(sim, BOARD) == rate


def test_the_wake_pulse_runs_the_pinned_instructions(booted):
    _, edges, cyccnt = booted
    if cyccnt:
        pytest.skip("delay_us is timed on CYCCNT (IFS08-CE-AMS#637): no CPU-rate window")
    pulses = widths(edges, WAKE.level)
    wake = [w for w in pulses if w > WAKE.instructions / 2]
    assert len(wake) >= CHAIN, f"no wake train on PB9: {pulses[:20]}"
    assert min(wake) == pytest.approx(WAKE.instructions, rel=INSTRUCTION_TOLERANCE), \
        "delay_us changed: redo the AMS rate bound and the chip measurement (docs/cpu-timing.md)"


def test_the_wake_train_is_20_us_low_500_us_high(booted):
    sim, edges, cyccnt = booted
    if not cyccnt:
        pytest.skip("delay_us is a NOP loop on this AMS build (before IFS08-CE-AMS#637)")
    rate = mips(sim, BOARD)
    lows, highs = _train(edges)
    assert len(lows) == CHAIN and len(highs) == CHAIN - 1, f"no wake train on PB9: {edges[:24]}"
    # Busy-waits only, no sleep: an edge's instructions over the rate is its
    # virtual time, to the instruction.
    for name, got, want in [("low", lows, PULSE_US), ("high", highs, GAP_US)]:
        us = [n / rate for n in got]
        assert all(want <= u <= want + OVERHEAD_US for u in us), f"PB9 {name} (us): {us}"


def test_virtual_time_follows_the_rate(booted):
    sim, edges, _ = booted
    # The wake train's first fall to its last rise: busy-waits only, no
    # sleep, so virtual time is instructions over the rate, to a quantum.
    train = [e for e in edges if e.t_us >= edges[1].t_us][: 2 * CHAIN]
    first, last = train[0], train[-1]
    expected = (last.instructions - first.instructions) / mips(sim, BOARD)
    assert abs((last.t_us - first.t_us) - expected) <= sim.system.sync_quantum_us() + 1


def test_the_wake_pulse_is_as_long_as_on_the_chip(booted):
    _, _, cyccnt = booted
    if cyccnt:
        pytest.skip("delay_us is timed on CYCCNT (IFS08-CE-AMS#637): no CPU-rate window")
    if WAKE.chip_us is None:
        pytest.skip("no chip measurement of the wake pulse yet (#246)")
    sim, _, _ = booted
    assert WAKE.instructions / mips(sim, BOARD) == pytest.approx(WAKE.chip_us, rel=CHIP_TOLERANCE)
