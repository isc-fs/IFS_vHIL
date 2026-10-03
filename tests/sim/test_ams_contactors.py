"""ams-contactors (#32): AIR-, AIR+, precharge and AMS_OK as GPIO edge traces,
watched from reset, through a whole Car arm, Run and Error.

AMS facts (IFS08-CE-AMS main.h, state_machine.hpp, safety_task.cpp):
  PB6 AIR-, PB5 AIR+, PB7 precharge, PB4 AMS_OK.
  Start -> Precharge closes AIR- and PRE (Car); Charger closes AIR- only.
  Precharge -> Transition closes AIR+ and opens PRE in one action set
  (apply_relay_actions); Run holds both AIRs.
  A latched fault opens all three and drops AMS_OK in the same SafetyTask
  tick (latch_error_, safety_task.cpp:83-88). AMS_OK is HIGH past the 2 s
  grace unless Error is latched (ams_ok_asserted).
  0x4A4 byte 0: bit 0 AIR-, 1 AIR+, 2 PRE, 3 AMS_OK read back from the ODR
  (relay_status.def), every 100 ms.
"""
import pytest

from ams_car import (AIR_N, AIR_P, AMS_OK, Car, ERROR, GPIOB, PRECHARGE, PRECHARGE_RELAY,
                     RUN, START)
from vhil.sim import Sim
from vhil.system import REPO

RELAYS = 0x4A4
TICK_US = 10_000


class Trace:
    def __init__(self, sim):
        io = sim.io("ams")
        self.io = io
        self.pins = {n: io.watch(GPIOB, p) for n, p in
                     {"air_n": AIR_N, "air_p": AIR_P, "pre": PRECHARGE_RELAY, "ok": AMS_OK}.items()}

    def edges(self, name, since_us=0):
        return self.io.edges(self.pins[name], since_us=since_us)

    def level(self, name):
        return self.io.level(self.pins[name])

    def levels(self):
        return {n: self.level(n) for n in ("air_n", "air_p", "pre", "ok")}


@pytest.fixture
def rig(firmware):
    with Sim(REPO / "systems" / "ams.yaml", {"ams": firmware("ams")}) as sim:
        trace = Trace(sim)                  # watched before the first instruction
        sim.run_for(ms=3000)
        yield Car(sim), trace


def _relay_bits(car):
    car.sim.run_for(ms=150)
    b = car.can.last(RELAYS).data[0]
    return {"air_n": bool(b & 1), "air_p": bool(b & 2), "pre": bool(b & 4), "ok": bool(b & 8)}


def test_start_has_every_contactor_open_and_ams_ok_high(rig):
    """R-110, F-063: and 0x4A4 reports exactly the pins."""
    car, trace = rig
    assert trace.levels() == {"air_n": False, "air_p": False, "pre": False, "ok": True}
    assert _relay_bits(car) == trace.levels()


def test_car_arm_closes_air_minus_and_precharge_together(rig):
    """F-060, F-062, F-064, R-112: AIR- and PRE rise in the same FSM step,
    AIR+ stays open."""
    car, trace = rig
    t = car.sim.now_us()
    car.arm()
    air_n, pre = trace.edges("air_n", t), trace.edges("pre", t)
    assert [e.level for e in air_n] == [True] and [e.level for e in pre] == [True]
    assert abs(air_n[0].t_us - pre[0].t_us) < TICK_US, (air_n, pre)
    assert trace.edges("air_p", t) == []
    assert _relay_bits(car) == {"air_n": True, "air_p": False, "pre": True, "ok": True}


def test_the_swap_closes_air_plus_and_opens_precharge_at_once(rig):
    """F-061, F-065, R-112, R-114: AIR+ up and PRE down in one action set, so
    they never overlap by a step; Run holds both AIRs."""
    car, trace = rig
    car.arm()
    t = car.sim.now_us()
    car.vcu(352)
    assert car.wait_for(RUN, 200) is not None
    air_p, pre = trace.edges("air_p", t), trace.edges("pre", t)
    assert [e.level for e in air_p] == [True] and [e.level for e in pre] == [False]
    assert abs(air_p[0].t_us - pre[0].t_us) < TICK_US, (air_p, pre)
    assert trace.edges("air_n", t) == [], "AIR- chattered through the swap"
    assert _relay_bits(car) == {"air_n": True, "air_p": True, "pre": False, "ok": True}


@pytest.mark.parametrize("cause", ["undervoltage", "vcu-stale"])
def test_error_opens_everything_and_drops_ams_ok_in_one_tick(rig, cause):
    """F-066, R-115, C-048, R-111: every contactor and AMS_OK fall within one
    SafetyTask tick, and 0x4A4 follows."""
    car, trace = rig
    car.to_run()
    t = car.sim.now_us()
    if cause == "undervoltage":
        car.sim.monitor("sysbus.spi1.isospi.cells2 SetCell 1 2700", board="ams")
    else:
        car.vcu_silent()
    assert car.wait_for(ERROR, 1000) is not None
    falls = {n: trace.edges(n, t) for n in ("air_n", "air_p", "ok")}
    assert all([e.level for e in es] == [False] for es in falls.values()), falls
    times = [es[0].t_us for es in falls.values()]
    assert max(times) - min(times) < TICK_US, falls
    assert not trace.level("pre") and trace.edges("pre", t) == []
    assert _relay_bits(car) == {"air_n": False, "air_p": False, "pre": False, "ok": False}


def test_a_tsms_drop_opens_the_contactors_but_keeps_ams_ok(rig):
    """A de-energise is not a fault: the AIRs open, AMS_OK stays HIGH."""
    car, trace = rig
    car.to_run()
    t = car.sim.now_us()
    car.tsms(False)
    assert car.wait_for(START, 100) is not None
    assert trace.levels() == {"air_n": False, "air_p": False, "pre": False, "ok": True}
    assert trace.edges("ok", t) == []


def test_charger_never_closes_the_precharge_relay(rig):
    """R-113, C-037: a silent VCU and a fresh 0x101 lock Charger; AIR-
    closes, PRE never does."""
    car, trace = rig
    car.charger()
    car.tsms(True)
    car.sim.run_for(ms=1100)
    car.press()
    assert car.wait_for(PRECHARGE, 100) is not None or car.state() > PRECHARGE
    car.sim.run_for(ms=1000)
    assert car.mode() == 2
    assert trace.level("air_n")
    assert [e for e in trace.edges("pre") if e.level] == [], "PRE closed in Charger mode"
