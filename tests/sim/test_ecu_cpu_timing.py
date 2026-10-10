"""The ECU's core runs its code at the chip's rate (docs/cpu-timing.md, #246).

Window: a high half-period of the nRF24's bit-banged SCK (PA5,
systems/ecu.yaml): HAL_GPIO_WritePin(SCK, 1), NRF24_BitBangDelay, the MISO
read and HAL_GPIO_WritePin(SCK, 0) (nrf24.c NRF24_BitBangTransfer).
NRF24_BitBangDelay is a volatile NOP loop of 400 iterations, 7 instructions
each (nrf24.c:359-365): 2875 instructions in ECU dev 2026-10-09, the loop
over two flash words. TelemetryTask sends the radio snapshot every 200 ms
(telemetry_task.cpp:35).
"""
import pytest

from cpu_timing import CHIP_TOLERANCE, INSTRUCTION_TOLERANCE, Window, mips, widths
from vhil.system import System, REPO

BOARD = "ecu"
SCK_HIGH = Window("sysbus.gpioPortA", 5, True, instructions=2875, chip_us=None)
CSN = ("sysbus.gpioPortB", 0)     # the nRF24's chip select, low through a transfer


@pytest.fixture(scope="module")
def run(make_sim):
    sim = make_sim("ecu")
    io = sim.io(BOARD)
    pin, csn = io.watch(SCK_HIGH.port, SCK_HIGH.pin), io.watch(*CSN)
    t0 = sim.now_us()
    sim.run_for(ms=500)
    return sim, io.edges(pin, since_us=t0), io.edges(csn, since_us=t0)


def test_the_core_runs_at_the_firmware_rate(run):
    sim, _, _ = run
    rate = System(REPO / "systems" / "ecu.yaml").boards[BOARD].firmware["cpu"]["mips"]
    assert mips(sim, BOARD) == rate


def test_the_sck_half_period_runs_the_pinned_instructions(run):
    _, edges, _ = run
    highs = widths(edges, SCK_HIGH.level)
    assert len(highs) > 1000, "the radio sent no snapshot"
    assert min(highs) == pytest.approx(SCK_HIGH.instructions, rel=INSTRUCTION_TOLERANCE), \
        "NRF24_BitBangDelay changed: redo the ECU rate bound and the chip measurement (docs/cpu-timing.md)"


def test_virtual_time_follows_the_rate(run):
    sim, edges, csn = run
    # Inside a transfer (CSN low) the ECU bit-bangs without yielding, so the
    # core never sleeps (its idle task WFIs, freertos.c vApplicationIdleHook):
    # virtual time is the instructions run over the rate, to a quantum. The
    # longest transfer is a 33-byte payload write, about 2.3 M instructions.
    lows = [(a.instructions, b.instructions) for a, b in zip(csn, csn[1:])
            if not a.level and b.level]
    start, end = max(lows, key=lambda w: w[1] - w[0])
    inside = [e for e in edges if start <= e.instructions <= end]
    first, last = inside[0], inside[-1]
    expected = (last.instructions - first.instructions) / mips(sim, BOARD)
    assert abs((last.t_us - first.t_us) - expected) <= sim.system.sync_quantum_us() + 1


def test_the_sck_half_period_is_as_long_as_on_the_chip(run):
    if SCK_HIGH.chip_us is None:
        pytest.skip("no chip measurement of the SCK half-period yet (#246)")
    sim, _, _ = run
    assert SCK_HIGH.instructions / mips(sim, BOARD) == pytest.approx(SCK_HIGH.chip_us,
                                                                     rel=CHIP_TOLERANCE)
