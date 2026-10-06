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
