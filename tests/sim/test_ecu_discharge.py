"""ecu-discharge: when the ECU drives its DC-link discharge output, PB6 (D3),
watched at the pin from reset.

ECU facts (IFS08-CE-ECU dev; control_task.cpp:272-303, discharge.cpp:12-72,
ecu_config.hpp:509, :540-552, vehicle_service.cpp:220-249):
  PB6 HIGH = secure = force the bleed on, written every control tick (10 ms);
    it must stay low through the bootloader and init (control_task.cpp:296-301).
  It secures when the AMS's 0x021 (ACU bus) reports fsm_in_start (bit 0) AND
    tsms (bit 1), fresh within DischargeReqStaleMs = 500, AND the inverter's
    0x466 DC link, fresh within InvDcBusStaleMs = 500, reads above
    DischargeReleaseV = 10 V (discharge.cpp:30-40).
  It releases on the ECU's own measurement, 0x466 below 10 V, never on 0x021
    going away (discharge.cpp:42-52). After DischargeTimeoutMs = 30 s without
    the link falling it gives up with a fault (not tested here).
  0x100 byte 2 bit 0, discharge_engaged, mirrors it (control_task.cpp:302-314).
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

INTERLOCK, VDC, HEARTBEAT = 0x021, 0x466, 0x100
FSM_IN_START, TSMS = 0x01, 0x02
TICK_MS = 10


def _vdc(volts: int) -> bytes:
    """0x466 with DCBus_Voltage_V, 10 bits LE from bit 16 (vehicle_service.cpp:62-66)."""
    return bytes([0, 0, volts & 0xFF, volts >> 8, 0, 0])


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        io = sim.io("ecu")
        pin = io.watch(*io.gpio("PB6"))          # watched before the first instruction
        sim.wait_for_app()
        sim.run_for(ms=500)
        yield sim, io, pin


def _engaged(sim) -> bool:
    sim.run_for(ms=3 * TICK_MS)
    return bool(sim.can("can_acu").last(HEARTBEAT).data[2] & 1)


def test_a_stranded_link_is_held_discharged_until_the_ecu_reads_it_drained(ecu):
    """Never through boot; secured within a tick or two of the AMS's report;
    held through a lost 0x021; released only on 0x466 below 10 V."""
    sim, io, pin = ecu
    assert io.edges(pin) == [] and not io.level(pin), "PB6 moved during boot"
    inv, acu = sim.can("can_inv"), sim.can("can_acu")
    inv.send_periodic("vdc", VDC, _vdc(350), 10)
    sim.run_for(ms=100)
    t = sim.now_us()
    acu.send_periodic("interlock", INTERLOCK, bytes([FSM_IN_START | TSMS]), 100)
    sim.run_for(ms=50)
    rise = io.edges(pin, since_us=t)
    assert [e.level for e in rise] == [True], rise
    assert rise[0].t_us - t <= 3 * TICK_MS * 1000, f"secured {rise[0].t_us - t} us late"
    assert _engaged(sim)

    acu.stop_periodic("interlock")                   # the AMS goes quiet mid-bleed
    t = sim.now_us()
    sim.run_for(ms=1000)                             # 0x021 now 2x past stale
    assert io.level(pin) and io.edges(pin, since_us=t) == [], "released on a lost 0x021"

    inv.update_periodic("vdc", _vdc(5))
    sim.run_for(ms=50)
    assert [e.level for e in io.edges(pin, since_us=t)] == [False]
    assert not _engaged(sim)


@pytest.mark.parametrize("interlock, volts", [
    (FSM_IN_START, 350),           # TSMS off: the SDC is open, the bleed already on
    (TSMS, 350),                   # not in Start: a live tractive system
    (FSM_IN_START | TSMS, 5),      # a link already drained
    (FSM_IN_START | TSMS, None),   # no 0x466: cannot see the link
], ids=["no-tsms", "not-in-start", "drained", "no-vdc"])
def test_it_never_secures_without_all_three_terms(ecu, interlock, volts):
    sim, io, pin = ecu
    if volts is not None:
        sim.can("can_inv").send_periodic("vdc", VDC, _vdc(volts), 10)
    sim.can("can_acu").send_periodic("interlock", INTERLOCK, bytes([interlock]), 100)
    sim.run_for(ms=1000)
    assert io.edges(pin) == [] and not io.level(pin)
    assert not _engaged(sim)


TIMEOUT_MS = 30_000            # DischargeTimeoutMs, ecu_config.hpp:547


@pytest.mark.parametrize("link", ["stuck", "sense-floor"])
def test_a_discharge_that_never_completes_gives_up_with_a_fault(ecu, link):
    """discharge.cpp:42-60: secured, the link never reads below 10 V: the
    ECU stops securing DischargeTimeoutMs (30 s) after it started and sets
    its fault (g_discharge_fault, control_task.cpp:303); with the AMS's
    request still standing it does not re-secure (that would oscillate,
    discharge.cpp:14-24); the AMS withdrawing it (TSMS off) clears the
    fault, and a new stranding secures again.
      stuck: the link holds 350 V (bleed open, relay not obeying).
      sense-floor: the inverter's reading stops at 48 V, its DC-link floor
        (ecu_config.hpp:523-529): 0x466 goes silent, dc_bus_valid false, and
        'cannot confirm' holds to the timeout."""
    sim, io, pin = ecu
    inv, acu = sim.can("can_inv"), sim.can("can_acu")
    inv.send_periodic("vdc", VDC, _vdc(350), 10)
    sim.run_for(ms=100)
    acu.send_periodic("interlock", INTERLOCK, bytes([FSM_IN_START | TSMS]), 100)
    sim.run_for(ms=50)
    secured = [e for e in io.edges(pin) if e.level]
    assert secured, "never secured"
    t_secure = secured[0].t_us
    if link == "sense-floor":
        inv.update_periodic("vdc", _vdc(48))
        sim.run_for(ms=500)
        inv.stop_periodic("vdc")
    sim.run_for(us=t_secure + (TIMEOUT_MS - 200) * 1000 - sim.now_us())
    assert io.level(pin) and sim.read_symbol("ecu", "g_discharge_fault") == 0, "gave up early"
    sim.run_for(ms=400)
    falls = [e for e in io.edges(pin, since_us=t_secure) if not e.level]
    assert falls, "still securing past DischargeTimeoutMs"
    assert abs((falls[0].t_us - t_secure) / 1000 - TIMEOUT_MS) <= 2 * TICK_MS
    assert sim.read_symbol("ecu", "g_discharge_fault") == 1
    sim.run_for(ms=2000)
    assert not io.level(pin) and io.edges(pin, since_us=falls[0].t_us + 1) == [], \
        "re-secured on the standing request"
    acu.update_periodic("interlock", bytes([FSM_IN_START]))          # TSMS off: request withdrawn
    sim.run_for(ms=300)
    assert sim.read_symbol("ecu", "g_discharge_fault") == 0, "the fault outlived the request"
    if link == "sense-floor":
        inv.send_periodic("vdc", VDC, _vdc(350), 10)
    acu.update_periodic("interlock", bytes([FSM_IN_START | TSMS]))
    sim.run_for(ms=300)
    assert io.level(pin), "a new stranding did not secure after the fault cleared"


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-ECU#259: 0x021 is 100 ms cyclic and trusted for 500 ms, so "
    "after the press the ECU still holds 'fsm_in_start' while the link rises "
    "through PRE; it secures within a tick and holds to the 30 s timeout"))
def test_an_arm_never_secures_the_discharge_into_the_precharge(ecu):
    """The AMS arms from Start: AIR- and PRE close and the link rises through
    the precharge resistor at once, but 0x021 is 100 ms cyclic
    (acu_discharge_interlock.def:30), so for up to 100 ms the ECU still
    holds a fresh 'fsm_in_start + tsms' from before the press. fsm_in_start
    exists to exclude exactly this (acu_discharge_interlock.def:33-35:
    'Excludes Precharge/Transition, where the link is deliberately rising
    through the resistor and a forced discharge would fight it'), and once
    secured the ECU releases only below 10 V or after 30 s
    (discharge.cpp:42-60), so a spurious secure puts the transient-duty
    bleed across a precharging, then live, link. Here the press lands 5 ms
    after a 0x021 and the link reads 352 V on the next 0x466; the AMS's next
    0x021 (95 ms later) says not-in-Start. The ECU must not secure."""
    sim, io, pin = ecu
    inv, acu = sim.can("can_inv"), sim.can("can_acu")
    inv.send_periodic("vdc", VDC, _vdc(0), 10)               # drained link, AMS in Start
    acu.send_periodic("interlock", INTERLOCK, bytes([FSM_IN_START | TSMS]), 100)
    sim.run_for(ms=1000)
    assert not io.level(pin)
    last = acu.sent([INTERLOCK])[-1].t_us
    sim.run_for(us=last + 5000 - sim.now_us())               # the press, 5 ms after a 0x021
    t = sim.now_us()
    acu.update_periodic("interlock", bytes([TSMS]))          # Precharge from the next 0x021 on
    inv.update_periodic("vdc", _vdc(352))                    # the link rising through PRE
    sim.run_for(ms=1000)
    assert io.edges(pin, since_us=t) == [], \
        f"secured the discharge into the precharge: {io.edges(pin, since_us=t)}"
