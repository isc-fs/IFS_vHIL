"""ecu-dv (#45): the ECU's side of the uDV (driverless) contract, in virtual time.

ECU facts (IFS08-CE-ECU, Core/Src/app/ unless shown):
  control_task.cpp:330-338: on the ACU bus, ungated: 0x506 VCU_motor_rpm every
    control tick (s32 LE, erpm / MotorPolePairs = 10, udv_tx.cpp:42-52);
    every UdvTxPeriodMs = 100: 0x504 ts_active (= ok_precharge && AMS fresh),
    0x505 brake_over_limit (brake_raw > cal.brake_dv_hard, default
    BrakeDvHardRaw = 2500, ecu_config.hpp:130) and 0x511 r2d_confirm (= the DV
    latch). Each is one byte, byte 0 (vcu_*.def).
  vehicle_service.cpp:268-281: 0x507 (s32 LE integer %, dlc >= 4) and 0x510
    (byte 0) on the ACU bus, stamped with their own ticks.
  control_task.cpp:152-155: dv_r2d_req = 0x510 byte 0 != 0 && fresh within
    UdvR2dStaleMs = 200; dv_fresh = 0x507 within UdvCmdStaleMs = 100
    (ecu_config.hpp:557-558); condition_udv_torque clamps to 0..100
    (vehicle_service.cpp:107-115).
  control.cpp:165-177: in WaitStartBrake (2), START + brake > brake_arm (750)
    wins; else a fresh 0x510 with brake > brake_dv_hard enters R2dDelay (3)
    and latches DV mode. control.cpp:21-30: entering any state below R2dDelay,
    or AmsError (6), clears the latch; WaitInvStandby (4) <-> Active (5) does
    not.
  control.cpp:83-85: latched, torque = conditioned 0x507 if fresh, else 0 (no
    APPS fallback); the pedal T.11.8.9 cut and the deadband do not apply to
    it, the caps (cell, motor, pack, power) do. control.cpp:150: a fresh AMS
    Error enters AmsError from any state, inverter Off, torque 0.
  inverter.cpp:11-22: 0x362 Nm = -(pct*240/95 - 1200/95), 0 below 5 %.
To see the uncapped DV torque the inverter reports rpm 0 (power cap 100 %,
power_limit.cpp:16), cool motor temperatures on 0x464 and the AMS cool pack
temperatures on 0x136/0x137 with all five modules online on 0x4A0: with any of
them stale the firmware caps at 51-60 % (IFS_HIL drift D6). The inverter here
is a scripted stub on FDCAN1 that follows the ECU's 0x360 words (Standby ->
Ready on 0x04 -> TorqueEnable on 0x06); the AMS frames are stimulus (ecu.yaml
has no AMS).

Drift (IFS_HIL Block L, docs/analysis/ifs-hil-tests-ecu.md D5): its
can_map.dv_torque_nm maps % /90 with a 10 % deadband; the firmware's map is
the one above (40 % -> -89 Nm, not -80), and that is what is asserted here.
vcu_r2d_confirm.def:5-6 calls 0x511 "acyclic, on the DV R2D transition"; the
firmware sends it every 100 ms carrying the latch (control_task.cpp:335-337),
which is what the uDV keys on and what is asserted here.
"""
import pytest

from vhil.plants import APPS1_FULL, APPS2_FULL, APPS1_REST, APPS2_REST, COUNTS_TO_V
from vhil.sim import Sim, assert_period
from vhil.system import REPO

TS_ACTIVE, BRAKE_OVER, MOTOR_RPM, R2D_CONFIRM = 0x504, 0x505, 0x506, 0x511
UDV_TORQUE, UDV_R2D = 0x507, 0x510
OK_PRECHARGE, AMS_STATUS, TMAX_A, TMAX_B = 0x020, 0x4A0, 0x136, 0x137
INV_CMD, INV_TORQUE, INV_STATE, INV_RPM, INV_TEMPS, INV_VDC = 0x360, 0x362, 0x461, 0x463, 0x464, 0x466
WAIT_START_BRAKE, R2D_DELAY, WAIT_INV_STANDBY, ACTIVE, AMS_ERROR, PRECHARGE = 2, 3, 4, 5, 6, 1
INV_STANDBY, INV_READY, INV_TORQUE_EN = 3, 4, 6
W_OFF, W_READY, W_TORQUE = 0x01, 0x04, 0x06
TICK_MS, UDV_PERIOD_MS, R2D_SOUND_MS = 10, 100, 2000
R2D_STALE_MS, CMD_STALE_MS = 200, 100
BRAKE_RELEASED, BRAKE_FIRM, BRAKE_DV_HARD, BRAKE_HARD = 580, 1500, 2500, 2700


def _nm(pct):
    """inverter.cpp:11-22, as the int16 on 0x362."""
    pct = max(0, min(100, pct))
    return 0 if pct < 5 else -(pct * 240 // 95 - 1200 // 95)


def _status(state):
    """0x4A0: FSM state, ams_ok, module mask 0x1F, min/max cell 3700 mV."""
    return bytes([state, 1 if state != 5 else 0, 0x1F, 0, 0x0E, 0x74, 0x0E, 0x74])


def _rpm_frame(erpm):
    raw = erpm & 0xFFFFF                                  # 20 bits signed from bit 44
    return bytes([0, 0, 0, 0, 0, (raw & 0x0F) << 4, (raw >> 4) & 0xFF, (raw >> 12) & 0xFF])


class _Car:
    """The ECU plus what it gates on: AMS stimulus on can_acu, a scripted
    inverter on can_inv, the pedals and START."""

    def __init__(self, sim):
        self.sim, self.acu, self.inv, self.io = sim, sim.can("can_acu"), sim.can("can_inv"), sim.io("ecu")
        self.inv_state = INV_STANDBY
        self._seen_us = 0
        self.acu.send_periodic("ok", OK_PRECHARGE, b"\x01", 10)
        self.acu.send_periodic("status", AMS_STATUS, _status(0), 50)
        self.acu.send_periodic("tmax_a", TMAX_A, bytes([0, 25, 0, 25, 0, 25]), 100)
        self.acu.send_periodic("tmax_b", TMAX_B, bytes([0, 25, 0, 25, 0, 25]), 100)
        self.inv.send_periodic("vdc", INV_VDC, bytes([0, 0, 0x5E, 0x01, 0, 0]), 10)       # 350 V
        self.inv.send_periodic("state", INV_STATE, self._state_frame(), 10)
        self.inv.send_periodic("rpm", INV_RPM, _rpm_frame(0), 10)
        self.inv.send_periodic("temps", INV_TEMPS, bytes([75, 75, 75, 75]), 100)       # 25 degC
        self.brake(BRAKE_RELEASED)
        self.throttle(0.0)

    def _state_frame(self):
        return bytes([0, 0, 0, 0, self.inv_state & 0x7F, 0, 0])

    def set_inverter(self, state):
        self.inv_state = state
        self.inv.update_periodic("state", self._state_frame())

    def brake(self, code):
        self.io.set_voltage("PF7", code * COUNTS_TO_V)

    def throttle(self, x):
        self.io.set_voltage("PF8", (APPS1_REST + x * (APPS1_FULL - APPS1_REST)) * COUNTS_TO_V)
        self.io.set_voltage("PF9", (APPS2_REST + x * (APPS2_FULL - APPS2_REST)) * COUNTS_TO_V)

    def start(self, pressed):
        self.io.set_input("sysbus.gpioPortB", 5, pressed)

    def state(self):
        return self.sim.read_symbol("ecu", "g_last_ctrl_state")

    def follow(self, ms):
        """Run tick by tick, the inverter stub acting on the ECU's 0x360 words."""
        for _ in range(int(ms // TICK_MS)):
            self.sim.run_for(ms=TICK_MS)
            words = [f.data[2] & 0x7F for f in self.inv.frames([INV_CMD], since_us=self._seen_us)]
            self._seen_us = self.sim.now_us() + 1
            s = self.inv_state
            for w in words:
                if w == W_READY and s in (INV_STANDBY, INV_TORQUE_EN):
                    s = INV_READY
                elif w == W_TORQUE and s == INV_READY:
                    s = INV_TORQUE_EN
                elif w == W_OFF and s in (INV_READY, INV_TORQUE_EN):
                    s = INV_STANDBY
            if s != self.inv_state:
                self.set_inverter(s)

    def torques(self, since_us=0, until_us=None):
        return [(f.t_us, int.from_bytes(f.data[2:4], "little", signed=True))
                for f in self.inv.frames([INV_TORQUE], since_us)
                if until_us is None or f.t_us <= until_us]


def _confirm(car, since_us):
    f = car.acu.last(R2D_CONFIRM, since_us)
    assert f is not None, "no 0x511"
    return f.data[0]


def _to_wait_start_brake(car):
    car.sim.wait_for_app()
    car.sim.run_for(ms=1000)
    assert car.state() == WAIT_START_BRAKE


def _to_dv_active(car, pct=40):
    """0x510 + EBS hard braking -> R2D (DV latched) -> the inverter climbs ->
    Active; the uDV then streams 0x507 and the EBS releases."""
    _to_wait_start_brake(car)
    car.brake(BRAKE_HARD)
    car.acu.send_periodic("r2d", UDV_R2D, b"\x01", 50)
    car.sim.run_for(ms=R2D_SOUND_MS + 100)
    car.acu.send_periodic("cmd", UDV_TORQUE, pct.to_bytes(4, "little", signed=True), 10)
    car.follow(200)
    assert car.state() == ACTIVE
    assert car.inv_state == INV_TORQUE_EN
    car.acu.stop_periodic("r2d")
    car.brake(BRAKE_RELEASED)
    car.follow(100)
    t = car.sim.now_us()
    car.follow(UDV_PERIOD_MS + 2 * TICK_MS)
    assert _confirm(car, t) == 1, "0x511 not confirming the DV drive"


def _boot(images):
    return Sim(REPO / "systems" / "ecu.yaml", images("ecu"))


@pytest.fixture
def car(images):
    with _boot(images) as sim:
        yield _Car(sim)


# -- the contract frames, on one boot parked in WaitStartBrake -------------------

@pytest.fixture(scope="module")
def parked(images):
    with _boot(images) as sim:
        car = _Car(sim)
        _to_wait_start_brake(car)
        yield car


@pytest.mark.parametrize("erpm, rpm", [(54321, 5432), (-12345, -1234), (0, 0)])
def test_motor_rpm_every_tick_mechanical(parked, erpm, rpm):
    """L-003: 0x506 = erpm / 10 (truncated toward zero), s32 LE, every 10 ms."""
    parked.inv.update_periodic("rpm", _rpm_frame(erpm))
    t = parked.sim.run_for(ms=200)
    frames = parked.acu.frames([MOTOR_RPM], since_us=t - 100_000)
    assert_period(frames, period_us=TICK_MS * 1000, tolerance_us=0, min_count=9)
    assert {int.from_bytes(f.data[:4], "little", signed=True) for f in frames} == {rpm}
    parked.inv.update_periodic("rpm", _rpm_frame(0))


def test_100_ms_frames_cadence_and_idle_values(parked):
    """0x504 / 0x505 / 0x511 every 100 ms; parked with the TS up and the brake
    released: ts_active 1, brake_over_limit 0, r2d_confirm 0."""
    t = parked.sim.run_for(ms=1000)
    for can_id, value in [(TS_ACTIVE, 1), (BRAKE_OVER, 0), (R2D_CONFIRM, 0)]:
        frames = parked.acu.frames([can_id], since_us=t - 1_000_000)
        assert_period(frames, period_us=UDV_PERIOD_MS * 1000, tolerance_us=0, min_count=9)
        assert {(len(f.data), f.data[0]) for f in frames} == {(1, value)}, hex(can_id)


@pytest.mark.parametrize("brake, over", [(BRAKE_FIRM, 0), (BRAKE_DV_HARD, 0),
                                         (BRAKE_DV_HARD + 1, 1), (4000, 1)])
def test_brake_over_limit_boundary(parked, brake, over):
    """L-002: 0x505 = brake_raw > 2500, strictly."""
    parked.brake(brake)
    t = parked.sim.run_for(ms=250)
    assert parked.sim.read_symbol("ecu", "g_last_brake_raw", 2) == brake
    frames = parked.acu.frames([BRAKE_OVER], since_us=t - 150_000)
    assert frames and all(f.data[0] == over for f in frames)
    parked.brake(BRAKE_RELEASED)
    parked.sim.run_for(ms=50)


@pytest.mark.parametrize("brake, req", [
    (BRAKE_RELEASED, 1), (BRAKE_FIRM, 1), (BRAKE_DV_HARD, 1),   # not hard braking
    (BRAKE_HARD, 0),                                            # hard braking, request byte 0
])
def test_dv_refused_without_hard_braking_or_a_request(parked, brake, req):
    """L-004: 0x510 is honoured only with brake > 2500 and byte 0 != 0."""
    parked.brake(brake)
    parked.acu.send_periodic("r2d", UDV_R2D, bytes([req]), 50)
    t = parked.sim.run_for(ms=500)
    parked.acu.stop_periodic("r2d")
    parked.brake(BRAKE_RELEASED)
    # Past UdvR2dStaleMs: the next case must not meet this case's request,
    # still fresh, with its own hard braking (as [2700-0] did after [2500-1]).
    parked.sim.run_for(ms=R2D_STALE_MS + 3 * TICK_MS)
    assert parked.state() == WAIT_START_BRAKE, "a DV R2D entry it should have refused"
    assert all(f.data[0] == 0 for f in parked.acu.frames([R2D_CONFIRM], since_us=t - 500_000))


# -- DV entry ---------------------------------------------------------------------

def test_dv_entry_r2d_latch_and_drive(car):
    """L-005: 0x510 + hard braking -> R2dDelay at once, RTDS for 2 s, 0x511 = 1
    from the next 100 ms slot; the inverter climbs and Active drives 0x507."""
    _to_wait_start_brake(car)
    pin = car.io.watch("sysbus.gpioPortB", 4)
    car.brake(BRAKE_HARD)
    car.sim.run_for(ms=50)
    assert car.state() == WAIT_START_BRAKE, "entered without a request"
    t_req = car.sim.now_us()
    car.acu.send_periodic("r2d", UDV_R2D, b"\x01", 50)
    car.sim.run_for(ms=2 * TICK_MS)
    assert car.state() == R2D_DELAY, "no R2D within two ticks of the request"
    car.sim.run_for(ms=UDV_PERIOD_MS)
    assert _confirm(car, t_req) == 1
    car.sim.run_for(ms=R2D_SOUND_MS)
    edges = car.io.edges(pin)
    assert len(edges) >= 2 and edges[0].level and not edges[1].level
    assert abs((edges[1].t_us - edges[0].t_us) / 1000 - R2D_SOUND_MS) <= TICK_MS
    assert car.state() == WAIT_INV_STANDBY
    car.acu.send_periodic("cmd", UDV_TORQUE, (40).to_bytes(4, "little"), 10)
    car.follow(100)
    assert car.state() == ACTIVE and car.inv_state == INV_TORQUE_EN
    t = car.sim.now_us()
    car.follow(100)
    assert {nm for _, nm in car.torques(since_us=t)} == {_nm(40)}


@pytest.mark.parametrize("brake_after_ms, enters", [
    (R2D_STALE_MS - 3 * TICK_MS, True),
    (R2D_STALE_MS + 3 * TICK_MS, False),
])
def test_a_stale_dv_request_is_refused(car, brake_after_ms, enters):
    """L-006: one 0x510, then hard braking brake_after_ms later: honoured while
    the request is within 200 ms, refused once it is older."""
    _to_wait_start_brake(car)
    car.acu.send(UDV_R2D, b"\x01")
    car.sim.run_for(ms=brake_after_ms)
    car.brake(BRAKE_HARD)
    car.sim.run_for(ms=200)
    assert (car.state() == R2D_DELAY) == enters


def test_manual_r2d_takes_precedence(car):
    """L-009: START (debounced) and a fresh 0x510 both true on the tick the
    brake goes hard: the manual gate wins, no DV latch, and in Active the
    torque is the pedals', not 0x507's."""
    _to_wait_start_brake(car)
    car.start(True)
    car.acu.send_periodic("r2d", UDV_R2D, b"\x01", 50)
    car.acu.send_periodic("cmd", UDV_TORQUE, (60).to_bytes(4, "little"), 10)
    car.sim.run_for(ms=200)
    assert car.state() == WAIT_START_BRAKE
    t = car.sim.now_us()
    car.brake(BRAKE_HARD)
    car.sim.run_for(ms=R2D_SOUND_MS + 100)
    car.start(False)
    car.brake(BRAKE_RELEASED)
    car.follow(200)
    assert car.state() == ACTIVE
    assert all(f.data[0] == 0 for f in car.acu.frames([R2D_CONFIRM], since_us=t)), "DV latched"
    t = car.sim.now_us()
    car.follow(100)
    assert {nm for _, nm in car.torques(since_us=t)} == {0}, "0x507 drove a manual car"


# -- DV torque --------------------------------------------------------------------

@pytest.fixture(scope="module")
def driving(images):
    with _boot(images) as sim:
        car = _Car(sim)
        _to_dv_active(car)
        yield car


@pytest.mark.parametrize("pct", [40, 100, 150, -20, 3, 5])
def test_dv_torque_follows_0x507_not_the_pedals(driving, pct):
    """L-007 / L-010: in DV Active 0x362 is the conditioned 0x507 (clamped to
    0..100, 0 Nm under 5 %) through the firmware map, with the APPS floored."""
    car = driving
    car.throttle(1.0)
    car.acu.update_periodic("cmd", pct.to_bytes(4, "little", signed=True))
    car.follow(50)
    t = car.sim.now_us()
    car.follow(100)
    got = {nm for _, nm in car.torques(since_us=t)}
    car.throttle(0.0)
    car.acu.update_periodic("cmd", (40).to_bytes(4, "little"))
    car.follow(50)
    assert car.state() == ACTIVE
    assert got == {_nm(pct)}


def test_a_stale_0x507_stream_is_zero_torque_never_apps(car):
    """L-008: the last 0x507, APPS floored: torque holds while the command is
    within 100 ms, then 0, and stays 0 (no fall-back to the pedals)."""
    _to_dv_active(car)
    car.throttle(1.0)
    car.follow(50)
    car.acu.stop_periodic("cmd")
    car.acu.send(UDV_TORQUE, (40).to_bytes(4, "little"))
    t0 = car.sim.now_us()
    car.follow(500)
    early = {nm for t, nm in car.torques(since_us=t0, until_us=t0 + (CMD_STALE_MS - 2 * TICK_MS) * 1000)}
    late = {nm for t, nm in car.torques(since_us=t0 + (CMD_STALE_MS + 2 * TICK_MS) * 1000)}
    assert early == {_nm(40)} and late == {0}
    assert car.state() == ACTIVE


def test_hard_braking_does_not_cut_dv_torque(car):
    """L-011: the EBS braking hard during a DV drive does not cut its torque
    (EV.2.3 was removed from the firmware, control.cpp:45-57; D1)."""
    _to_dv_active(car)
    car.brake(4000)
    car.follow(50)
    t = car.sim.now_us()
    car.follow(200)
    assert {nm for _, nm in car.torques(since_us=t)} == {_nm(40)}


# -- the latch --------------------------------------------------------------------

def test_the_latch_survives_an_inverter_redrive(car):
    """L-012: Active -> WaitInvStandby -> Active (inverter dropped to Standby)
    stays the same drive cycle: 0x511 keeps confirming, 0x507 keeps driving."""
    _to_dv_active(car)
    t = car.sim.now_us()
    car.set_inverter(INV_STANDBY)
    car.sim.run_for(ms=3 * TICK_MS)
    assert car.state() == WAIT_INV_STANDBY
    car.follow(200)
    assert car.state() == ACTIVE
    assert all(f.data[0] == 1 for f in car.acu.frames([R2D_CONFIRM], since_us=t))
    t = car.sim.now_us()
    car.follow(100)
    assert {nm for _, nm in car.torques(since_us=t)} == {_nm(40)}


def test_leaving_the_drive_cycle_clears_the_latch(car):
    """L-012: the AMS drops ok_precharge in DV Active -> Precharge, the latch
    clears (0x511 = 0); back in WaitStartBrake a manual R2D is not DV."""
    _to_dv_active(car)
    car.acu.update_periodic("ok", b"\x00")
    car.follow(3 * TICK_MS)
    assert car.state() == PRECHARGE
    t = car.sim.now_us()
    car.follow(UDV_PERIOD_MS + 2 * TICK_MS)
    assert _confirm(car, t) == 0
    car.acu.update_periodic("ok", b"\x01")
    car.follow(50)
    assert car.state() == WAIT_START_BRAKE
    t = car.sim.now_us()
    car.brake(BRAKE_FIRM)
    car.start(True)
    car.sim.run_for(ms=R2D_SOUND_MS + 200)
    car.start(False)
    car.brake(BRAKE_RELEASED)
    car.follow(200)
    assert car.state() == ACTIVE
    assert all(f.data[0] == 0 for f in car.acu.frames([R2D_CONFIRM], since_us=t))
    t = car.sim.now_us()
    car.follow(100)
    assert {nm for _, nm in car.torques(since_us=t)} == {0}, "0x507 still drives"


def test_ams_error_preempts_a_dv_drive(car):
    """L-013: a fresh AMS Error in DV Active: AmsError within a tick, 0x362 0
    and 0x360 Off from that tick on, 0x511 = 0 at its next slot."""
    _to_dv_active(car)
    car.acu.stop_periodic("status")
    car.acu.send(AMS_STATUS, _status(5))
    t0 = car.sim.now_us()
    car.acu.send_periodic("status", AMS_STATUS, _status(5), 50)
    car.sim.run_for(ms=2 * TICK_MS)
    assert car.state() == AMS_ERROR
    t1 = car.sim.now_us()
    car.sim.run_for(ms=UDV_PERIOD_MS + 2 * TICK_MS)
    assert {nm for _, nm in car.torques(since_us=t1)} == {0}
    assert {f.data[2] & 0x7F for f in car.inv.frames([INV_CMD], since_us=t1)} == {W_OFF}
    assert _confirm(car, t1) == 0
    first_zero = [t for t, nm in car.torques(since_us=t0) if nm == 0]
    assert first_zero and first_zero[0] - t0 <= 2 * TICK_MS * 1000
