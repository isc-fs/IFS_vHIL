"""ecu-torque (#44): pedals to Torque_Nm_Req in Active, tick by tick.

ECU facts (IFS08-CE-ECU):
  APPS % = (raw - min) * 100 / (max - min), clamped 0..100 (control.cpp:13-19);
    cal defaults APPS1 2490..3350, APPS2 2345..3025 (ecu_config.hpp:95-98),
    in force when the NVM sector holds no record (control_task.cpp:49-56).
  demand = mean of the two, only if both > AppsAgreementPct (3); < 5 -> 0,
    > 90 -> 100 (control.cpp:38-43, ecu_config.hpp:165-172).
  T.11.8.9: |apps1 - apps2| > 10 points for >= 100 ms -> 0; not latched, it
    clears on the first agreeing sample (control.cpp:59-73, ecu_config.hpp:178-179).
  No EV.2.3 brake + throttle cut: removed with the rule (control.cpp:45-57).
  Four caps, each a min() on the demand (control.cpp:96-143):
    cell: IR-compensated OCV = v_cell_min + I_dA * 1 mOhm / 10, ramp 100 % at
      the 3130 mV knee to 13 % at the 2900 mV floor; raw cell <= 2850 mV -> 13 %
      whatever the compensation says (cell_derate.cpp:12-91, ecu_config.hpp:204-272).
      0x4A0 bytes 4-5 carry v_cell_min (vehicle_service.cpp:259-266).
    motor thermal: hotter valid 0x464 motor temp (raw - 50 degC), ramp 90..110
      degC to 20 %; 0xFF / < -40 degC / 0x464 stale 500 ms -> unknown ->
      min(60 %, last cap) (motor_thermal.cpp:22-104, ecu_config.hpp:393-411).
    pack thermal: hottest plausible online module on 0x136/0x137 (BE16 degC),
      ramp 40..50 degC to 20 %; offline modules (0x4A0 byte 2) skipped; either
      frame stale 1 s or none plausible -> min(60 %, last cap)
      (pack_thermal.cpp:23-90, control_task.cpp:128-147, ecu_config.hpp:437-450).
    EV 2.2.1 power: T_max = 653151 / mech rpm, mech = 0x463 erpm / 10, as % of
      the torque map (power_limit.cpp:13-31, ecu_config.hpp:353-368); 0x463
      stale 200 ms -> 5500 rpm assumed (control_task.cpp:175-178, ecu_config.hpp:618).
  0x362 Torque_Nm_Req = -(pct * 240 / 95 - 1200 / 95), bytes 2-3 LE, sent
    every 10 ms tick (inverter.cpp:11-22, control_task.cpp:344-360); non-zero
    only in Active (control.cpp:300-307).
  An ADC3 read that fails reads 0, "pedal released" (io_signals.cpp:13-31).
The inverter and AMS frames are stimulus held at fixed values (Ready, 350 V,
cool temperatures); the closed loop with an inverter plant is test_cosim.py.
The thermal and cell filters seed on their first valid sample after an
unknown one, so a test steps them by sending one unknown sample first
(motor_thermal.cpp:41-44,86-88, pack_thermal.cpp:50-55,77-79, cell_derate.cpp:28-29,67-69).
ADC3's ConversionFault (models/renode/Stm32H7Adc3.cs) stands in for a dead
converter.

IFS_HIL drift (findings, not encoded here; tests/hil/vcu):
  E-002 says the deadband is 10 % (it is 5 %), releases the brake "else EV.2.3
    cuts", and asserts 0x700.torque_cmd == 0, which now carries real Nm
    (pit_diag.cpp:52; appendix D4).
  G-002 tests the EV.2.3 latch the firmware no longer has (D1); replaced here
    by "brake + throttle does not cut".
  G-001 only checks the flag 400 ms after a 30-point split, not the 100 ms edge.
  vcu_profile.yaml brake_arm_raw = 900; the firmware's BrakeArmRaw is 750
    (ecu_config.hpp:123). Harmless there (it arms at +200), but stale.
"""
import random

import pytest

from vhil.plants import APPS1_FULL, APPS1_REST, APPS2_FULL, APPS2_REST
from vhil.sim import Sim
from vhil.system import REPO

ACTIVE, TICK_MS = 5, 10
APPS1, APPS2 = (2490, 3350), (2345, 3025)            # ecu_config.hpp:95-98
BRAKE_RELEASED, BRAKE_FIRM, BRAKE_FULL = 580, 1500, 3500
VREF = 3.3
ADC = "sysbus.adc3_h73x"
INV_STATE, INV_RPM, INV_TEMPS, INV_VDC, TORQUE_REQ = 0x461, 0x463, 0x464, 0x466, 0x362
OK_PRECHARGE, AMS_STATUS, CURRENTS, TMAX_A, TMAX_B, HEARTBEAT = 0x020, 0x4A0, 0x135, 0x136, 0x137, 0x100
COOL_MOTOR, COOL_PACK, HEALTHY_CELL = (75, 75), (25,) * 5, 3700
MOTOR_UNKNOWN, PACK_UNKNOWN = (0xFF, 0xFF), (0x7FFF,) * 5


# -- the firmware's arithmetic, for exact expectations ---------------------------

def _apps_pct(raw, cal):
    lo, hi = cal
    return 0 if raw <= lo else 100 if raw >= hi else (raw - lo) * 100 // (hi - lo)


def _demand(a1, a2):
    t = (a1 + a2) // 2 if a1 > 3 and a2 > 3 else 0
    return 0 if t < 5 else 100 if t > 90 else t


def _nm(pct):
    """Torque_Nm_Req on the wire for a torque %, negated (forward drive)."""
    return 0 if pct < 5 else -(pct * 240 // 95 - 1200 // 95)


def _raw(pct, cal):
    """The lowest ADC code that reads as pct."""
    lo, hi = cal
    return lo + -(-pct * (hi - lo) // 100)


def _power_cap(erpm):
    rpm = abs(int(erpm / 10))
    if rpm == 0 or 653151 // rpm >= 240:
        return 100
    return min(100, (653151 // rpm * 95 + 1200) // 240)


def _cell_cap(mv, current_da):
    if mv <= 2850:
        return 13
    comp = 0 if current_da is None else max(-500, min(500, int(current_da / 10)))
    est = mv + comp
    return 100 if est >= 3130 else 13 if est <= 2900 else 13 + 87 * (est - 2900) // 230


# -- stimulus ---------------------------------------------------------------------

def _v(code):
    return code * VREF / 4095


def _rpm_frame(erpm):
    raw = erpm & 0xFFFFF
    return bytes([0, 0, 0, 0, 0, (raw & 0x0F) << 4, (raw >> 4) & 0xFF, (raw >> 12) & 0xFF])


def _feed(sim, rpm=0, motor=COOL_MOTOR, cell_mv=HEALTHY_CELL, mask=0x1F, pack=COOL_PACK,
          current_da=None):
    """(Re)start every frame the ECU's torque path reads, each at or faster
    than its real cycle (0x4A0 500 ms, 0x136/0x137 250 ms), so nothing goes
    stale unless a test stops it."""
    inv, acu = sim.can("can_inv"), sim.can("can_acu")
    inv.send_periodic("state", INV_STATE, bytes([0, 0, 0, 0, 4, 0, 0]), 10)     # Ready
    inv.send_periodic("vdc", INV_VDC, bytes([0, 0, 0x5E, 0x01, 0, 0]), 10)      # 350 V
    inv.send_periodic("rpm", INV_RPM, _rpm_frame(rpm), 10)
    inv.send_periodic("temps", INV_TEMPS, bytes([75, 75, *motor]), 50)
    acu.send_periodic("ok", OK_PRECHARGE, b"\x01", 50)
    acu.send_periodic("status", AMS_STATUS,
                      bytes([0, 1, mask, 0, *cell_mv.to_bytes(2, "big"), 0x0E, 0x74]), 50)
    be = b"".join(t.to_bytes(2, "big", signed=True) for t in pack)
    acu.send_periodic("tmax_a", TMAX_A, be[:6], 50)
    acu.send_periodic("tmax_b", TMAX_B, be[6:] + bytes(2), 50)
    if current_da is None:
        acu.stop_periodic("currents")
    else:
        acu.send_periodic("currents", CURRENTS, current_da.to_bytes(2, "big", signed=True) + bytes(2), 50)


def _reseed(sim, **target):
    """One unknown sample for every filter, then the target values: each
    filter seeds on the target exactly."""
    _feed(sim, motor=MOTOR_UNKNOWN, cell_mv=0, pack=PACK_UNKNOWN,
          current_da=target.get("current_da"))
    sim.run_for(ms=120)
    _feed(sim, **target)
    sim.run_for(ms=150)


def _pedals(sim, apps1, apps2):
    """Raw ADC codes on APPS1 (PF8) and APPS2 (PF9)."""
    io = sim.io("ecu")
    io.set_voltage("PF8", _v(apps1))
    io.set_voltage("PF9", _v(apps2))


def _pct_pedals(sim, p1, p2):
    _pedals(sim, _raw(p1, APPS1), _raw(p2, APPS2))


def _nm_of(f):
    return int.from_bytes(f.data[2:4], "little", signed=True)


def _torques(sim, since_us):
    return [(f.t_us, _nm_of(f)) for f in sim.can("can_inv").frames(TORQUE_REQ, since_us)]


def _settled(sim, ms=60):
    """Torque_Nm_Req values of the last 30 ms after running ms."""
    t = sim.run_for(ms=ms)
    vals = {nm for _, nm in _torques(sim, t - 30_000)}
    assert vals, "no 0x362 in the last 30 ms"
    return vals


def _state(sim):
    return sim.read_symbol("ecu", "g_last_ctrl_state")


@pytest.fixture(scope="module")
def car(images):
    """One ECU driven to Active: inverter Ready, AMS precharged, START + brake."""
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        _feed(sim)
        io = sim.io("ecu")
        _pedals(sim, APPS1_REST, APPS2_REST)
        io.set_voltage("PF7", _v(BRAKE_FIRM))
        sim.wait_for_app()
        sim.run_for(ms=1000)
        io.set_input("sysbus.gpioPortB", 5, True)
        sim.run_for(ms=100)
        io.set_input("sysbus.gpioPortB", 5, False)
        io.set_voltage("PF7", _v(BRAKE_RELEASED))
        sim.run_until(lambda: _state(sim) == ACTIVE, timeout_ms=3000, step_ms=50)
        yield sim


@pytest.fixture
def active(car):
    """The shared car, back to nominal: pedals at rest, every input fresh and
    healthy, every filter seeded on it; and still in Active afterwards."""
    car.monitor(f"{ADC} ConversionFault false")
    _pedals(car, APPS1_REST, APPS2_REST)
    car.io("ecu").set_voltage("PF7", _v(BRAKE_RELEASED))
    _reseed(car)
    assert _state(car) == ACTIVE
    yield car
    assert _state(car) == ACTIVE, "the car left Active"


# -- APPS map and deadbands (E-002) -----------------------------------------------

@pytest.mark.parametrize("apps1, apps2", [
    (APPS1_REST, APPS2_REST),                     # the car's pedal at rest
    (_raw(4, APPS1), _raw(4, APPS2)),             # under the 5 % deadband
    (_raw(5, APPS1), _raw(5, APPS2)),             # the map's zero: 0 Nm
    (_raw(6, APPS1), _raw(6, APPS2)),
    (_raw(3, APPS1), _raw(12, APPS2)),            # mean 7 %, but APPS1 not > 3 %
    (_raw(4, APPS1), _raw(12, APPS2)),            # both past the agreement gate
    (_raw(50, APPS1), _raw(50, APPS2)),
    (_raw(89, APPS1), _raw(92, APPS2)),           # mean 90 %: still on the map
    (_raw(90, APPS1), _raw(93, APPS2)),           # mean 91 %: past the high deadband
    (APPS1_FULL, APPS2_FULL),                     # the car's pedal fully down
], ids=["rest", "4%", "5%", "6%", "3/12%", "4/12%", "50%", "89/92%", "90/93%", "full"])
def test_apps_map_to_torque_nm_req(active, apps1, apps2):
    """E-002: 0x362 follows the firmware's map exactly, deadbands and the 3 %
    agreement gate included."""
    _pedals(active, apps1, apps2)
    expected = _nm(_demand(_apps_pct(apps1, APPS1), _apps_pct(apps2, APPS2)))
    assert _settled(active) == {expected}


# -- T.11.8.9 (G-001) ---------------------------------------------------------------

def test_t11_8_9_cuts_on_the_100_ms_sample_and_clears_on_agreement(active):
    """G-001: 11 points apart, torque for the samples at 0..90 ms and none from
    the one at 100 ms; it comes back on the first agreeing sample."""
    _pct_pedals(active, 50, 50)
    active.run_for(ms=100)
    t0 = active.now_us()
    _pct_pedals(active, 50, 39)
    active.run_for(ms=300)
    seen = _torques(active, t0)
    disagreeing = _nm(_demand(50, 39))
    on = [t for t, nm in seen if nm == disagreeing]
    off = [t for t, nm in seen if nm == 0]
    assert len(on) == 10, f"torque on {len(on)} samples of the disagreement: {seen}"
    assert off and abs(off[0] - on[0] - 100_000) <= 1000
    assert all(nm == 0 for t, nm in seen if t >= off[0])
    assert active.read_symbol("ecu", "g_last_t11_8_9") == 1
    t1 = active.now_us()
    _pct_pedals(active, 50, 50)
    active.run_for(ms=50)
    back = [nm for _, nm in _torques(active, t1)]
    assert _nm(50) in back[:2] and set(back[back.index(_nm(50)):]) == {_nm(50)}, back
    assert active.read_symbol("ecu", "g_last_t11_8_9") == 0


def test_t11_8_9_ten_points_apart_is_plausible(active):
    """Exactly 10 points is not 'more than ten': no cut."""
    _pct_pedals(active, 50, 40)
    assert _settled(active, ms=500) == {_nm(_demand(50, 40))}
    assert active.read_symbol("ecu", "g_last_t11_8_9") == 0


@pytest.mark.parametrize("glitch_ms, cut", [(85, False), (115, True)])
def test_t11_8_9_needs_100_ms_of_persistence(active, glitch_ms, cut):
    """A disagreement over fewer than 11 samples never cuts; one over more does."""
    _pct_pedals(active, 50, 50)
    active.run_for(ms=100)
    t0 = active.now_us()
    _pct_pedals(active, 50, 30)
    active.run_for(ms=glitch_ms)
    _pct_pedals(active, 50, 50)
    active.run_for(ms=100)
    zeros = [t for t, nm in _torques(active, t0) if nm == 0]
    assert bool(zeros) == cut, f"zero-torque samples at {zeros}"


# -- brake + throttle (G-002 replaced) ----------------------------------------------

@pytest.mark.parametrize("brake", [BRAKE_FIRM, BRAKE_FULL])
def test_brake_and_throttle_together_do_not_cut(active, brake):
    """D1: EV.2.3 is gone from the rules and the firmware; braking hard on
    half throttle leaves the torque alone."""
    _pct_pedals(active, 50, 50)
    active.run_for(ms=100)
    t0 = active.now_us()
    active.io("ecu").set_voltage("PF7", _v(brake))
    active.run_for(ms=500)
    assert {nm for _, nm in _torques(active, t0)} == {_nm(50)}


# -- sensor and ADC faults (G-003) ---------------------------------------------------

@pytest.mark.parametrize("pin, volts, immediate", [
    ("PF8", 0.0, True),         # APPS1 shorted to ground: reads 0 %, agreement gate
    ("PF9", 0.0, True),         # APPS2 shorted to ground
    ("PF9", VREF, False),       # APPS2 shorted to supply at half travel: T.11.8.9
], ids=["apps1-gnd", "apps2-gnd", "apps2-supply"])
def test_a_failed_apps_sensor_removes_torque(active, pin, volts, immediate):
    """G-003: a sensor short at half throttle: 0 within a sample (to ground),
    or by the T.11.8.9 cut (to supply, 100 vs 50 %)."""
    _pct_pedals(active, 50, 50)
    active.run_for(ms=100)
    t0 = active.now_us()
    active.io("ecu").set_voltage(pin, volts)
    active.run_for(ms=300)
    seen = _torques(active, t0)
    first_zero = next(t for t, nm in seen if nm == 0)
    assert all(nm == 0 for t, nm in seen if t >= first_zero)
    assert first_zero - t0 <= (2 * TICK_MS if immediate else 110 + TICK_MS) * 1000, seen


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-ECU#247: an APPS shorted to supply reads 100 % (apps_pct clamps any raw >= max, "
    "control.cpp:16) and is never treated as a failure; with the other sensor at >= 90 % "
    "they agree, so FS T11.8.8/T11.9.2 (short to supply = implausibility, safe state "
    "within T11.9.4's 500 ms) is not met and full torque stays commanded"))
def test_an_apps_shorted_to_supply_is_detected_at_full_throttle(active):
    _pedals(active, APPS1_FULL, APPS2_FULL)
    active.run_for(ms=100)
    active.io("ecu").set_voltage("PF8", VREF)
    assert _settled(active, ms=500) == {0}


def test_an_adc_conversion_fault_reads_as_released(active):
    """G-003: ADC3 stops converting (no EOC): each read fails to 0, so torque
    goes to 0 within a sample, and the ControlTask keeps its 10 ms cadence
    through the HAL timeouts. Torque returns when the converter does."""
    _pct_pedals(active, 50, 50)
    active.run_for(ms=100)
    t0 = active.now_us()
    active.monitor(f"{ADC} ConversionFault true")
    active.run_for(ms=500)
    seen = _torques(active, t0)
    first_zero = next(t for t, nm in seen if nm == 0)
    assert first_zero - t0 <= 2 * TICK_MS * 1000
    assert all(nm == 0 for t, nm in seen if t >= first_zero)
    assert active.read_symbol("ecu", "g_last_apps1_raw", 2) == 0
    assert 49 <= active.can("can_acu").count(HEARTBEAT, t0) <= 51
    active.monitor(f"{ADC} ConversionFault false")
    assert _settled(active, ms=100) == {_nm(50)}


# -- caps (gap 5) ---------------------------------------------------------------------

@pytest.mark.parametrize("erpm, pedal", [
    (20_000, 100),              # 2000 rpm: under the 2721 rpm knee, not binding
    (30_000, 100),
    (50_000, 100),
    (-50_000, 100),             # reverse: the envelope uses |rpm|
    (80_000, 100),
    (50_000, 30),               # a cap, not a gain: 30 % under a 56 % cap passes
])
def test_power_envelope_caps_on_motor_speed(active, erpm, pedal):
    """EV 2.2.1: the cap follows 0x463's speed exactly."""
    _feed(active, rpm=erpm)
    _pct_pedals(active, pedal, pedal)
    assert _settled(active, ms=100) == {_nm(min(_demand(pedal, pedal), _power_cap(erpm)))}


def test_stale_motor_speed_assumes_5500_rpm(active):
    """D6: 0x463 silent for 200 ms -> the envelope assumes 5500 rpm (51 %),
    never the 0 rpm of a frozen or uninitialised value."""
    _pedals(active, APPS1_FULL, APPS2_FULL)
    assert _settled(active) == {_nm(100)}
    active.can("can_inv").stop_periodic("rpm")
    assert _settled(active, ms=300) == {_nm(_power_cap(55_000))} == {_nm(51)}


@pytest.mark.parametrize("cell_mv, current_da", [
    (3130, None),               # the knee
    (3015, None),
    (2950, None),
    (2900, None),               # the floor: 13 % = 20 Nm
    (2950, 1000),               # 100 A: +100 mV of IR compensation
    (2950, 2000),               # +200 mV: back over the knee
    (2850, 2000),               # raw backstop: 13 % whatever the compensation
])
def test_low_cell_derate(active, cell_mv, current_da):
    _reseed(active, cell_mv=cell_mv, current_da=current_da)
    _pedals(active, APPS1_FULL, APPS2_FULL)
    assert _settled(active) == {_nm(_cell_cap(cell_mv, current_da))}


@pytest.mark.parametrize("motor, cap", [
    ((140, 140), 100),          # 90 degC: the ramp starts
    ((145, 75), 80),            # the hotter sensor counts: 95 degC
    ((75, 155), 40),            # 105 degC
    ((160, 160), 20),           # 110 degC: the floor
    ((0xFF, 145), 80),          # one disconnected: the other one counts
    ((0xFF, 0xFF), 60),         # both disconnected: unknown
    ((5, 5), 60),               # -45 degC: not a temperature
])
def test_motor_thermal_cap(active, motor, cap):
    _reseed(active, motor=motor)
    _pedals(active, APPS1_FULL, APPS2_FULL)
    assert _settled(active) == {_nm(cap)}


def test_motor_temps_lost_keep_a_hot_cap(active):
    """Losing 0x464 caps at min(60 %, the last cap): a hot motor stays at its
    20 % floor rather than being released to 60 %."""
    _reseed(active, motor=(160, 160))
    _pedals(active, APPS1_FULL, APPS2_FULL)
    assert _settled(active) == {_nm(20)}
    active.can("can_inv").stop_periodic("temps")
    assert _settled(active, ms=700) == {_nm(20)}


def test_motor_temps_stale_cap_at_60(active):
    _pedals(active, APPS1_FULL, APPS2_FULL)
    active.can("can_inv").stop_periodic("temps")
    assert _settled(active, ms=700) == {_nm(60)}


@pytest.mark.parametrize("pack, mask, cap", [
    ((25, 25, 25, 42, 25), 0x1F, 84),        # module 3 at 42 degC
    ((50, 25, 25, 25, 25), 0x1F, 20),        # module 0 at the 50 degC limit
    ((25, 25, 50, 25, 25), 0x1B, 100),       # module 2 hot but offline: skipped
    ((25, 25, 25, 25, 126), 0x1F, 100),      # 126 degC is not a reading: skipped
    ((126,) * 5, 0x1F, 60),                  # nothing plausible: unknown
])
def test_pack_thermal_cap(active, pack, mask, cap):
    _reseed(active, pack=pack, mask=mask)
    _pedals(active, APPS1_FULL, APPS2_FULL)
    assert _settled(active) == {_nm(cap)}


@pytest.mark.parametrize("silent", [("tmax_a", "tmax_b"), ("tmax_b",)])
def test_pack_temps_stale_cap_at_60(active, silent):
    """Either frame silent for 1 s is enough: 0x136 and 0x137 carry different
    modules, so one fresh frame must not keep the other's frozen."""
    _pedals(active, APPS1_FULL, APPS2_FULL)
    for key in silent:
        active.can("can_acu").stop_periodic(key)
    assert _settled(active, ms=1200) == {_nm(60)}


# -- soak (J-001) ----------------------------------------------------------------------

def test_drive_soak_60_s(active):
    """J-001: 60 s in Active under a varying pedal, every step's torque exactly
    the map's, the heartbeat unbroken (no reset)."""
    rng = random.Random(44)
    t0 = active.now_us()
    for _ in range(300):
        p = rng.choice([0, rng.randint(0, 100)])
        _pct_pedals(active, p, p)
        t = active.run_for(ms=200)
        last = active.can("can_inv").last(TORQUE_REQ, t - 20_000)
        assert last is not None and _nm_of(last) == _nm(_demand(p, p)), f"at {t} us, pedal {p} %"
    beats = active.can("can_acu").frames(HEARTBEAT, t0)
    assert abs(len(beats) - 6000) <= 1
    assert max(b.t_us - a.t_us for a, b in zip(beats, beats[1:])) <= 11_000
