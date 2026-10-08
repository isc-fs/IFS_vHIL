"""AMS pack current captured by ADC3 + DMA1 (#204): the oversampled capture
of AMS `dev` (af07ec8), through the ADC3 and DMA1 models and DMAMUX1.

Firmware facts (IFS08-CE-AMS dev, 1508d13):
  ADC3: continuous, software start, 12-bit, 64x oversampling >> 2 (a Q4
    code: 16 x the 12-bit one), ASYNC_DIV2       main.c:416-436
  channel 3 (PF7/PF8) differential, 47.5 cycles  current_task.cpp:225-235
  kernel clock PLL2P = 96 MHz                    stm32h7xx_hal_msp.c:101-110
    -> a result every 64 x (47.5 + 12.5) / 48 MHz = 80 us, 12.5 kHz
                                                 ams_config.hpp:789-809
  DMA1 Stream1, DMAMUX1 request ADC3, P->M half-words, normal (one-shot)
    mode, 1024-sample capture buffers in AXI SRAM stm32h7xx_hal_msp.c:131-142,
                                                 current_task.cpp:90-95
  every 50 ms: stop, read NDTR, one single-ended oversampled read of OUT_P,
    restart into the other buffer; the capture's mean feeds the IIR; the
    measured rate goes to s_rate_hz             current_task.cpp:387-462
  no sample in a cycle -> no update -> CurrentStale (9) after IStaleMs 200
                                                 current_task.cpp:26-28
  mA from Q4: the 12-bit formula with 2^4 cancelling exactly
                                                 current_service.cpp:16-45
The test is for a build that captures by DMA; one that does not (`main`)
skips it.
"""
import pytest

from ams_car import Car, RUN
from test_ams_current import CURRENT_STALE, SETTLE_MS, _code_diff, _code_single, _legs, _pack_dA
from vhil import elf
from vhil.sim import Sim
from vhil.system import REPO

CURRENTS = 0x135
STREAM = 1                                    # DMA1 Stream1 (hdma_adc3)
CAPTURE = "_ZN12_GLOBAL__N_19s_captureE"      # uint16_t s_capture[2][1024]
RATE = "_ZN12_GLOBAL__N_19s_rate_hzE"
ADC_FAIL = "_ZN12_GLOBAL__N_118g_current_adc_failE"
CAPACITY = 1024
NOMINAL_HZ = 12_500                           # 48 MHz / (60 x 64)


@pytest.fixture(scope="module")
def ams(make_sim):
    sim = make_sim("ams")
    if "hdma_adc3" not in elf.symbols(sim.firmware["ams"]):
        pytest.skip("this AMS build samples ADC3 without DMA")
    sim.run_for(ms=3000)                      # past the 2 s boot grace
    return sim


def _transfers(sim) -> int:
    return int(sim.monitor(f"sysbus.dma1_h7 Transfers {STREAM}", board="ams").strip(), 0)


def _capture(sim, buf: int, n: int) -> list[int]:
    base, _ = elf.symbol(sim.firmware["ams"], CAPTURE)
    return [int(sim.monitor(f"sysbus ReadWord {base + 2 * (buf * CAPACITY + i):#x}",
                            board="ams").strip(), 16) for i in range(n)]


def test_the_capture_runs_at_the_oversampled_rate(ams):
    """One DMA transfer per 80 us result: 12.5 kHz, less the ~0.1 ms the
    task spends between captures each 50 ms. The firmware measures the same
    rate itself."""
    before = _transfers(ams)
    ams.run_for(ms=1000)
    moved = _transfers(ams) - before
    assert 12_000 <= moved <= NOMINAL_HZ, f"{moved} samples in 1 s"
    rate = ams.read_symbol("ams", RATE, size=4)
    assert 12_000 <= rate <= NOMINAL_HZ + 300, f"s_rate_hz {rate}"   # ms-tick quantised
    assert ams.read_symbol("ams", ADC_FAIL, size=4) == 0


def test_the_buffers_hold_the_oversampled_code(ams):
    """Each sample is the sum of 64 identical 12-bit conversions >> 2: 16 x
    the differential code, in both ping-pong buffers once both have been
    refilled under the new input."""
    io = ams.io("ams")
    vp, vn = _legs(40)
    io.set_voltage("PF7", vp)
    io.set_voltage("PF8", vn)
    ams.run_for(ms=200)                       # each buffer restarts every 100 ms
    want = 16 * _code_diff(vp, vn)
    for buf in (0, 1):
        assert set(_capture(ams, buf, 16)) == {want}, f"buffer {buf}"
    io.set_voltage("PF7", _legs(0)[0])
    io.set_voltage("PF8", _legs(0)[1])


def test_the_disconnect_check_reads_the_oversampled_leg(ams):
    """Between captures the task reads OUT_P single-ended through the same
    oversampler (current_task.cpp:259-269): a connected leg stays inside the
    700..2300 mV window, so no sensor fault, and nothing goes stale."""
    assert 700 <= _code_single(_legs(0)[0]) * 3300 // 4095 <= 2300   # the stimulus
    ams.run_for(ms=1000)
    assert ams.read_symbol("ams", "g_state_telemetry") == 0
    assert ams.read_symbol("ams", "g_fault_reason_telemetry") == 0


@pytest.mark.parametrize("amps", [60, -30])
def test_run_reports_the_stimulus_current(images, amps):
    """The AMS reaches Run through Precharge and, with the pack current
    stimulus applied, puts the IIR-settled reading on 0x135 within the
    firmware's own quantisation; it never goes CurrentStale."""
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        if "hdma_adc3" not in elf.symbols(sim.firmware["ams"]):
            pytest.skip("this AMS build samples ADC3 without DMA")
        sim.wait_for_app()
        sim.run_for(ms=3000)
        _run_at(Car(sim), amps)


def _run_at(car, amps):
    sim = car.sim
    car.to_run()
    vp, vn = _legs(amps)
    car.io.set_voltage("PF7", vp)
    car.io.set_voltage("PF8", vn)
    since = sim.run_for(ms=SETTLE_MS)
    assert car.state() == RUN, f"state {car.state()}, reason {car.reason()}"
    assert car.reason() != CURRENT_STALE
    accu = int.from_bytes(car.can.last(CURRENTS).data[0:2], "big", signed=True)
    expected = _pack_dA(vp, vn)
    assert abs(accu - expected) <= 5, f"{amps} A in: 0x135 accu {accu} dA, expected {expected:.0f}"
    assert car.can.count(CURRENTS, since_us=since - 1_000_000) >= 15, "0x135 not at 20 Hz"
