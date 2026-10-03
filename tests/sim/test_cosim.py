"""The co-simulation port (M6, #14): scripted plants drive the ECU's pedals
and inverter in closed loop, deterministically in virtual time.

ECU facts (IFS08-CE-ECU):
  control.cpp: WaitStartBrake -> R2dDelay on START + brake past BrakeArmRaw;
    the RTDS buzzer (PB4) sounds for R2dSoundMs = 2000; then WaitInvStandby
    commands Ready(0x04) until the inverter reports Ready(4), then Active
    commands TorqueEnable(0x06) and torque; a hard fault (11) gets
    HardFaultReset(0x0D) then Off(0x01), and once the inverter is back in
    Standby the ECU climbs it to Ready again (inv_redrive_count).
  inverter.cpp: torque_to_nm_req maps 5 %..100 % to 0..240 Nm, negated
    (forward drive is negative).
"""
import pytest

from vhil.cosim import Port
from vhil.plants import AcuStimulus, Inverter, Pedals
from vhil.sim import Sim
from vhil.system import REPO

R2D_SOUND_MS = 2000
TORQUE_MAX_NM = 240


def _driver(t):
    """Brake firm 0.5-3.0 s; START pressed 0.8-1.2 s; 50 % throttle 3.5-4.5 s.
    (Lock-step costs ~15 s of wall time per virtual second: keep it short.)"""
    throttle = 0.5 if 3.5 <= t < 4.5 else 0.0
    return throttle, 1.0 if 0.5 <= t < 3.0 else 0.0, 0.8 <= t < 1.2


def _drive(firmware, ms=5000, during=None):
    """Run the scenario; returns (inverter, frame trace of the ECU's setpoints,
    RTDS edges, torques seen)."""
    with Sim(REPO / "systems" / "ecu.yaml", {"ecu": firmware("ecu")}) as sim:
        port, inv = Port(sim), Inverter()
        plants = [AcuStimulus(), Pedals(_driver), inv]
        torques = []
        step = 250
        for t in range(0, ms, step):
            if during:
                during(t, inv)
            port.run(plants, step)
            torques.append((t + step, inv.torque_nm))
        setpoints = [(f.t_ms, f.id, f.data.hex()) for f in sim.can("can_inv").frames([0x360, 0x362])]
        rtds = [(e.t_us, e.level) for e in sim.io("ecu").edges("sysbus.gpioPortB:4")]
        return inv, setpoints, rtds, torques


@pytest.fixture(scope="module")
def drive(firmware):
    return _drive(firmware)


def test_ready_to_drive_from_the_driver(drive):
    """START + brake: the RTDS sounds for 2 s, then the ECU climbs the
    inverter to Ready and TorqueEnable."""
    inv, _, rtds, _ = drive
    (on_us, on), (off_us, off) = rtds[:2]
    assert on and not off and 0.8e6 <= on_us <= 0.9e6, f"RTDS edges {rtds}"
    assert abs((off_us - on_us) / 1000 - R2D_SOUND_MS) <= 20
    states = [s for _, s in inv.history]
    assert states[:2] == [Inverter.READY, Inverter.TORQUE], f"inverter went {states}"
    assert inv.history[0][0] >= off_us, "inverter Ready before the RTDS ended"


def test_throttle_gives_forward_torque_and_speed(drive):
    inv, _, _, torques = drive
    during = [nm for t, nm in torques if 3750 <= t <= 4500]
    assert during and all(-0.6 * TORQUE_MAX_NM <= nm <= -0.4 * TORQUE_MAX_NM for nm in during), \
        f"torque at 50 % throttle: {during}"
    assert torques[-1][1] == 0, "torque still requested after the throttle lifted"
    assert inv.rpm > 1000, f"motor at {inv.rpm:.0f} rpm"


def test_an_inverter_trip_is_recovered_in_the_loop(firmware):
    """A hard fault mid-drive: the ECU's reset words bring the inverter back
    to Standby, and the ECU climbs it to TorqueEnable again."""
    def trip(t, inv):
        if t == 4000:
            inv.fault(hard=True, t_us=4_000_000)
    inv, _, _, _ = _drive(firmware, during=trip)
    after = [(t, s) for t, s in inv.history if t >= 4_000_000]
    assert [s for _, s in after][:4] == [Inverter.HARD, Inverter.STANDBY, Inverter.READY,
                                         Inverter.TORQUE], f"after the trip: {after}"
    assert after[3][0] - after[0][0] <= 200_000, "recovery took longer than 200 ms"


def test_the_loop_is_deterministic(firmware, drive):
    """The same scenario twice: the same setpoint frames at the same virtual
    times, the same inverter history."""
    inv, setpoints, rtds, _ = drive
    inv2, setpoints2, rtds2, _ = _drive(firmware)
    assert inv2.history == inv.history and rtds2 == rtds
    assert setpoints2 == setpoints
