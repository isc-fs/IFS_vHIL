"""The AMS's core runs at its catalogue rate, and its isoSPI wake train keeps
the firmware's timing (docs/cpu-timing.md, #246).

Since IFS08-CE-AMS#637 the AMS runs with the I-cache on, and delay_us is timed
on the DWT cycle counter, not a NOP loop. So the wake train is no CPU-rate
window any more: its pulses last what the firmware asks whatever the rate,
and the rate (528 MIPS) is pinned on the Cortex-M7 pipeline instead (the
catalogue entry, docs/cpu-timing.md "The AMS: I-cache on, no window left").

The train is LTC6820 CS (PB9, systems/ams.yaml) low for delay_us(20), then
high for delay_us(500), once per IC of the 10-IC chain at boot (ltc6820.cpp
WakePulseUs, WakeGapUs, Bus::wakeup; app_init_task.cpp:150; ams_config.hpp
LtcChainLength), after the LTC6811 datasheet's "Waking a Daisy Chain,
Method 2". The widths check the firmware's intent and the cycle counter's
model (platforms/cpus/stm32h733.repl `dwt`).
"""
import pytest

from cpu_timing import mips
from vhil.system import System, REPO

BOARD = "ams"
CS = ("sysbus.gpioPortB", 9)
CHAIN = 10                        # ams_config.hpp LtcChainLength
PULSE_US, GAP_US = 20, 500        # ltc6820.cpp WakePulseUs, WakeGapUs
# What a width spends besides its wait: the GPIO write's return, the cycle
# counter's check (a 16-pass loop) and the poll loop's last pass. Well under a
# us at the catalogue's rate.
OVERHEAD_US = 1.0
# The train measured on the chip (isc-fs/IFS_HIL#154): (low us, high us), the
# minimum over a boot. None until it is.
CHIP_WAKE_US = None
CHIP_TOLERANCE_US = 1.0


@pytest.fixture(scope="module")
def booted(make_sim):
    sim = make_sim("ams", wait_for_app=False)   # the wake train runs at boot
    pin = sim.io(BOARD).watch(*CS)
    sim.wait_for_app()
    sim.run_for(ms=500)
    return sim, sim.io(BOARD).edges(pin)


def _train_us(sim, edges):
    """The boot wake train's CHAIN low widths and the CHAIN - 1 high widths
    between them, in virtual us: busy-waits only, no sleep, so an edge's
    executed instructions over the rate is its virtual time, to the
    instruction."""
    rate = mips(sim, BOARD)
    first = next(i for i, e in enumerate(edges) if not e.level and i > 0)
    train = edges[first:first + 2 * CHAIN]
    lows = [(b.instructions - a.instructions) / rate for a, b in zip(train[0::2], train[1::2])]
    highs = [(b.instructions - a.instructions) / rate for a, b in zip(train[1::2], train[2::2])]
    assert len(lows) == CHAIN and len(highs) == CHAIN - 1, f"no wake train on PB9: {edges[:24]}"
    return lows, highs


def test_the_core_runs_at_the_firmware_rate(booted):
    sim, _ = booted
    rate = System(REPO / "systems" / "ams.yaml").boards[BOARD].firmware["cpu"]["mips"]
    assert mips(sim, BOARD) == rate


def test_the_wake_train_is_20_us_low_500_us_high(booted):
    lows, highs = _train_us(*booted)
    assert all(PULSE_US <= u <= PULSE_US + OVERHEAD_US for u in lows), f"PB9 low (us): {lows}"
    assert all(GAP_US <= u <= GAP_US + OVERHEAD_US for u in highs), f"PB9 high (us): {highs}"


def test_virtual_time_follows_the_rate(booted):
    sim, edges = booted
    # The wake train's first fall to its last rise: busy-waits only, no
    # sleep, so virtual time is instructions over the rate, to a quantum.
    train = [e for e in edges if e.t_us >= edges[1].t_us][: 2 * CHAIN]
    first, last = train[0], train[-1]
    expected = (last.instructions - first.instructions) / mips(sim, BOARD)
    assert abs((last.t_us - first.t_us) - expected) <= sim.system.sync_quantum_us() + 1


def test_the_wake_train_is_as_long_as_on_the_chip(booted):
    if CHIP_WAKE_US is None:
        pytest.skip("no chip measurement of the wake train yet (isc-fs/IFS_HIL#154)")
    lows, highs = _train_us(*booted)
    chip_low, chip_high = CHIP_WAKE_US
    assert min(lows) == pytest.approx(chip_low, abs=CHIP_TOLERANCE_US)
    assert min(highs) == pytest.approx(chip_high, abs=CHIP_TOLERANCE_US)
