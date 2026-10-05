"""ecu-inverter (#43): what the ECU commands the traction inverter, cycle by
cycle, in virtual time.

ECU facts (IFS08-CE-ECU, /vhil/fw/ecu@dev):
  control_task.cpp:344-360: every 10 ms control tick posts 0x360 (mode),
    then the fault-recovery follow words, then 0x362 (torque), in that order;
    FDCAN1 is in TX FIFO mode (fdcan.c:68), so the wire order is the post order.
  control.cpp:249-309: healthy words: Off(0x01) before R2D and in AmsError,
    Ready(0x04) in WaitInvStandby (Off then Ready from inv 0; Off from 13),
    TorqueEnable(0x06) + torque in Active while inv_state < 10.
  control.cpp:327-340: in any state but AmsError, inv 11 -> [0x0D, Off];
    inv 10 -> [0x13, 0x0D, Off]; Flt_Clear (0x360 byte 2 bit 7,
    ecu_config.hpp:632) rides the last word only (control_task.cpp:352-358),
    so it rises once per cycle.
  control.cpp:187-233: Active -> WaitInvStandby when inv_state leaves
    {Ready 4, TorqueEnable 6} with the TS up; ++inv_redrive_count, reported on
    0x708 byte 6 (pit_diag_inv_faults.def:81); no RTDS on the way back; drive
    resumes on Ready with no driver action (team decision, control.cpp:223-230).
  0x708 byte 3: cmd_follow_n (bits 0-1), cmd_flt_clear (bit 2); byte 4:
    ms since the last 0x461, saturating at 255 (pit_diag.cpp:247-252).
  control_task.cpp:92: inv_present = an inverter frame within InvStaleMs = 200
    (ecu_config.hpp:503), but Controller::step never reads it (control.cpp):
    a silent inverter in Active keeps its last inv_state and keeps being
    commanded TorqueEnable and torque. Strict xfail below.
The inverter's 0x461/0x466 here are stimulus, except in the boot-latched
recovery, which runs the Inverter plant (vhil/plants.py) in lock-step.

IFS_HIL drift (docs/analysis/ifs-hil-tests-ecu.md D2): E-003, E-004, E-005 and
E-006 assert the LAST 0x360 is the reset word (0x0D / 0x13). In a faulted
cycle the last 0x360 is always Off|Flt_Clear (0x81); the reset word is first.
The tests here assert the whole ordered burst instead.
"""
import pytest

from vhil.cosim import Port
from vhil.plants import (APPS1_FULL, APPS1_REST, APPS2_FULL, APPS2_REST, BRAKE_FIRM,
                         BRAKE_RELEASED, COUNTS_TO_V, AcuStimulus, Inverter, Pedals)
from vhil.sim import Sim
from vhil.system import REPO

MODE, TORQUE, INV_STATE, VDC = 0x360, 0x362, 0x461, 0x466
OK_PRECHARGE, AMS_STATUS, PIT_ARM, INV_FAULTS = 0x020, 0x4A0, 0x7E0, 0x708
OFF, READY, TORQUE_ENABLE, FAULT, HARD_FAULT_RESET = 0x01, 0x04, 0x06, 0x13, 0x0D
FLT_CLEAR = 0x80
INV_STANDBY, INV_READY, INV_TORQUE, INV_SOFT, INV_HARD = 3, 4, 6, 10, 11
WAIT_VDC, PRECHARGE, WAIT_START_BRAKE, R2D_DELAY, WAIT_INV_STANDBY, ACTIVE, AMS_ERROR = range(7)
TICK_MS, INV_STALE_MS, R2D_SOUND_MS = 10, 200, 2000
BURST = {INV_HARD: [HARD_FAULT_RESET, OFF | FLT_CLEAR],
         INV_SOFT: [FAULT, HARD_FAULT_RESET, OFF | FLT_CLEAR]}


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        sim.wait_for_app()                      # past the bootloader's window
        yield sim


# -- stimulus -------------------------------------------------------------------

def _inv(ecu, state):
    """The inverter's 0x461 every 10 ms, App_State_App in byte 4 (a new state
    replaces the old one's job: VhilProbe.SendPeriodic keys on "inv")."""
    ecu.can("can_inv").send_periodic("inv", INV_STATE, bytes([0, 0, 0, 0, state & 0x7F, 0, 0]), 10)


def _vdc(ecu):
    ecu.can("can_inv").send_periodic("vdc", VDC, bytes([0, 0, 0x5E, 0x01, 0, 0]), 10)  # 350 V


def _silence_inverter(ecu):
    bus = ecu.can("can_inv")
    bus.stop_periodic("inv")
    bus.stop_periodic("vdc")


def _status(state):
    return bytes([state, 1 if state != 5 else 0, 0x1F, 0, 0x0E, 0x74, 0x0E, 0x74])


def _ams(ecu, fsm_state=0):
    acu = ecu.can("can_acu")
    acu.send_periodic("ams", OK_PRECHARGE, b"\x01", 10)
    acu.send_periodic("status", AMS_STATUS, _status(fsm_state), 50)


def _pedals(ecu, throttle=0.0, brake=0.0):
    io = ecu.io("ecu")
    io.set_voltage("PF8", (APPS1_REST + throttle * (APPS1_FULL - APPS1_REST)) * COUNTS_TO_V)
    io.set_voltage("PF9", (APPS2_REST + throttle * (APPS2_FULL - APPS2_REST)) * COUNTS_TO_V)
    io.set_voltage("PF7", (BRAKE_RELEASED + brake * (BRAKE_FIRM - BRAKE_RELEASED)) * COUNTS_TO_V)


# -- observation ----------------------------------------------------------------

def _state(ecu):
    return ecu.read_symbol("ecu", "g_last_ctrl_state")


def _cycles(ecu, since_us, until_us=None):
    """The ECU's inverter setpoints since since_us, one tuple per control
    cycle: 0x360 byte 2 for each mode word, then ('T', Nm) for the 0x362 that
    closes the cycle. Partial cycles at either end are dropped."""
    frames = [f for f in ecu.can("can_inv").frames([MODE, TORQUE], since_us=since_us)
              if until_us is None or f.t_us < until_us]
    cycles, cur, t0 = [], [], []
    for f in frames:
        if not cur:
            t0.append(f.t_us)
        if f.id == MODE:
            cur.append(f.data[2])
        else:
            assert len(f.data) == 4 and f.data[:2] == b"\0\0", f"0x362 {f.data.hex()}"
            cur.append(("T", int.from_bytes(f.data[2:4], "little", signed=True)))
            cycles.append((t0[-1], f.t_us, tuple(cur)))
            cur = []
    cycles = cycles[1:]                               # the first may have started earlier
    for start, end, _ in cycles:
        assert end - start <= 1000, f"a cycle's frames spread over {end - start} us"
    return [(start, words) for start, _, words in cycles]


def _words(cycles):
    return [w for _, w in cycles]


def _inv_faults(ecu, since_us=0):
    f = ecu.can("can_acu").last(INV_FAULTS, since_us)
    assert f is not None and len(f.data) == 7, "no 0x708 (pit stream armed?)"
    return {"follow_n": f.data[3] & 0x3, "flt_clear": (f.data[3] >> 2) & 1,
            "age_ms": f.data[4], "redrive": f.data[6]}


def _arm_pit(ecu):
    """Arm the pit-diag stream once the ECU is up (0x7E0, can_rx_task.cpp:77)."""
    ecu.run_for(ms=200)
    ecu.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))


def _to_active(ecu, throttle):
    """Climb the ladder with a scripted inverter: Standby, then Ready once the
    ECU asks, then TorqueEnable once it asks; returns with throttle applied."""
    _inv(ecu, INV_STANDBY)
    _vdc(ecu)
    _ams(ecu)
    _pedals(ecu)
    ecu.run_for(ms=1000)
    if _state(ecu) != WAIT_START_BRAKE:
        raise RuntimeError(f"stuck in state {_state(ecu)} before R2D")
    _pedals(ecu, brake=1.0)
    ecu.run_for(ms=50)
    io = ecu.io("ecu")
    io.set_input("sysbus.gpioPortB", 5, True)
    ecu.run_for(ms=100)
    io.set_input("sysbus.gpioPortB", 5, False)
    _pedals(ecu)
    ecu.run_until(lambda: _state(ecu) == WAIT_INV_STANDBY, timeout_ms=R2D_SOUND_MS + 200)
    _inv(ecu, INV_READY)
    ecu.run_until(lambda: _state(ecu) == ACTIVE, timeout_ms=100)
    _inv(ecu, INV_TORQUE)
    _pedals(ecu, throttle=throttle)
    ecu.run_for(ms=300)


# -- tests ----------------------------------------------------------------------

def test_healthy_cycle_is_one_mode_word_then_torque(ecu):
    """Healthy inverter before R2D: every 10 ms exactly [Off, 0x362 0 Nm],
    never a follow word or Flt_Clear."""
    _inv(ecu, INV_STANDBY)
    _vdc(ecu)
    _ams(ecu)
    t = ecu.run_for(ms=1000)
    assert _state(ecu) == WAIT_START_BRAKE
    ecu.run_for(ms=500)
    cycles = _cycles(ecu, t)
    assert len(cycles) >= 48
    assert set(_words(cycles)) == {(OFF, ("T", 0))}
    starts = [s for s, _ in cycles]
    assert {b - a for a, b in zip(starts, starts[1:])} == {TICK_MS * 1000}


@pytest.mark.parametrize("inv_state, reach", [(INV_HARD, "WaitInvVdcConfig"),
                                              (INV_SOFT, "WaitStartBrake")])
def test_fault_burst_is_ordered_every_cycle(ecu, inv_state, reach):
    """E-003 / E-004: a latched inverter fault gets, in every cycle and in the
    pre-Active states, its reset word(s) then Off|Flt_Clear, then 0 Nm; 0x708
    mirrors the commanded side. (IFS_HIL asserts the last 0x360 is the reset
    word: drift D2.)"""
    _arm_pit(ecu)
    _inv(ecu, inv_state)
    _ams(ecu)
    if reach == "WaitStartBrake":
        _vdc(ecu)
    t = ecu.run_for(ms=1000)
    assert _state(ecu) == {"WaitInvVdcConfig": WAIT_VDC, "WaitStartBrake": WAIT_START_BRAKE}[reach]
    ecu.run_for(ms=500)
    cycles = _cycles(ecu, t)
    assert len(cycles) >= 48
    assert set(_words(cycles)) == {tuple(BURST[inv_state]) + (("T", 0),)}
    pit = _inv_faults(ecu, t)
    assert (pit["follow_n"], pit["flt_clear"], pit["redrive"]) == (len(BURST[inv_state]) - 1, 1, 0)


def test_flt_clear_rises_once_per_cycle_only_while_faulted(ecu):
    """Flt_Clear is low-high within each faulted cycle (a fresh rising edge at
    100 Hz), on the Off word only, and gone with the fault."""
    _inv(ecu, INV_STANDBY)
    _ams(ecu)
    ecu.run_for(ms=500)
    t_fault = ecu.now_us()
    _inv(ecu, INV_HARD)
    ecu.run_for(ms=500)
    t_clear = ecu.now_us()
    _inv(ecu, INV_STANDBY)
    ecu.run_for(ms=300)
    margin = 3 * TICK_MS * 1000                       # 0x461 -> RX -> next tick
    frames = ecu.can("can_inv").frames(MODE, since_us=t_fault - 200_000)
    bits = [(f.t_us, f.data[2]) for f in frames]
    healthy = [b for t, b in bits if t < t_fault or t >= t_clear + margin]
    assert healthy and not any(b & FLT_CLEAR for b in healthy), "Flt_Clear while healthy"
    faulted = [b for t, b in bits if t_fault + margin <= t < t_clear]
    rises = [i for i in range(1, len(faulted))
             if faulted[i] & FLT_CLEAR and not faulted[i - 1] & FLT_CLEAR]
    with_bit = [b for b in faulted if b & FLT_CLEAR]
    assert set(with_bit) == {OFF | FLT_CLEAR}, "Flt_Clear on a word other than Off"
    assert len(rises) == len(with_bit) >= 45, "Flt_Clear not one rising edge per cycle"
    after = _cycles(ecu, t_clear + margin)
    assert set(_words(after)) == {(OFF, ("T", 0))}, "burst outlived the fault"


def test_ams_error_suppresses_the_recovery_burst(ecu):
    """E-007: in AmsError a faulted inverter gets Off alone: no reset word,
    no follow word, no Flt_Clear."""
    _inv(ecu, INV_HARD)
    _vdc(ecu)
    _ams(ecu, fsm_state=5)
    t = ecu.run_for(ms=1000)
    assert _state(ecu) == AMS_ERROR
    ecu.run_for(ms=500)
    cycles = _cycles(ecu, t)
    assert len(cycles) >= 48
    assert set(_words(cycles)) == {(OFF, ("T", 0))}


def test_boot_latched_fault_is_cleared_and_the_car_reaches_active(images):
    """E-005: an inverter that boots in hard fault (Inverter plant, cleared by
    [0x0D, Off]) is recovered before R2D; the driver's R2D then climbs it
    Standby -> Ready -> TorqueEnable and the FSM reaches Active."""
    def driver(t):
        return 0.0, 1.0 if 0.5 <= t < 3.0 else 0.0, 0.8 <= t < 1.2

    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        port, inv = Port(sim), Inverter(state=Inverter.HARD)
        t0 = sim.wait_for_app()                 # the driver's clock: the app's start
        port.run([AcuStimulus(), Pedals(driver, start_us=t0), inv], 3400)
        assert [s for _, s in inv.history][:3] == [Inverter.STANDBY, Inverter.READY,
                                                   Inverter.TORQUE], f"inverter went {inv.history}"
        cleared_us = inv.history[0][0]
        assert cleared_us - t0 < 800_000, "fault not cleared before the driver's R2D"
        assert inv.history[1][0] - t0 >= 0.8e6 + R2D_SOUND_MS * 1000, "Ready before R2D"
        assert _state(sim) == ACTIVE
        words = [f.data[2] for f in sim.can("can_inv").frames(MODE) if f.t_us <= cleared_us]
        assert words[-2:] == [HARD_FAULT_RESET, OFF | FLT_CLEAR], f"cleared after {words[-4:]}"


@pytest.mark.parametrize("left_to", [INV_HARD, INV_STANDBY])
def test_inverter_leaving_the_drive_is_redriven(ecu, left_to):
    """E-006 + gap 4: the inverter leaves the drive in Active (hard fault, or
    parked in Standby as after an overspeed): torque is cut that cycle, the
    FSM falls back to WaitInvStandby once (0x708 inv_redrive_count +1), the
    fault gets its burst, then Ready; on Ready drive resumes, throttle still
    down, without the RTDS sounding again."""
    _arm_pit(ecu)
    rtds = ecu.io("ecu").watch("sysbus.gpioPortB", 4)
    _to_active(ecu, throttle=0.4)
    t = ecu.now_us()
    driving = _cycles(ecu, t - 200_000)
    assert driving and all(w[0] == TORQUE_ENABLE and w[1][1] < 0 for w in _words(driving)), \
        f"not driving: {_words(driving)[-3:]}"
    redrives = _inv_faults(ecu, t - 150_000)["redrive"]
    rtds_edges = len(ecu.io("ecu").edges(rtds))

    _inv(ecu, left_to)
    t_left = ecu.now_us()
    ecu.run_until(lambda: _state(ecu) == WAIT_INV_STANDBY, timeout_ms=3 * TICK_MS)
    ecu.run_for(ms=300)
    away = _cycles(ecu, t_left)
    expect = (tuple(BURST[INV_HARD]) if left_to == INV_HARD else (READY,)) + (("T", 0),)
    first_cut = next(i for i, w in enumerate(_words(away)) if w[-1] == ("T", 0))
    assert first_cut <= 2, "torque not cut within two cycles"
    assert set(_words(away)[first_cut:]) == {expect}, f"while away: {set(_words(away))}"
    assert _state(ecu) == WAIT_INV_STANDBY
    assert _inv_faults(ecu, ecu.now_us() - 150_000)["redrive"] == (redrives + 1) & 0xFF

    if left_to == INV_HARD:
        _inv(ecu, INV_STANDBY)                        # the burst cleared it
        ecu.run_for(ms=100)
        assert set(_words(_cycles(ecu, ecu.now_us() - 50_000))) == {(READY, ("T", 0))}
    _inv(ecu, INV_READY)
    ecu.run_until(lambda: _state(ecu) == ACTIVE, timeout_ms=3 * TICK_MS)
    _inv(ecu, INV_TORQUE)
    t_back = ecu.run_for(ms=200)
    back = _words(_cycles(ecu, t_back - 100_000))
    assert back and all(w[0] == TORQUE_ENABLE and w[1][1] < 0 for w in back), f"after: {back}"
    assert _inv_faults(ecu, t_back - 150_000)["redrive"] == (redrives + 1) & 0xFF
    assert len(ecu.io("ecu").edges(rtds)) == rtds_edges, "RTDS sounded on the re-drive"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "isc-fs/IFS08-CE-ECU#246: inv_present (control_task.cpp:92, InvStaleMs=200) is "
    "computed but never read by Controller::step (control.cpp:187-233, 300-308); "
    "a silent inverter in Active keeps its held inv_state and keeps being "
    "commanded TorqueEnable + torque"))
def test_silent_inverter_in_active_cuts_torque(ecu):
    """Gap 1: the inverter stops talking in Active (CAN loss, dead inverter).
    The ECU marks it stale after InvStaleMs (0x708 age saturates) but must
    also stop commanding torque: expected 0 Nm within InvStaleMs + 3 ticks."""
    _arm_pit(ecu)
    _to_active(ecu, throttle=0.4)
    t = ecu.now_us()
    if not all(w[1][1] < 0 for w in _words(_cycles(ecu, t - 200_000))):
        raise RuntimeError("not driving before the silence")
    _silence_inverter(ecu)
    t_silent = ecu.now_us()
    ecu.run_for(ms=INV_STALE_MS + 500)
    if _inv_faults(ecu, ecu.now_us() - 150_000)["age_ms"] != 255:
        raise RuntimeError("the ECU did not see the inverter go silent")
    late = _cycles(ecu, t_silent + (INV_STALE_MS + 3 * TICK_MS) * 1000)
    assert late
    assert all(w[-1] == ("T", 0) and w[0] != TORQUE_ENABLE for w in _words(late)), \
        f"still commanding {_words(late)[-1]} {len(late)} cycles after the inverter went silent"
