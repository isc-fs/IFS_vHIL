"""ECU pedal inputs through the H73x ADC3 model (#19).

Firmware facts (IFS08-CE-ECU):
  single-ended ADC3, 12-bit, single conversion       adc.c:46-60
  brake IN3 = PF7, APPS1 IN7 = PF8, APPS2 IN2 = PF9  io_signals.cpp:37-48
  0x7E0 DE AD BE EF on the ACU bus arms the pit stream  can_rx_task.cpp:78-88
  0x701 PitDiag_pedals: apps1_raw, apps2_raw, brake_raw, BE u16, every 100 ms
                                                     pit_diag_pedals.def, ecu_config.hpp:24
The raw fields are the ADC codes unfiltered, so they are asserted exactly.
"""
import pytest

VREF = 3.3
PIT_ARM, PEDALS = 0x7E0, 0x701


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
def test_pedal_voltages_reach_0x701_exactly(ecu, apps1, apps2, brake):
    io = ecu.io("ecu")
    io.set_voltage("PF8", apps1)
    io.set_voltage("PF9", apps2)
    io.set_voltage("PF7", brake)
    since = ecu.run_for(ms=300)
    frame = ecu.can("can_acu").last(PEDALS)
    assert frame is not None and frame.t_us >= since - 150_000, "no fresh 0x701"
    got = [int.from_bytes(frame.data[i:i + 2], "big") for i in (0, 2, 4)]
    assert got == [_code(apps1), _code(apps2), _code(brake)]


def test_pedal_stream_every_100_ms(ecu):
    from vhil.sim import assert_period
    t = ecu.run_for(ms=1000)
    assert_period(ecu.can("can_acu").frames(PEDALS, since_us=t - 1_000_000),
                  period_us=100_000, tolerance_us=0, min_count=9)
