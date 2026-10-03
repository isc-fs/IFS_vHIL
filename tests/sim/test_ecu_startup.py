"""ecu-startup (#41): the ECU's start-up gates, tick by tick in virtual time.

ECU facts (IFS08-CE-ECU control.cpp:150-186, ecu_config.hpp):
  WaitInvVdcConfig (0) -> Precharge (1) on the inverter's DC-bus report
    (0x466; inv_vconfig_ready, set on any receipt).
  Precharge -> WaitStartBrake (2) only on ok_precharge (fresh 0x020[0]).
  WaitStartBrake -> R2dDelay (3) on START (PB5, debounced over
    StartDebounceSamples = 5 control ticks) with brake_raw > BrakeArmRaw (750).
  R2dDelay -> WaitInvStandby (4) after R2dSoundMs = 2000; the RTDS output
    (PB4) sounds while in R2dDelay.
  g_last_ctrl_state mirrors the state every control tick (ControlPeriodMs 10).
The full climb to Active with an inverter plant is in test_cosim.py; the AMS
and inverter frames here are stimulus.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

WAIT_VDC, PRECHARGE, WAIT_START_BRAKE, R2D_DELAY, WAIT_INV_STANDBY = 0, 1, 2, 3, 4
TICK_MS, R2D_SOUND_MS, DEBOUNCE_MS = 10, 2000, 50
BRAKE_FIRM_V = 1500 * 3.3 / 4095          # well past BrakeArmRaw (750 counts)


@pytest.fixture
def ecu(firmware):
    with Sim(REPO / "systems" / "ecu.yaml", {"ecu": firmware("ecu")}) as sim:
        yield sim


def _state(ecu):
    return ecu.read_symbol("ecu", "g_last_ctrl_state")


def _vdc(ecu, on=True):
    if on:
        ecu.can("can_inv").send_periodic("vdc", 0x466, bytes([0, 0, 0x5E, 0x01, 0, 0]), 10)
    else:
        ecu.can("can_inv").stop_periodic("vdc")


def _ams(ecu, ok):
    ecu.can("can_acu").send_periodic("ams", 0x020, bytes([1 if ok else 0]), 10)


def _to_wait_start_brake(ecu):
    _vdc(ecu)
    _ams(ecu, ok=True)
    ecu.run_for(ms=1000)
    assert _state(ecu) == WAIT_START_BRAKE


def test_the_vdc_gate_holds_then_latches(ecu):
    """C-001: no 0x466, no Precharge; one report opens the gate for good."""
    ecu.run_for(ms=1500)
    assert _state(ecu) == WAIT_VDC
    _vdc(ecu)
    ecu.run_for(ms=3 * TICK_MS)
    assert _state(ecu) == PRECHARGE
    _vdc(ecu, on=False)
    ecu.run_for(ms=1000)
    assert _state(ecu) == PRECHARGE, "the vdc gate did not stay latched"


@pytest.mark.parametrize("ok, expected", [(False, PRECHARGE), (True, WAIT_START_BRAKE)])
def test_precharge_waits_for_the_ams_verdict(ecu, ok, expected):
    """C-002: Precharge advances only on ok_precharge."""
    _vdc(ecu)
    _ams(ecu, ok)
    ecu.run_for(ms=1000)
    assert _state(ecu) == expected


@pytest.mark.parametrize("press_ms, r2d", [(DEBOUNCE_MS - 2 * TICK_MS, False),
                                           (DEBOUNCE_MS + 2 * TICK_MS, True)])
def test_start_is_debounced(ecu, press_ms, r2d):
    """START must hold for the 5-sample debounce (50 ms) to arm R2D."""
    _to_wait_start_brake(ecu)
    io = ecu.io("ecu")
    io.set_voltage("PF7", BRAKE_FIRM_V)
    ecu.run_for(ms=100)
    io.set_input("sysbus.gpioPortB", 5, True)
    ecu.run_for(ms=press_ms)
    io.set_input("sysbus.gpioPortB", 5, False)
    ecu.run_for(ms=100)
    assert (_state(ecu) == R2D_DELAY) == r2d


def test_start_without_the_brake_does_nothing(ecu):
    _to_wait_start_brake(ecu)
    io = ecu.io("ecu")
    io.set_input("sysbus.gpioPortB", 5, True)
    ecu.run_for(ms=500)
    assert _state(ecu) == WAIT_START_BRAKE


def test_r2d_dwells_2_s_with_the_buzzer_on(ecu):
    """C-004's dwell: R2dDelay lasts 2000 ms (+- a tick), the RTDS on all of it."""
    _to_wait_start_brake(ecu)
    io = ecu.io("ecu")
    pin = io.watch("sysbus.gpioPortB", 4)
    io.set_voltage("PF7", BRAKE_FIRM_V)
    io.set_input("sysbus.gpioPortB", 5, True)
    entered = left = None
    for _ in range(300):                                 # 3 s, tick by tick
        ecu.run_for(ms=TICK_MS)
        s = _state(ecu)
        if entered is None and s == R2D_DELAY:
            entered = ecu.now_us()
        if entered is not None and s == WAIT_INV_STANDBY:
            left = ecu.now_us()
            break
    assert entered and left, "never passed through R2dDelay"
    assert abs((left - entered) / 1000 - R2D_SOUND_MS) <= TICK_MS
    edges = io.edges(pin)
    assert len(edges) >= 2 and edges[0].level and not edges[1].level
    assert abs((edges[1].t_us - edges[0].t_us) / 1000 - R2D_SOUND_MS) <= TICK_MS
