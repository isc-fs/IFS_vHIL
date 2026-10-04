"""sys-ecu-ams (#50): the real ECU and the real AMS on the ACU bus, the
contract between them both ways, in virtual time.

ECU facts (IFS08-CE-ECU):
  control_task.cpp:305-320: 0x100 every control tick in every state, its
    DC-link volts relayed from the inverter's 0x466 and dc_bus_valid while
    0x466 is fresh (InvDcBusStaleMs).
  control_task.cpp:117-120: ok_precharge = AMS 0x020 && AMS fresh (a frame
    within AmsStaleMs = 200); ams_error = 0x4A0 state 5 && fresh. 0x504
    VCU_ts_active (100 ms) = ok_precharge && fresh; AmsError = CtrlState 6.
AMS facts (IFS08-CE-AMS):
  0x020 ok_precharge = FSM in Run or Charge (acu_ok_precharge.def).
  Car mode locks at a TSMS + DASH_CHG arm with a fresh 0x100; precharge
    completes when the relayed link reaches 95 % of the pack; VcuStale (11)
    once Car-locked and 0x100 is older than 200 ms (state_machine.hpp,
    safety_predicates.hpp).
The plant: the inverter's DC link (0x466, every 10 ms) sits at the pack
voltage while the AMS holds AIR- and either the precharge relay or AIR+
closed, and at 0 V otherwise (an instant precharge and bleed; enough for a
contract test, not a thermal one). A board losing power is modelled by
halting its CPU: its controller stops transmitting, as a dead node does.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

GPIOB, GPIOF = "sysbus.gpioPortB", "sysbus.gpioPortF"
AIR_P, AIR_N, PRE = 5, 6, 7
TSMS, DASH_CHG = 9, 10
INV_VDC, TS_ACTIVE = 0x466, 0x504
PACK_V = 352
START, PRECHARGE, RUN, ERROR = 0, 1, 3, 5
VCU_STALE, UNDERVOLTAGE = 11, 4
ECU_AMS_ERROR = 6


class Rig:
    def __init__(self, sim):
        self.sim = sim
        self.ams_io = sim.io("ams")
        self.pins = {n: self.ams_io.watch(GPIOB, p) for n, p in
                     {"air_p": AIR_P, "air_n": AIR_N, "pre": PRE}.items()}
        self.inv = sim.can("can_inv")
        self.acu = sim.can("can_acu")
        self.link_v = None
        self._link(0)

    def _link(self, volts):
        if volts == self.link_v:
            return
        v = volts & 0x3FF
        data = bytes([0, 0, v & 0xFF, v >> 8, 0, 0])
        if self.link_v is None:
            self.inv.send_periodic("vdc", INV_VDC, data, period_ms=10)
        else:
            self.inv.update_periodic("vdc", data)
        self.link_v = volts

    def run(self, ms, step_ms=10):
        """Advance, keeping the DC link on the AMS's contactors."""
        for _ in range(int(ms // step_ms)):
            self.sim.run_for(ms=step_ms)
            closed = {n: self.ams_io.level(p) for n, p in self.pins.items()}
            energised = closed["air_n"] and (closed["pre"] or closed["air_p"])
            self._link(PACK_V if energised else 0)

    def ams(self, symbol="g_state_telemetry"):
        return self.sim.read_symbol("ams", symbol)

    def ecu_state(self):
        return self.sim.read_symbol("ecu", "g_last_ctrl_state")

    def ts_active(self):
        return self.acu.last(TS_ACTIVE).data[0]

    def arm(self):
        self.ams_io.set_input(GPIOF, TSMS, True)
        self.run(100)
        self.ams_io.set_input(GPIOF, DASH_CHG, True)
        self.run(50)
        self.ams_io.set_input(GPIOF, DASH_CHG, False)

    def wait_ams(self, state, limit_ms):
        for elapsed in range(10, limit_ms + 10, 10):
            self.run(10)
            if self.ams() == state:
                return elapsed
        return None

    def power_off(self, board):
        self.sim.monitor("cpu IsHalted true", board=board)


@pytest.fixture
def rig(firmware):
    with Sim(REPO / "systems" / "ecu-ams.yaml",
             {"ecu": firmware("ecu"), "ams": firmware("ams")}) as sim:
        rig = Rig(sim)
        rig.run(3000)
        yield rig


def test_the_car_arms_through_the_ecu_heartbeat(rig):
    """F-005, F-001: with the ECU's real 0x100 relaying the link, the AMS
    locks Car, precharges to Run and stays there; the ECU sees ok_precharge
    and reports the tractive system active."""
    assert (rig.ams(), rig.ts_active()) == (START, 0)
    rig.arm()
    assert rig.wait_ams(RUN, 1000) is not None, f"AMS state {rig.ams()}"
    assert rig.ams("g_mode_locked_telemetry") == 1
    rig.run(5000)
    assert (rig.ams(), rig.ams("g_fault_reason_telemetry")) == (RUN, 0), "the AMS left Run"
    assert rig.ts_active() == 1, "the ECU does not see the AMS ready"


def test_an_ams_fault_puts_the_ecu_in_ams_error(rig):
    """F-001: a latched AMS fault reaches the ECU's FSM within one AMS
    status frame plus a tick, and the ECU drops ts_active."""
    rig.arm()
    assert rig.wait_ams(RUN, 1000) is not None
    rig.sim.monitor("sysbus.spi1.isospi.cells0 SetCell 0 2700", board="ams")
    assert rig.wait_ams(ERROR, 1000) is not None
    assert rig.ams("g_fault_reason_telemetry") == UNDERVOLTAGE
    for _ in range(60):
        rig.run(10)
        if rig.ecu_state() == ECU_AMS_ERROR:
            break
    assert rig.ecu_state() == ECU_AMS_ERROR, f"ECU state {rig.ecu_state()}"
    rig.run(200)
    assert rig.ts_active() == 0


def test_a_dead_ecu_faults_the_armed_ams_vcu_stale(rig):
    """The heartbeat contract from the AMS side: the ECU loses power with
    the car in Run -> VcuStale at 200 ms."""
    rig.arm()
    assert rig.wait_ams(RUN, 1000) is not None
    rig.power_off("ecu")
    elapsed = rig.wait_ams(ERROR, 500)
    assert elapsed is not None and 200 <= elapsed <= 240, f"Error after {elapsed} ms"
    assert rig.ams("g_fault_reason_telemetry") == VCU_STALE


def test_a_dead_ams_goes_stale_at_the_ecu_not_into_error(rig):
    """F-004: the AMS loses power in Run: the ECU's view of it goes stale,
    ts_active drops within AmsStaleMs plus a 0x504 period, and the ECU does
    not report AmsError (that needs a fresh 0x4A0 saying Error)."""
    rig.arm()
    assert rig.wait_ams(RUN, 1000) is not None
    rig.run(300)
    assert rig.ts_active() == 1
    rig.power_off("ams")
    t0 = rig.sim.now_us()
    rig.run(400)
    frames = rig.acu.frames(TS_ACTIVE, since_us=t0)
    first_zero = next((f for f in frames if f.data[0] == 0), None)
    assert first_zero is not None, "ts_active never dropped"
    assert first_zero.t_us - t0 <= 310_000, f"ts_active dropped after {(first_zero.t_us - t0) / 1000} ms"
    assert rig.ecu_state() != ECU_AMS_ERROR
