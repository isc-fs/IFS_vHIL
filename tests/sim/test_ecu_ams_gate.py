"""ecu-ams-gate (#42): the ECU's view of the AMS, in virtual time.

ECU facts (IFS08-CE-ECU):
  control_task.cpp:117-120: ams_fresh = a frame from the AMS within
    AmsStaleMs = 200 (ecu_config.hpp:488); ok_precharge = 0x020 && fresh;
    ams_error = 0x4A0 state == AmsFsmError (5) && fresh.
  vehicle_service.cpp: 0x020 and 0x4A0 refresh last_ams_tick; uDV frames
    (0x507, 0x510...) deliberately do not.
  0x504 VCU_ts_active (100 ms, vcu_ts_active.def) = ok_precharge && fresh.
  control.cpp: AmsError entered from any state while ams_error; left to
    WaitInvVdcConfig (0) when it clears. g_last_ctrl_state mirrors the FSM
    state every control tick (CtrlState: AmsError = 6).
The AMS's frames here are stimulus: ecu.yaml has no AMS (sys-ecu-ams runs the
real one).
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

OK_PRECHARGE, AMS_STATUS, TS_ACTIVE = 0x020, 0x4A0, 0x504
STALE_MS, TICK_MS = 200, 10
WAIT_INV_VDC, WAIT_START_BRAKE, AMS_ERROR = 0, 2, 6


def _status(state):
    return bytes([state, 1 if state != 5 else 0, 0x1F, 0, 0x0E, 0x74, 0x0E, 0x74])


@pytest.fixture
def ecu(firmware):
    with Sim(REPO / "systems" / "ecu.yaml", {"ecu": firmware("ecu")}) as sim:
        yield sim


def _ts_active_after_last_frame(ecu, lead_ms, udv=False):
    """Feed the AMS, then send its last frame lead_ms before a 0x504 TX;
    return what that 0x504 reports."""
    acu = ecu.can("can_acu")
    acu.send_periodic("ams", OK_PRECHARGE, b"\x01", 10)
    ecu.run_for(ms=1000)
    beats = acu.frames([TS_ACTIVE])
    assert beats and beats[-1].data[0] == 1, "ts_active never rose"
    period = beats[-1].t_us - beats[-2].t_us
    target = beats[-1].t_us + 5 * period                 # a 0x504 TX well ahead
    ecu.run_for(us=target - lead_ms * 1000 - ecu.now_us())
    acu.stop_periodic("ams")
    acu.send(OK_PRECHARGE, b"\x01")                      # the AMS's last frame, now
    if udv:
        acu.send_periodic("udv", 0x507, bytes(8), 10)    # uDV traffic keeps flowing
    ecu.run_for(us=target - ecu.now_us() + 2000)
    tx = [f for f in acu.frames([TS_ACTIVE]) if abs(f.t_us - target) <= 1000]
    assert tx, f"no 0x504 at {target} us"
    return tx[0].data[0]


@pytest.mark.parametrize("lead_ms, expected", [
    (STALE_MS - 2 * TICK_MS, 1),     # last AMS frame 180 ms before: still fresh
    (STALE_MS + 2 * TICK_MS, 0),     # 220 ms before: stale, ts_active dropped
])
def test_ams_freshness_edge_is_200_ms(ecu, lead_ms, expected):
    """F-003 / L-001: ok_precharge lapses 200 ms (± 2 ticks) after the last AMS frame."""
    assert _ts_active_after_last_frame(ecu, lead_ms) == expected


def test_udv_traffic_does_not_keep_the_ams_fresh(ecu):
    """L-014: uDV frames flowing don't refresh the AMS: stale at the same edge."""
    assert _ts_active_after_last_frame(ecu, STALE_MS + 2 * TICK_MS, udv=True) == 0


def _state(ecu):
    return ecu.read_symbol("ecu", "g_last_ctrl_state")


@pytest.mark.parametrize("reach", ["WaitInvVdcConfig", "WaitStartBrake"])
def test_ams_error_enters_amserror_within_a_tick_and_rearms(ecu, reach):
    """F-001: a fresh AMS Error (0x4A0 state 5) puts the ECU in AmsError
    within a control tick, from any state; F-002: AMS ok again leaves it for
    exactly WaitInvVdcConfig."""
    acu, inv = ecu.can("can_acu"), ecu.can("can_inv")
    acu.send_periodic("ams", OK_PRECHARGE, b"\x01", 10)
    acu.send_periodic("status", AMS_STATUS, _status(0), 50)
    if reach == "WaitStartBrake":
        inv.send_periodic("vdc", 0x466, bytes([0, 0, 0x5E, 0x01, 0, 0]), 10)   # 350 V
    ecu.run_for(ms=1500)
    assert _state(ecu) == {"WaitInvVdcConfig": WAIT_INV_VDC, "WaitStartBrake": WAIT_START_BRAKE}[reach]
    acu.stop_periodic("status")
    acu.send(AMS_STATUS, _status(5))
    ecu.run_for(ms=2 * TICK_MS)
    assert _state(ecu) == AMS_ERROR, "not in AmsError within two ticks"
    acu.send_periodic("status", AMS_STATUS, _status(5), 50)
    ecu.run_for(ms=500)
    assert _state(ecu) == AMS_ERROR, "left AmsError while the AMS is still in Error"
    acu.stop_periodic("status")
    acu.send_periodic("status", AMS_STATUS, _status(0), 50)
    seen = []
    for _ in range(10):                                   # every tick from the clear
        ecu.run_for(ms=TICK_MS)
        seen.append(_state(ecu))
    left = [s for s in seen if s != AMS_ERROR]
    assert left and left[0] == WAIT_INV_VDC, f"left AmsError via {seen}"
