"""ECU pedal inputs through the H73x ADC3 model (#19).

Firmware facts (IFS08-CE-ECU):
  single-ended ADC3, 12-bit, single conversion       adc.c:46-60
  brake IN3 = PF7, APPS1 IN7 = PF8, APPS2 IN2 = PF9  io_signals.cpp:37-48
  0x7E0 DE AD BE EF on the ACU bus arms the pit stream  can_rx_task.cpp:78-88
  0x701 PitDiag_pedals: apps1_raw, apps2_raw, brake_raw, BE u16, every 100 ms
                                                     pit_diag_pedals.def, ecu_config.hpp:24
The raw fields are the ADC codes unfiltered, so they are asserted to the
code but for the ADC noise the ECU role carries (#326, 0.42 codes rms, its
draw bounded at 4.9 rms = 2.06 codes, then rounded): within NOISE_CODES = 3
codes of the ideal one.
"""
import pytest

# Impact selection (ci/impact.yaml, docs/ci.md): boards, systems, subsystems.
pytestmark = [pytest.mark.ecu, pytest.mark.system_ecu, pytest.mark.adc]

VREF = 3.3
PIT_ARM, PEDALS = 0x7E0, 0x701
NOISE_CODES = 3      # 4.9 x 0.42 = 2.06, + rounding: catalog/boards/mainlite.yaml ecu adc_single_ended


def _code(volts):
    return max(0, min(4095, round(volts / VREF * 4095)))


@pytest.fixture(scope="module")
def ecu(make_sim):
    sim = make_sim("ecu")
    sim.run_for(ms=200)
    sim.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    sim.run_for(ms=200)
    return sim


@pytest.mark.parametrize("apps1, apps2, brake", [
    (0.50, 0.45, 0.30),
    (2.80, 2.50, 1.90),
    (0.00, 3.30, 3.30),
])
def test_pedal_voltages_reach_0x701(ecu, apps1, apps2, brake):
    io = ecu.io("ecu")
    io.set_voltage("PF8", apps1)
    io.set_voltage("PF9", apps2)
    io.set_voltage("PF7", brake)
    since = ecu.run_for(ms=300)
    frame = ecu.can("can_acu").last(PEDALS)
    assert frame is not None and frame.t_us >= since - 150_000, "no fresh 0x701"
    got = [int.from_bytes(frame.data[i:i + 2], "big") for i in (0, 2, 4)]
    want = [_code(apps1), _code(apps2), _code(brake)]
    assert all(abs(g - w) <= NOISE_CODES for g, w in zip(got, want)), (got, want)


def test_pedal_stream_every_100_ms(ecu):
    """ControlTask's pit-diag tick: on a 100 ms grid, within a kernel tick
    (ECU FreeRTOS tick: configTICK_RATE_HZ 1000, FreeRTOSConfig.h:67)."""
    from vhil.sim import assert_cadence
    t = ecu.run_for(ms=1000)
    assert_cadence(ecu.can("can_acu").frames(PEDALS, since_us=t - 1_000_000),
                   period_us=100_000, jitter_us=1000, min_count=9)
