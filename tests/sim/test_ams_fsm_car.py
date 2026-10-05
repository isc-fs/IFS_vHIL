"""ams-fsm-car (#30): the AMS state machine in Car mode, in virtual time, the
ECU heartbeat and the cockpit inputs scripted (ams_car.Car).

AMS facts (IFS08-CE-AMS state_machine.hpp, safety_task.cpp, ams_config.hpp):
  Start -> Precharge on a DASH_CHG rising edge while TSMS is held; the press
    is consumed every FSM step, used or not, and the mode locks on that step:
    Car unless a fresh 0x101 request meets a silent VCU.
  Precharge -> Transition when the fresh, valid link reaches 95 % of the pack
    (cell sum); -> Error (FsmError, 12) after PrechargeMaxMs = 5000.
  Transition -> Run on the next step if the link still holds 95 %, else Error.
  Run: a TSMS drop, or the link below BusCollapsePercent = 50 % of the pack
    for BusCollapseConfirmTicks = 20 (200 ms), de-energises to Start without
    latching; DASH_CHG is ignored.
  Re-arm: refused while the ECU reports the bleed connected, or (once it has
    sent a DLC-3 0x100) while the link is above DcBusDischargedV = 60 V.
  VcuStale (11): Car-locked and 0x100 older than VcuStaleMs = 200.
  Error is sticky for the boot; the mode lock is kept.
  0x4A0[0] state; 0x4A2[5] cockpit byte: bit 7 sentinel, 3:2 mode lock,
  1 TSMS, 0 DASH_CHG.
"""
import pytest

from ams_car import (AMS_OK, Car, DASH_CHG, ERROR, GPIOB, GPIOF, PACK_V, PRECHARGE,
                     RUN, START, TRANSITION)
from vhil.sim import Sim
from vhil.system import REPO

FSM_ERROR, VCU_STALE, UNDERVOLTAGE = 12, 11, 4
STATUS, TEMPS = 0x4A0, 0x4A2
BOOT_MS = 3000
TARGET_V = 334          # 95 % of 351.5 V, rounded up
COLLAPSE_V = 175        # < 50 % of 351.5 V


@pytest.fixture
def car(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=BOOT_MS)
        yield Car(sim)


def test_tsms_alone_stays_in_start(car):
    """C-030."""
    car.vcu(0)
    car.tsms(True)
    car.sim.run_for(ms=1000)
    assert car.state() == START


def test_a_press_without_tsms_is_spent(car):
    """C-031: the press is consumed on the next FSM step, so TSMS coming on
    afterwards, with the button still held, does not arm."""
    car.vcu(0)
    car.dash(True)
    car.sim.run_for(ms=100)
    car.tsms(True)
    car.sim.run_for(ms=500)
    assert car.state() == START


def test_tsms_and_a_press_lock_car_and_precharge(car):
    """C-032: Precharge within one sample and one FSM step of the press,
    Car locked."""
    car.vcu(0)
    car.tsms(True)
    car.sim.run_for(ms=100)
    car.dash(True)
    elapsed = car.wait_for(PRECHARGE, 100, step_ms=2)
    assert elapsed is not None and elapsed <= 30, f"Precharge after {elapsed} ms"
    assert car.mode() == 1


def test_a_button_held_from_reset_is_not_a_press(images):
    """C-032b."""
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.io("ams").set_input(GPIOF, DASH_CHG, True)
        sim.wait_for_app()
        sim.run_for(ms=BOOT_MS)
        car = Car(sim)
        car.vcu(0)
        car.tsms(True)
        sim.run_for(ms=500)
        assert car.state() == START


def test_precharge_completes_at_95_percent(car):
    """C-033: 94.7 % holds Precharge, 95.0 % goes through Transition to Run."""
    car.arm()
    car.vcu(TARGET_V - 1)
    car.sim.run_for(ms=1000)
    assert car.state() == PRECHARGE, "left Precharge below 95 %"
    car.vcu(TARGET_V)
    elapsed = car.wait_for(RUN, 100)
    assert elapsed is not None and elapsed <= 60, f"Run after {elapsed} ms"


def test_precharge_times_out_into_error(car):
    """C-034: a link that never rises -> FsmError at 5 s."""
    car.arm()
    elapsed = car.wait_for(ERROR, 5200, step_ms=10)
    assert elapsed is not None and 4950 <= elapsed <= 5060, f"Error after {elapsed} ms"
    assert car.reason() == FSM_ERROR


def test_the_transition_guard_catches_a_link_that_drops(car):
    """Transition re-checks 95 %: a link lost on the swap step -> Error 12."""
    car.arm()
    car.vcu(round(PACK_V))
    assert car.wait_for(TRANSITION, 200, step_ms=1) is not None, "Transition never seen"
    car.vcu(0)
    assert car.wait_for(ERROR, 100) is not None, f"state {car.state()}"
    assert car.reason() == FSM_ERROR


def test_a_tsms_drop_in_run_de_energises_without_latching(car):
    """C-039a: Run -> Start, AMS_OK held, mode cleared; the re-arm waits for
    the link to drain below 60 V, then runs again."""
    ok = car.io.watch(GPIOB, AMS_OK)
    car.to_run()
    t = car.sim.now_us()
    car.tsms(False)
    assert car.wait_for(START, 100) is not None
    assert car.reason() == 0 and car.mode() == 0
    assert car.io.level(ok) and car.io.edges(ok, since_us=t) == [], "AMS_OK moved on a TSMS drop"
    car.tsms(True)
    car.sim.run_for(ms=100)
    car.press()
    car.sim.run_for(ms=200)
    assert car.state() == START, "re-armed onto a charged link"
    car.vcu(60)
    car.sim.run_for(ms=100)
    car.press()
    assert car.wait_for(PRECHARGE, 100) is not None, "no re-arm on a drained link"


def test_the_bleed_interlock_refuses_to_arm(car):
    """Re-arm gate: the ECU reporting the bleed connected blocks the press,
    whatever the link reads."""
    car.vcu(0, discharge=True)
    car.tsms(True)
    car.sim.run_for(ms=100)
    car.press()
    car.sim.run_for(ms=300)
    assert car.state() == START
    car.vcu(0)
    car.sim.run_for(ms=100)             # the ECU's next frames clear the flag
    car.press()
    assert car.wait_for(PRECHARGE, 100) is not None


@pytest.mark.xfail(strict=True, reason="isc-fs/IFS08-CE-AMS#616: a refused arm keeps the "
                                       "Car lock in Start, so VcuStale applies there")
def test_a_refused_arm_does_not_leave_car_locked(car):
    """A press the re-arm gate refuses is spent, and so should be the mode
    lock it took: a VCU that then goes quiet in Start is no fault."""
    car.vcu(0, discharge=True)
    car.tsms(True)
    car.sim.run_for(ms=100)
    car.press()
    car.sim.run_for(ms=300)
    assert car.state() == START
    car.vcu_silent()
    car.sim.run_for(ms=1000)
    assert (car.state(), car.mode()) == (START, 0)


def test_run_ignores_the_button(car):
    """C-039b."""
    car.to_run()
    for _ in range(3):
        car.press()
        car.sim.run_for(ms=100)
    assert car.state() == RUN


@pytest.mark.parametrize("dip_ms, expected", [(170, RUN), (260, START)],
                         ids=["brief-dip", "collapse"])
def test_a_collapsed_link_returns_to_start(car, dip_ms, expected):
    """C-049, C-050: below 50 % for 200 ms -> Start (unlatched); shorter
    stays in Run."""
    car.to_run()
    car.vcu(COLLAPSE_V)
    car.sim.run_for(ms=dip_ms)
    car.vcu(round(PACK_V))
    car.sim.run_for(ms=100)
    assert car.state() == expected
    assert car.reason() == 0


def test_a_silent_vcu_in_run_faults_vcu_stale(car):
    """B-021, B-029-VCU, C-041: 0x100 stops in Run -> VcuStale after 200 ms,
    and the Car lock survives into Error."""
    car.to_run()
    car.vcu_silent()
    elapsed = car.wait_for(ERROR, 400)
    assert elapsed is not None and 200 <= elapsed <= 240, f"Error after {elapsed} ms"
    assert car.reason() == VCU_STALE
    assert car.mode() == 1


def test_a_silent_vcu_matters_only_once_car_is_locked(car):
    """B-027, C-038: no VCU in Start is fine; a press with no VCU and no
    charge request locks Car, which then faults VcuStale."""
    car.tsms(True)
    car.sim.run_for(ms=2000)
    assert car.state() == START
    car.press()
    assert car.wait_for(ERROR, 500) is not None, f"state {car.state()}"
    assert (car.reason(), car.mode()) == (VCU_STALE, 1)


def test_error_is_sticky(car):
    """C-043: once latched, clearing the cause and re-arming change nothing."""
    car.arm()
    assert car.wait_for(ERROR, 5200, step_ms=20) is not None
    car.vcu(0)
    car.tsms(False)
    car.sim.run_for(ms=500)
    car.tsms(True)
    car.press()
    car.sim.run_for(ms=10_000)
    assert car.state() == ERROR


def test_status_and_cockpit_frames_follow_the_state(car):
    """C-042, K-100: 0x4A0[0] and the 0x4A2[5] cockpit byte through Start,
    Precharge, Run and Error (via a cell under-voltage)."""
    can = car.can

    def frames():
        car.sim.run_for(ms=600)
        return can.last(STATUS).data[0], can.last(TEMPS).data[5]

    car.vcu(0)
    assert frames() == (START, 0x80)
    car.tsms(True)
    assert frames() == (START, 0x82)
    car.press()
    assert frames() == (PRECHARGE, 0x86)
    car.vcu(round(PACK_V))
    assert frames() == (RUN, 0x86)
    car.sim.monitor("sysbus.spi1.isospi.cells0 SetCell 0 2700", board="ams")
    car.sim.run_for(ms=500)
    assert frames() == (ERROR, 0x86)
    assert car.reason() == UNDERVOLTAGE
