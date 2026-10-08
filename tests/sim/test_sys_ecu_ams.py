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

The AMS image (ams@main) still carries IFS08-CE-AMS#599 (fixed on AMS dev by
#601): CurrentSensorTask's 1 KB stack leaves 32 B below update_soc()'s
deepest call, and an interrupt landing there (up to 104 B with FPU state)
overflows it. vApplicationStackOverflowHook (freertos.c:77-98) names the task
in g_stack_overflow_task_name, opens the relays, latches Error and spins until
the IWDG resets the AMS into its bootloader. Whether an interrupt lands in
that window is a race the interrupt timing decides, and between boards that
timing varies from run to run. Rig.run watches the hook's global every step
and decides the run there: #599 -> xfail; any other task -> fail. A test's own
assertions so never read the stale RAM of an AMS that reset into its
bootloader (g_state_telemetry still saying Run). One test forces an interrupt
into that window instead, so #599 fails it every time: a strict xfail until
the catalogue's AMS carries #601.
"""
import pytest

from vhil import elf
from vhil.sim import Sim
from vhil.snapshot import parse_bytes
from vhil.system import REPO

GPIOB, GPIOF = "sysbus.gpioPortB", "sysbus.gpioPortF"
AIR_P, AIR_N, PRE = 5, 6, 7
TSMS, DASH_CHG = 9, 10
INV_VDC, TS_ACTIVE = 0x466, 0x504
PACK_V = 352
START, PRECHARGE, RUN, ERROR = 0, 1, 3, 5
VCU_STALE, UNDERVOLTAGE = 11, 4
ECU_AMS_ERROR = 6
OVERFLOW_TASK = "g_stack_overflow_task_name"   # char[16], freertos.c:64
AMS_599 = "CurrentSensorTa"                    # the hook copies 15 chars (freertos.c:88)
# IFS08-CE-AMS#599's deepest frame: ams::soc::r_int_element_ohm, reached from
# CurrentSensorTask through update_soc -> KalmanSoc::correct.
R_INT = "_ZN3ams3soc17r_int_element_ohmEds"
R_INT_PROLOGUE = (0xB480, 0xB099)              # push {r7}; sub sp, #100
ICSR, PENDSTSET = 0xE000ED04, 1 << 26          # SCB->ICSR (core_cm7.h)


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
            self._check_overflow()
            closed = {n: self.ams_io.level(p) for n, p in self.pins.items()}
            energised = closed["air_n"] and (closed["pre"] or closed["air_p"])
            self._link(PACK_V if energised else 0)

    def _check_overflow(self):
        """The AMS's stack-overflow hook fired: decide the run (module doc).
        The hook spins ~100 ms before the IWDG resets (reload 100 at LSI/32,
        AMS main.c:564-566), so a 10 ms step reads its global before the
        next boot's startup clears it."""
        if not self.sim.read_symbol("ams", OVERFLOW_TASK):
            return
        address, _ = elf.symbol(self.sim.firmware["ams"], OVERFLOW_TASK)
        raw = self.sim.monitor(f"sysbus ReadBytes {address:#x} 16", board="ams")
        task = parse_bytes(raw).split(b"\0")[0].decode(errors="replace")
        at = f"{self.sim.now_us() / 1e6:.3f} s"
        if task == AMS_599:
            pytest.xfail(f"isc-fs/IFS08-CE-AMS#599 at {at}: CurrentSensorTask overflowed its "
                         "stack; the hook latches Error and the IWDG resets the AMS")
        pytest.fail(f"the AMS's stack-overflow hook fired at {at} for task {task!r}")

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
def rig(images):
    with Sim(REPO / "systems" / "ecu-ams.yaml",
             images("ecu-ams")) as sim:
        rig = Rig(sim)
        sim.wait_for_app()                      # both boards, past their bootloaders
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
    assert rig.sim.in_app("ams"), "the AMS reset into its bootloader"
    assert (rig.ams(), rig.ams("g_fault_reason_telemetry")) == (RUN, 0), "the AMS left Run"
    assert rig.ts_active() == 1, "the ECU does not see the AMS ready"


@pytest.mark.xfail(strict=True, reason="isc-fs/IFS08-CE-AMS#599 (fixed on AMS dev by #601, "
                   "not on main): an interrupt at update_soc's deepest frame overflows "
                   "CurrentSensorTask's 1 KB stack")
def test_an_interrupt_at_the_deepest_soc_frame_leaves_the_ams_in_run(rig):
    """The race the arming test can lose, decided: in Run, one interrupt (a
    pended SysTick) lands as r_int_element_ohm has reserved its frame, the
    deepest point of CurrentSensorTask. On the car any interrupt can land
    there (TIM23's 1 kHz HAL tick, SysTick, FDCAN RX), and its exception
    frame (up to 104 B with FPU state) must fit the task's stack: the AMS
    stays in Run and the ECU keeps ts_active."""
    rig.arm()
    assert rig.wait_ams(RUN, 1000) is not None
    fn = elf.symbol(rig.sim.firmware["ams"], R_INT)[0] & ~1
    prologue = tuple(int(rig.sim.monitor(f"sysbus ReadWord {fn + 2 * i:#x}", board="ams").strip(), 16)
                     for i in range(2))
    assert prologue == R_INT_PROLOGUE, f"{R_INT} changed: {prologue}"
    at = fn + 4                                         # past push and sub sp
    rig.sim.monitor(f'cpu AddHook {at:#x} "self.GetMachine().SystemBus.WriteDoubleWord('
                    f'{ICSR:#x}, {PENDSTSET:#x}); self.RemoveHooksAt({at:#x})"', board="ams")
    rig.run(300)                                        # correct() runs every 50 ms
    assert rig.sim.in_app("ams") and rig.ams() == RUN, f"AMS state {rig.ams()}"
    assert rig.ts_active() == 1


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
