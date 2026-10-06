"""ams-fsm-charger (#31): the AMS state machine in Charger mode, in virtual
time, the charger's 0x101 and the cockpit inputs scripted (ams_car.Car).

AMS facts (IFS08-CE-AMS state_machine.hpp, safety_task.cpp, vehicle_service.cpp):
  Charger locks at the arming press only with a fresh (<= ChargeReqFreshMs
    = 1000) 0x101 "CHRG" request AND a VCU not heard for VcuFreshMs (1000);
    a stray request with the VCU live locks Car. Any other 0x101 payload
    is ignored (vehicle_service.cpp:52-58).
  Charger skips the precharge resistor: Start -> Precharge closes AIR-
    only; Precharge proceeds while 0x101 is still fresh, Transition commits
    to Charge (4) on the next step.
  ChargerStale (14): Charger-locked and 0x101 older than ChargerStaleMs
    (1000). ChargerTsmsOpen (15): a TSMS drop in a Charger-mode energised
    state LATCHES Error (the car's TSMS drop doesn't).
  Charge ignores DASH_CHG.
  DASH_CHG is PF10, a GPIO input with a pull-down (AMS.ioc), read every 10 ms;
    its rising edge is the press, its live level goes out on 0x4A2[5] bit 0
    (safety_task.cpp:189-195, :386-395).
"""
import pytest

from ams_car import (AIR_N, AIR_P, CHARGE, Car, ERROR, GPIOB, PRECHARGE, PRECHARGE_RELAY,
                     START)
from vhil.cosim import Port
from vhil.sim import Sim
from vhil.system import REPO

VCU_STALE, CHARGER_STALE, CHARGER_TSMS_OPEN = 11, 14, 15
TEMPS = 0x4A2
BOOT_MS = 3000


@pytest.fixture
def car(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        car = Car(sim)
        car.pins = {n: car.io.watch(GPIOB, p) for n, p in
                    {"air_n": AIR_N, "air_p": AIR_P, "pre": PRECHARGE_RELAY}.items()}
        sim.wait_for_app()
        sim.run_for(ms=BOOT_MS)
        yield car


def _plug_in_and_press(car, payload=b"CHRG"):
    car.charger(payload)
    car.tsms(True)
    car.sim.run_for(ms=1100)
    car.press()


def test_a_charger_with_no_vcu_locks_charger_and_charges(car):
    """C-037, C-037b, R-113: Charger lock, AIR- then AIR+, PRE never closed,
    Charge reached."""
    _plug_in_and_press(car)
    assert car.wait_for(CHARGE, 200) is not None, f"state {car.state()}"
    assert car.mode() == 2
    assert car.io.level(car.pins["air_n"]) and car.io.level(car.pins["air_p"])
    assert [e for e in car.io.edges(car.pins["pre"]) if e.level] == [], "PRE closed"


def test_the_dashboard_button_on_pf10_requests_the_charge(car):
    """PF10 driven through the system's port (dash_chg = ams.PF10, the AMS
    role's GPIO): held, it reads back on 0x4A2[5] bit 0; its rising edge,
    with the charger's 0x101 fresh and no VCU, locks Charger and closes AIR-
    (safety_task.cpp:285-303, state_machine.hpp:274-284); released, the
    readback clears and the charge goes on."""
    port = Port(car.sim)
    car.charger()
    port.set_level("tsms", True)
    car.sim.run_for(ms=1100)
    t = car.sim.now_us()
    port.set_level("dash_chg", True)
    car.sim.run_for(ms=600)
    assert car.can.last(TEMPS, since_us=t).data[5] & 0x01, "PF10 HIGH not read back"
    assert car.mode() == 2 and car.io.level(car.pins["air_n"])
    t = car.sim.now_us()
    port.set_level("dash_chg", False)
    car.sim.run_for(ms=600)
    assert not car.can.last(TEMPS, since_us=t).data[5] & 0x01, "PF10 LOW not read back"
    assert car.state() == CHARGE


def test_a_stray_request_with_the_vcu_live_locks_car(car):
    """A live VCU wins: the request cannot flip a running car to Charger."""
    car.vcu(0)
    _plug_in_and_press(car)
    assert car.wait_for(PRECHARGE, 100) is not None
    assert car.mode() == 1


@pytest.mark.parametrize("payload", [b"CHRX", b"chrg", b"CHR"], ids=["wrong", "case", "short"])
def test_a_request_without_the_magic_is_ignored(car, payload):
    """B-030, C-038: no valid request and no VCU -> Car lock -> VcuStale."""
    _plug_in_and_press(car, payload)
    assert car.wait_for(ERROR, 500) is not None, f"state {car.state()}"
    assert (car.reason(), car.mode()) == (VCU_STALE, 1)


def test_an_unplugged_charger_faults_charger_stale(car):
    """C-037c, ChargerStale: the request stops in Charge -> Error 14, one
    second after the last frame."""
    _plug_in_and_press(car)
    assert car.wait_for(CHARGE, 200) is not None
    car.charger_unplugged()
    elapsed = car.wait_for(ERROR, 1600, step_ms=10)
    assert elapsed is not None and elapsed <= 1550, f"Error after {elapsed} ms"
    assert car.reason() == CHARGER_STALE
    assert not car.io.level(car.pins["air_p"]) and not car.io.level(car.pins["air_n"])


def test_a_tsms_drop_while_charging_latches(car):
    """C-039c, ChargerTsmsOpen: unlike the car, a TSMS drop in Charge latches
    Error 15 (IFS_HIL expects a return to Start: drift)."""
    _plug_in_and_press(car)
    assert car.wait_for(CHARGE, 200) is not None
    car.tsms(False)
    assert car.wait_for(ERROR, 100) is not None, f"state {car.state()}"
    assert car.reason() == CHARGER_TSMS_OPEN
    car.tsms(True)
    car.press()
    car.sim.run_for(ms=1000)
    assert car.state() == ERROR


def test_charge_ignores_the_button(car):
    """C-039c: DASH_CHG released and pressed again: still charging."""
    _plug_in_and_press(car)
    assert car.wait_for(CHARGE, 200) is not None
    for _ in range(3):
        car.press()
        car.sim.run_for(ms=100)
    assert car.state() == CHARGE


def test_a_charger_lock_without_a_press_is_no_lock(car):
    """The request alone arms nothing: Start, Undecided."""
    car.charger()
    car.tsms(True)
    car.sim.run_for(ms=3000)
    assert (car.state(), car.mode()) == (START, 0)
