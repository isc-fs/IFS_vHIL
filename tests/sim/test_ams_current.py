"""AMS current sensing through the H73x ADC3 model (#19).

Firmware facts (IFS08-CE-AMS):
  pack current: differential ADC3 INP3/INN3 = PF7/PF8, Bourns SSA-2;
    mA = (raw - CurrentZeroCount) * 2 * Vref / 4095 * 10 / CurrentMvPerAmpe1
                                                   current_service.cpp:34-40
  DCDC current: single-ended ADC3 INP11 = PC1, ACS758;
    mA = (raw * Vref / 4095 - DcdcCurrentZeroMv) * 10 / DcdcCurrentMvPerAmpe1
                                                   current_service.cpp:55-62
  constants                                         ams_config.hpp:733-740
  both sampled every 50 ms, IIR-filtered (shift 4), sent as BE i16
  deciamps [pack | dcdc] on 0x135                   acu_tx_encoders.hpp:165-170
  CurrentStale = fault reason 9                     safety_predicates.hpp:53
  0x6C0 byte 6 = fault reason once 0x7F0 DE AD BE EF arms the pit stream
                                                   pit_fsm_status.def, ams_config.hpp:588
"""
import pytest

VREF = 3.3
CURRENT_ZERO_COUNT = 2054
CURRENT_MV_PER_A_E1 = 46
DCDC_ZERO_MV = 1650
DCDC_MV_PER_A_E1 = 264

CURRENTS = 0x135
PIT_ARM, PIT_FSM = 0x7F0, 0x6C0
CURRENT_STALE = 9

COMMON_MODE_V = 1.44       # the sensor's output common mode (ams_config.hpp:692)
SETTLE_MS = 8000           # 10 time constants of the 800 ms IIR (residue < 0.01 %)


def _code_diff(vp, vn):
    """What the ADC3 model returns for a 12-bit differential conversion."""
    return max(0, min(4095, round((1 + (vp - vn) / VREF) * 2048)))


def _code_single(v):
    return max(0, min(4095, round(v / VREF * 4095)))


def _pack_dA(vp, vn):
    delta = _code_diff(vp, vn) - CURRENT_ZERO_COUNT
    diff_uV = int(delta * 2 * 3300 * 1000 / 4095)
    return int(diff_uV * 10 / CURRENT_MV_PER_A_E1) / 100


def _dcdc_dA(v):
    v_uV = _code_single(v) * 3300 * 1000 // 4095
    return int((v_uV - DCDC_ZERO_MV * 1000) * 10 / DCDC_MV_PER_A_E1) / 100


def _currents(frame):
    accu = int.from_bytes(frame.data[0:2], "big", signed=True)
    dcdc = int.from_bytes(frame.data[2:4], "big", signed=True)
    return accu, dcdc


@pytest.fixture(scope="module")
def ams(make_sim):
    sim = make_sim("ams")
    sim.io("ams").set_voltage("PF7", COMMON_MODE_V)
    sim.io("ams").set_voltage("PF8", COMMON_MODE_V)
    sim.io("ams").set_voltage("PC1", DCDC_ZERO_MV / 1000)
    return sim


@pytest.mark.parametrize("amps", [0, 100, -50])
def test_pack_current_reaches_0x135(ams, amps):
    io = ams.io("ams")
    half = amps * CURRENT_MV_PER_A_E1 / 10 / 1000 / 2      # volts per leg
    vp, vn = COMMON_MODE_V + half, COMMON_MODE_V - half
    io.set_voltage("PF7", vp)
    io.set_voltage("PF8", vn)
    since = ams.run_for(ms=SETTLE_MS)
    accu, _ = _currents(ams.can("can_acu").last(CURRENTS))
    expected = _pack_dA(vp, vn)
    # 0.5 A: the firmware's IIR drops the low 4 bits of every step.
    assert abs(accu - expected) <= 5, f"{amps} A in: 0x135 accu {accu} dA, expected {expected:.0f}"
    assert ams.can("can_acu").count(CURRENTS, since_us=since - 1_000_000) >= 15, "0x135 not at 20 Hz"


def test_dcdc_current_reaches_0x135(ams):
    io = ams.io("ams")
    volts = DCDC_ZERO_MV / 1000 + 10 * DCDC_MV_PER_A_E1 / 10 / 1000   # +10 A
    io.set_voltage("PC1", volts)
    ams.run_for(ms=SETTLE_MS)
    _, dcdc = _currents(ams.can("can_acu").last(CURRENTS))
    expected = _dcdc_dA(volts)
    assert abs(dcdc - expected) <= 5, f"0x135 dcdc {dcdc} dA, expected {expected:.0f}"


def test_current_never_goes_stale(ams):
    """Every ADC3 read now succeeds, so the AMS never faults on CurrentStale."""
    can = ams.can("can_acu")
    can.send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    t0 = ams.run_for(ms=1500)
    status = can.frames(PIT_FSM, since_us=t0 - 1_000_000)
    assert status, "no 0x6C0 after arming the pit stream"
    reasons = {f.data[6] for f in status}
    assert CURRENT_STALE not in reasons, f"fault reasons seen: {sorted(reasons)}"
