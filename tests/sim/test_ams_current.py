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
  CurrentStale = fault reason 9, once the last sample is older than
    IStaleMs = 200 (safety_predicates.hpp:241, ams_config.hpp:149)
  CurrentOverLimit (10): |filtered| > CurrentMaxMa = 185 A, no debounce
    beyond the IIR itself (filtered -= f >> 4; += mA >> 4 every 50 ms;
    current_service.cpp:84-89, safety_predicates.hpp:244)
  CurrentSensorFault (8): the OUT_P leg outside 700..2300 mV for
    CurrentDisconnectConfirm = 3 reads in a row (current_task.cpp:202-212)
  Error latches for the boot (state_machine.hpp step: sticky ERROR)
  0x6C0 byte 6 = fault reason once 0x7F0 DE AD BE EF arms the pit stream
                                                   pit_fsm_status.def, ams_config.hpp:588
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

VREF = 3.3
CURRENT_ZERO_COUNT = 2054
CURRENT_MV_PER_A_E1 = 46
DCDC_ZERO_MV = 1650
DCDC_MV_PER_A_E1 = 264

CURRENTS = 0x135
PIT_ARM, PIT_FSM = 0x7F0, 0x6C0
CURRENT_STALE = 9
SENSOR_FAULT, OVER_LIMIT = 8, 10
CURRENT_MAX_MA = 185_000
ERROR = 5

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


def _pack_mA(vp, vn):
    """adc_to_mA exactly (current_service.cpp:34-40), C truncation."""
    delta = _code_diff(vp, vn) - CURRENT_ZERO_COUNT
    diff_uV = int(delta * 2 * 3300 * 1000 / 4095)
    return int(diff_uV * 10 / CURRENT_MV_PER_A_E1)


def _legs(amps):
    half = amps * CURRENT_MV_PER_A_E1 / 10 / 1000 / 2
    return COMMON_MODE_V + half, COMMON_MODE_V - half


def _samples_to_trip(f0, mA, limit=400):
    """50 ms samples until the firmware's IIR passes CurrentMaxMa, or None."""
    f = f0
    for n in range(1, limit + 1):
        f -= f >> 4
        f += mA >> 4
        if abs(f) > CURRENT_MAX_MA:
            return n
    return None


def _dcdc_dA(v):
    v_uV = _code_single(v) * 3300 * 1000 // 4095
    return int((v_uV - DCDC_ZERO_MV * 1000) * 10 / DCDC_MV_PER_A_E1) / 100


def _currents(frame):
    accu = int.from_bytes(frame.data[0:2], "big", signed=True)
    dcdc = int.from_bytes(frame.data[2:4], "big", signed=True)
    return accu, dcdc


@pytest.fixture(scope="module")
def ams(make_sim):
    # systems/ams.yaml fits the car's sensors at 0 A (ssa-2-250a on PF7/PF8,
    # acs758lcb-050b on PC1); tests drive the pins from there.
    return make_sim("ams")


def test_sensors_keep_the_ams_out_of_error(ams):
    """With its sensors fitted at 0 A, the AMS passes its boot grace without a
    current fault (0 V legs read as a disconnected sensor), and reads ~0 A
    up to the firmware's zero calibration (CurrentZeroCount 2054 vs the
    ideal 2048: about -2 A)."""
    can = ams.can("can_acu")
    ams.run_for(ms=1000)                           # booted: listening for the arm
    can.send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    t0 = ams.run_for(ms=3000)                      # past the 2 s boot grace
    status = can.frames(PIT_FSM, since_us=t0 - 1_000_000)
    assert status, "no 0x6C0 after arming the pit stream"
    assert {f.data[0] for f in status} == {0}, "AMS left Start"
    assert {f.data[6] for f in status} == {0}, f"fault reasons {sorted({f.data[6] for f in status})}"
    accu, dcdc = _currents(can.last(CURRENTS))
    assert abs(accu - _pack_dA(COMMON_MODE_V, COMMON_MODE_V)) <= 5 and abs(dcdc) <= 5


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


@pytest.fixture
def fresh(firmware):
    """A booted AMS past its grace, at 0 A, for tests that latch Error."""
    with Sim(REPO / "systems" / "ams.yaml", {"ams": firmware("ams")}) as sim:
        sim.run_for(ms=3000)
        yield sim


def _ms_to_error(sim, limit_ms, step_ms=10):
    for elapsed in range(step_ms, limit_ms + step_ms, step_ms):
        sim.run_for(ms=step_ms)
        if sim.read_symbol("ams", "g_state_telemetry") == ERROR:
            return elapsed
    return None


def _drive(sim, amps):
    vp, vn = _legs(amps)
    io = sim.io("ams")
    io.set_voltage("PF7", vp)
    io.set_voltage("PF8", vn)
    return _pack_mA(vp, vn)


def test_just_under_the_limit_never_trips(fresh):
    """J-100: 180 A held for 8 s (ten IIR time constants) stays healthy."""
    _drive(fresh, 180)
    fresh.run_for(ms=SETTLE_MS)
    assert fresh.read_symbol("ams", "g_state_telemetry") == 0


@pytest.mark.parametrize("amps", [190, -190, 300], ids=["190A", "charge-190A", "300A"])
def test_over_current_trips_on_the_filter_curve(fresh, amps):
    """J-101, J-102, I-100: the trip comes when the IIR crosses 185 A, either
    sign, at the sample the firmware's own filter predicts (IFS_HIL's J-102
    profile says 200 A: drift)."""
    f0 = _pack_mA(*_legs(0))
    mA = _drive(fresh, amps)
    n = _samples_to_trip(f0, mA)
    assert n is not None
    elapsed = _ms_to_error(fresh, n * 50 + 500)
    assert elapsed is not None, f"no trip at {amps} A ({mA} mA)"
    assert abs(elapsed - n * 50) <= 70, f"tripped after {elapsed} ms, filter says {n * 50} ms"
    assert fresh.read_symbol("ams", "g_fault_reason_telemetry") == OVER_LIMIT


def test_the_over_current_latch_survives_the_current_falling(fresh):
    """K-101: back to 0 A, still Error."""
    _drive(fresh, 300)
    assert _ms_to_error(fresh, 3000) is not None
    _drive(fresh, 0)
    fresh.run_for(ms=SETTLE_MS)
    assert fresh.read_symbol("ams", "g_state_telemetry") == ERROR


@pytest.mark.parametrize("volts", [0.0, 0.6, 2.5], ids=["open", "low", "rail"])
def test_a_disconnected_leg_faults_the_sensor(fresh, volts):
    """N-001..N-003: OUT_P outside 700..2300 mV for three reads -> reason 8,
    inside ~3 samples."""
    fresh.io("ams").set_voltage("PF7", volts)
    elapsed = _ms_to_error(fresh, 400)
    assert elapsed is not None and elapsed <= 220, f"Error after {elapsed} ms"
    assert fresh.read_symbol("ams", "g_fault_reason_telemetry") == SENSOR_FAULT


def test_a_leg_glitch_shorter_than_the_confirmation_is_ignored(fresh):
    """N-004: out of window for at most two reads."""
    io = fresh.io("ams")
    io.set_voltage("PF7", 0.0)
    fresh.run_for(ms=80)
    io.set_voltage("PF7", COMMON_MODE_V)
    fresh.run_for(ms=1000)
    assert fresh.read_symbol("ams", "g_state_telemetry") == 0


def test_a_dead_adc_faults_current_stale(fresh):
    """J-000 / I-101: ADC3 stops converting (its kernel clock lost, the
    model's ConversionFault): no pack-current sample lands, and once the
    last good one is older than IStaleMs (200 ms) the AMS latches
    CurrentStale (9). That sample is up to one 50 ms poll older than the
    fault, and the safety tick adds up to 10 ms (plus the 10 ms polling
    here): 150..270 ms after injection. The latch outlives the ADC
    recovering."""
    fresh.monitor("sysbus.adc3_h73x ConversionFault true", board="ams")
    elapsed = _ms_to_error(fresh, 500)
    assert elapsed is not None and 150 <= elapsed <= 270, f"Error after {elapsed} ms"
    assert fresh.read_symbol("ams", "g_fault_reason_telemetry") == CURRENT_STALE
    fresh.monitor("sysbus.adc3_h73x ConversionFault false", board="ams")
    fresh.run_for(ms=1000)
    assert fresh.read_symbol("ams", "g_state_telemetry") == ERROR
