"""AMS current sensing through the H73x ADC3 model (#19).

Firmware facts (IFS08-CE-AMS):
  pack current: differential ADC3 INP3/INN3 = PF7/PF8, Bourns SSA-2;
    mA = (raw - CurrentZeroCount) * 2 * Vref / 4095 * 10 / CurrentMvPerAmpe1
                                                   current_service.cpp:34-40
  DCDC current (main only): single-ended ADC3 INP11 = PC1, ACS758;
    mA = (raw * Vref / 4095 - DcdcCurrentZeroMv) * 10 / DcdcCurrentMvPerAmpe1
    (main current_service.cpp:55-62). dev fits no DC-DC: PC1 is not sampled
    and 0x135's dcdc slot is always 0         acu_tx_encoders.hpp:165-171
  constants                                         ams_config.hpp:733-740
  sampled every 50 ms, IIR-filtered (shift 4), sent as BE i16
  deciamps [pack | dcdc] on 0x135                   acu_tx_encoders.hpp:165-174
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

The car's carrier reads the pair through a measured transfer (gain 0.92 on
the differential, +6 codes; catalog/boards/mainlite.yaml roles.ams
adc_differential, #320), which the firmware's calibration (46 for the
sensor's 5 mV/A, zero 2054) cancels: the AMS reads the true current. On the
ideal ADC it read ~1.087 x I - 2.1 A and tripped at 173 A.
"""
import pytest
import yaml

from vhil import elf
from vhil.plant_pack import sensor_volts
from vhil.sim import Sim
from vhil.system import CATALOG, REPO

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

SSA2 = yaml.safe_load((CATALOG / "models" / "ssa-2-250a.yaml").read_text())
_T = yaml.safe_load((CATALOG / "boards" / "mainlite.yaml").read_text())["roles"]["ams"]["adc_differential"]["PF7"]
DIFF_GAIN, DIFF_OFFSET = _T["gain"], _T.get("offset_codes", 0)


def _code_diff(vp, vn):
    """What the ADC3 model returns for a 12-bit differential conversion of
    the AMS carrier's pack-current pair (its measured transfer)."""
    return max(0, min(4095, round((1 + DIFF_GAIN * (vp - vn) / VREF) * 2048 + DIFF_OFFSET)))


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
    """The SSA-2-250A's legs at a true current (its catalogue transfer, 5 mV/A)."""
    v = sensor_volts(SSA2, amps)
    return v["out_p"], v["out_n"]


def _accuracy_dA(amps):
    """What the AMS's reading may miss a true current by: the firmware's
    calibration residual (CurrentMvPerAmpe1 46 vs the carrier's 4.6 mV/A
    within the car's check; the bench saw +0.4 %), one 12-bit code (0.35 A)
    and the IIR's dropped low bits (0.5 A)."""
    return 0.004 * abs(amps) * 10 + 3.5 + 5


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
    current fault (0 V legs read as a disconnected sensor), and reads 0 A:
    the carrier's zero (+6 codes) is the firmware's CurrentZeroCount 2054
    (on the ideal ADC it read -2.1 A, #320)."""
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
    assert abs(accu) <= 5, f"0 A reads {accu} dA"


@pytest.mark.parametrize("amps", [0, 50, -50, 100, 172, 185])
def test_pack_current_reaches_0x135(ams, amps):
    """A true current on the sensor reads as itself on 0x135, within the
    firmware's calibration accuracy, and exactly as its converter computes
    from the carrier's code. 172 A read 184.997 A on the ideal ADC (#320);
    185 A, the limit, stays under it."""
    io = ams.io("ams")
    vp, vn = _legs(amps)
    io.set_voltage("PF7", vp)
    io.set_voltage("PF8", vn)
    since = ams.run_for(ms=SETTLE_MS)
    accu, _ = _currents(ams.can("can_acu").last(CURRENTS))
    expected = _pack_dA(vp, vn)
    # 0.5 A: the firmware's IIR drops the low 4 bits of every step.
    assert abs(accu - expected) <= 5, f"{amps} A in: 0x135 accu {accu} dA, expected {expected:.0f}"
    assert abs(accu - 10 * amps) <= _accuracy_dA(amps), f"{amps} A in: 0x135 accu {accu} dA"
    assert ams.can("can_acu").count(CURRENTS, since_us=since - 1_000_000) >= 15, "0x135 not at 20 Hz"


def test_dcdc_slot_of_0x135(ams):
    """AMS dev fits no DC-DC: PC1 is not sampled and 0x135's dcdc slot is
    always 0, whatever the pin carries (acu_tx_encoders.hpp:165-171,
    acu_currents.def:5-6; af07ec8). A build without the DMA capture (main)
    still samples PC1 and must report it."""
    io = ams.io("ams")
    volts = DCDC_ZERO_MV / 1000 + 10 * DCDC_MV_PER_A_E1 / 10 / 1000   # +10 A
    io.set_voltage("PC1", volts)
    ams.run_for(ms=SETTLE_MS)
    _, dcdc = _currents(ams.can("can_acu").last(CURRENTS))
    if "hdma_adc3" in elf.symbols(ams.firmware["ams"]):
        assert dcdc == 0, f"0x135 dcdc {dcdc} dA with no DC-DC fitted"
        return
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
def fresh(images):
    """A booted AMS past its grace, at 0 A, for tests that latch Error."""
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
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


def _off_zero(sim):
    """Settle the AMS's current filter on 20 A, off exactly 0: the firmware
    re-seeds the filter with the next sample while it holds exactly 0
    (current_service.cpp update_from_q4), where the noise-free ADC leaves it
    at rest and the car's noise does not (#327). ~4 time constants: within
    0.4 A of 20 A."""
    _drive(sim, 20)
    sim.run_for(ms=3000)


@pytest.mark.parametrize("amps", [175, 185, -185], ids=["175A", "185A", "charge-185A"])
def test_up_to_the_limit_never_trips(fresh, amps):
    """J-100: up to CurrentMaxMa = 185 A of true current (ams_config.hpp:153;
    the predicate is |filtered| > the limit, safety_predicates.hpp:244),
    held for 8 s (ten IIR time constants), stays healthy. On the ideal ADC
    175 A tripped (read ~188 A, #320)."""
    _drive(fresh, amps)
    fresh.run_for(ms=SETTLE_MS)
    assert fresh.read_symbol("ams", "g_state_telemetry") == 0


@pytest.mark.parametrize("amps", [190, -190, 200, 300], ids=["190A", "charge-190A", "200A", "300A"])
def test_over_current_trips_on_the_filter_curve(fresh, amps):
    """J-101, J-102, I-100: the trip comes when the IIR crosses 185 A, either
    sign, at the sample the firmware's own filter predicts (IFS_HIL's J-102
    profile says 200 A: drift).

    From 20 A (_off_zero): from exactly 0 the filter would re-seed with the
    step's partial 50 ms capture, and the trip come anywhere up to the
    filter's prediction (#327)."""
    _off_zero(fresh)
    f0 = _pack_mA(*_legs(20))
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
    inside ~3 samples, before the over-current check: from 20 A the filter
    takes far longer to cross 185 A (#327: from exactly 0 it would re-seed
    and trip CurrentOverLimit first)."""
    _off_zero(fresh)
    fresh.io("ams").set_voltage("PF7", volts)
    elapsed = _ms_to_error(fresh, 400)
    assert elapsed is not None and elapsed <= 220, f"Error after {elapsed} ms"
    assert fresh.read_symbol("ams", "g_fault_reason_telemetry") == SENSOR_FAULT


def test_a_leg_glitch_shorter_than_the_confirmation_is_ignored(fresh):
    """N-004: out of window for at most two reads (from 20 A, #327)."""
    _off_zero(fresh)
    io = fresh.io("ams")
    io.set_voltage("PF7", 0.0)
    fresh.run_for(ms=80)
    io.set_voltage("PF7", _legs(20)[0])
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
