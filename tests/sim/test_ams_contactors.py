"""ams-contactors (#32): AIR-, AIR+, precharge and AMS_OK as GPIO edge traces,
watched from reset at the pins systems/ams.yaml wires (its port: air_n
ams.PB6, air_p ams.PB5, precharge ams.PB7, ams_ok ams.PB4), through a whole
Car arm, Run and Error.

AMS facts (IFS08-CE-AMS main, main.h:76-83, state_machine.hpp,
safety_task.cpp, relay_driver.cpp):
  PB6 AIR-, PB5 AIR+, PB7 precharge, PB4 AMS_OK; HIGH = closed / OK
  (relay_driver.cpp:46-64).
  Start -> Precharge closes AIR- and PRE (Car); Charger closes AIR- only
  (state_machine.hpp:274-284).
  Precharge -> Transition closes AIR+ and opens PRE in one action set
  (state_machine.hpp:325-329, apply_relay_actions safety_task.cpp:109-116);
  Run holds both AIRs; a TSMS drop opens all three without latching
  (state_machine.hpp:221-240).
  Precharge held past PrechargeMaxMs = 5000 (the resistor's thermal limit,
  ams_config.hpp:185) latches Error and opens all three
  (state_machine.hpp:288-305).
  A latched fault opens all three and drops AMS_OK in the same SafetyTask
  tick (latch_error_, safety_task.cpp:83-88). AMS_OK is HIGH past the 2 s
  grace unless Error is latched (ams_ok_asserted).
  0x4A4 byte 0: bit 0 AIR-, 1 AIR+, 2 PRE, 3 AMS_OK read back from the ODR
  (relay_status.def), every 100 ms.
  0x6C0 byte 3: AMS_OK read back from the pin (pit_fsm_status.def,
  acu_can_task.cpp:311-312), 1 Hz once 0x7F0 DE AD BE EF arms it.
"""
import pytest

from ams_car import Car, ERROR, PACK_V, PRECHARGE, RUN, START
from vhil.sim import Sim
from vhil.system import REPO

RELAYS = 0x4A4
PIT_ARM, PIT_FSM = 0x7F0, 0x6C0
TICK_US = 10_000


# Trace name -> the system's port signal (systems/ams.yaml).
SIGNALS = {"air_n": "air_n", "air_p": "air_p", "pre": "precharge", "ok": "ams_ok"}
PRECHARGE_MAX_MS = 5000


class Trace:
    """The AMS's relay outputs at the pins its system wires to them."""

    def __init__(self, sim):
        io = sim.io("ams")
        self.io = io
        signals = sim.system.doc["port"]["signals"]
        self.pins = {}
        for name, signal in SIGNALS.items():
            board, pin = signals[signal]["gpio_out"].split(".", 1)
            assert board == "ams", signals[signal]
            self.pins[name] = io.watch(*io.gpio(pin))

    def edges(self, name, since_us=0):
        return self.io.edges(self.pins[name], since_us=since_us)

    def level(self, name):
        return self.io.level(self.pins[name])

    def levels(self):
        return {n: self.level(n) for n in ("air_n", "air_p", "pre", "ok")}


@pytest.fixture
def rig(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        trace = Trace(sim)                  # watched before the first instruction
        sim.wait_for_app()
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


def test_a_car_precharge_drives_air_minus_and_precharge_through_the_sequence(rig):
    """At PB6 and PB7 over one whole cycle: both close on the arm and hold
    while the link is short of 95 %; PRE alone opens at the swap; AIR- opens
    on the TSMS drop. Exactly one close and one open each, in that order."""
    car, trace = rig
    t = car.sim.now_us()
    car.arm()
    car.vcu(round(PACK_V * 0.5))                     # charging, short of 95 %
    car.sim.run_for(ms=500)
    assert trace.level("air_n") and trace.level("pre") and not trace.level("air_p")
    car.vcu(round(PACK_V))
    assert car.wait_for(RUN, 200) is not None
    car.sim.run_for(ms=100)
    car.tsms(False)
    assert car.wait_for(START, 100) is not None
    air_n, pre = trace.edges("air_n", t), trace.edges("pre", t)
    assert [e.level for e in air_n] == [True, False], air_n
    assert [e.level for e in pre] == [True, False], pre
    assert abs(air_n[0].t_us - pre[0].t_us) < TICK_US, (air_n, pre)
    assert pre[1].t_us - pre[0].t_us >= 500_000, "PRE opened before the link was up"
    assert pre[1].t_us < air_n[1].t_us, "AIR- opened before PRE"
    assert trace.levels() == {"air_n": False, "air_p": False, "pre": False, "ok": True}


def test_a_stuck_precharge_opens_the_resistor_path_at_the_timeout(rig):
    """A link that never rises: PRE and AIR- open together PrechargeMaxMs
    after they closed (the resistor's thermal limit), into Error."""
    car, trace = rig
    t = car.sim.now_us()
    car.arm()
    assert car.wait_for(ERROR, PRECHARGE_MAX_MS + 200, step_ms=10) is not None
    air_n, pre = trace.edges("air_n", t), trace.edges("pre", t)
    assert [e.level for e in air_n] == [True, False], air_n
    assert [e.level for e in pre] == [True, False], pre
    held_ms = (pre[1].t_us - pre[0].t_us) / 1000
    assert PRECHARGE_MAX_MS - 10 <= held_ms <= PRECHARGE_MAX_MS + 60, f"PRE held {held_ms} ms"
    assert abs(air_n[1].t_us - pre[1].t_us) < TICK_US, (air_n, pre)
    assert trace.edges("air_p", t) == []


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
    """Replaces IFS_HIL F-063, F-066 and R-115 ("undervoltage", a cell set low
    on the chain model): every contactor and AMS_OK fall within one
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


def test_ams_ok_stays_low_while_error_is_latched(rig):
    """Replaces IFS_HIL C-048: AMS_OK falls with the Error latch and never
    rises again for the boot, though the cell that tripped it is healthy
    again and the cockpit is re-armed (ams_ok_asserted, safety_task.cpp:371)."""
    car, trace = rig
    car.to_run()
    t = car.sim.now_us()
    car.sim.monitor("sysbus.spi1.isospi.cells1 SetCell 0 2500", board="ams")
    assert car.wait_for(ERROR, 1000) is not None
    car.sim.monitor("sysbus.spi1.isospi.cells1 SetCell 0 3700", board="ams")
    car.tsms(False)
    car.sim.run_for(ms=500)
    car.tsms(True)
    car.press()
    car.sim.run_for(ms=3000)
    assert [e.level for e in trace.edges("ok", t)] == [False], trace.edges("ok", t)
    assert car.state() == ERROR and not trace.level("ok")


def test_the_ams_ok_bit_tracks_the_pit_diag_readback(rig):
    """Replaces IFS_HIL R-111: 0x4A4 bit 3, 0x6C0 byte 3 and the PB4 pin agree,
    HIGH while healthy and LOW once a cell under-voltage latches Error."""
    car, trace = rig
    car.can.send(PIT_ARM, bytes.fromhex("DEADBEEF"))

    def readbacks():
        car.sim.run_for(ms=1100)                      # a fresh 0x6C0 (1 Hz)
        return _relay_bits(car)["ok"], bool(car.can.last(PIT_FSM).data[3]), trace.level("ok")

    assert readbacks() == (True, True, True)
    car.sim.monitor("sysbus.spi1.isospi.cells1 SetCell 0 2500", board="ams")
    assert car.wait_for(ERROR, 1000) is not None
    assert readbacks() == (False, False, False)


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
