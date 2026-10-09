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

IFS08-CE-AMS#599 (AMS main): CurrentSensorTask's 1 KB stack left 32 B below
update_soc()'s deepest call, and an interrupt landing there (up to 104 B with
FPU state) overflowed it. vApplicationStackOverflowHook (freertos.c:77-98)
names the task in g_stack_overflow_task_name, opens the relays, latches Error
and spins until the IWDG resets the AMS into its bootloader. AMS dev carries
the fix (#601: a 2 KB stack, main.c:104-110). Whether an interrupt lands in
that window is a race the interrupt timing decides, and between boards that
timing varies from run to run, so Rig.run watches the hook's global every
step and fails the run there for any task: a test's own assertions so never
read the stale RAM of an AMS that reset into its bootloader
(g_state_telemetry still saying Run). One test forces an interrupt into
CurrentSensorTask's deepest frame instead and checks the margin #601 bought;
it reads the frame off AMS dev's Release image, and on any other build its
prologue checks fail first.
"""
import pytest

from vhil import elf, snapshot
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
# CurrentSensorTask's deepest frame on AMS dev (1508d13, Release -Os since
# IFS08-CE-AMS#610), by the prologues on the path: the cycle's restart_into
# (current_task.cpp:413) -> start_capture, inlined (:241) -> configure_channel
# (:225-235) -> HAL_ADC_ConfigChannel. From the task's entry: 8 (StartCurrent-
# SensorTask) + 168 (ams_current_sensor_task_run) + 16 (restart_into) + 48
# (configure_channel) + 32 (HAL_ADC_ConfigChannel) = 272 B. #599's frame,
# r_int_element_ohm, is inlined into KalmanSoc::correct now (248 B deep). The
# FreeRTOS tick path below osDelayUntil goes 8 B deeper, but xTaskResumeAll
# runs it with BASEPRI raised, where no kernel-aware interrupt lands.
ADC_CONFIG = "HAL_ADC_ConfigChannel"
ADC_CONFIG_PROLOGUE = (0x2300, 0xB5F7)          # movs r3, #0; push {r0-r2, r4-r7, lr}
CONFIGURE = "_ZN12_GLOBAL__N_117configure_channelEm"
CONFIGURE_PROLOGUE = (0xB510, 0xB08A)           # push {r4, lr}; sub sp, #40
RESTART = "_ZN12_GLOBAL__N_112restart_intoEh"
# From HAL_ADC_ConfigChannel's SP past its push to configure_channel's saved
# LR: its own 32 B, then configure_channel's 40 B and r4.
CONFIGURE_LR_AT = 32 + 40 + 4
DEEPEST_B = 272
EXC_FRAME_B = 32                               # the basic exception frame (ARMv7-M B1.5.7)
CURRENT_TASK = "CurrentSensorTask_attributes"   # osThreadAttr_t, .stack_size at +20 (cmsis_os2.h)
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
        AMS main.c:578-580), so a 10 ms step reads its global before the
        next boot's startup clears it."""
        if not self.sim.read_symbol("ams", OVERFLOW_TASK):
            return
        address, _ = elf.symbol(self.sim.firmware["ams"], OVERFLOW_TASK)
        raw = self.sim.monitor(f"sysbus ReadBytes {address:#x} 16", board="ams")
        task = parse_bytes(raw).split(b"\0")[0].decode(errors="replace")
        at = f"{self.sim.now_us() / 1e6:.3f} s"
        hint = " (isc-fs/IFS08-CE-AMS#599, fixed by #601)" if task == AMS_599 else ""
        pytest.fail(f"the AMS's stack-overflow hook fired at {at} for task {task!r}{hint}")

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


def test_an_interrupt_at_the_current_tasks_deepest_frame_leaves_the_ams_in_run(rig):
    """The race the arming test can lose, decided: in Run, one interrupt (a
    pended SysTick) lands as HAL_ADC_ConfigChannel, called from the cycle's
    restart_into, has reserved its frame, the deepest point of
    CurrentSensorTask (DEEPEST_B). On the car any interrupt can land there
    (TIM23's 1 kHz HAL tick, SysTick, FDCAN RX), and its exception frame (up
    to 104 B with FPU state, plus PendSV's save if it switches tasks) must fit
    the task's stack: the AMS stays in Run and the ECU keeps ts_active. On
    dev's 2 KB stack (#601) that holds by a wide margin; this guards it.

    The hook halts the AMS there first, so the test reads the depth it hit
    off the stack pointer instead of trusting it."""
    rig.arm()
    assert rig.wait_ams(RUN, 1000) is not None
    sim, image = rig.sim, rig.sim.firmware["ams"]
    mon = lambda cmd: sim.monitor(cmd, board="ams").strip()

    def prologue(name):
        at = elf.symbol(image, name)[0] & ~1
        return at, tuple(int(mon(f"sysbus ReadWord {at + 2 * i:#x}"), 16) for i in range(2))
    fn, words = prologue(ADC_CONFIG)
    assert words == ADC_CONFIG_PROLOGUE, f"{ADC_CONFIG} changed: {words}"
    _, words = prologue(CONFIGURE)
    assert words == CONFIGURE_PROLOGUE, f"{CONFIGURE} changed: {words}"
    lo, size = elf.symbol(image, RESTART)
    lo &= ~1
    at = fn + 4                                         # past movs and push
    # Only the call under restart_into: the cycle's single-ended read
    # (read_leg_q4, inlined in the task's loop) calls it 16 B shallower.
    mon(f'cpu AddHook {at:#x} "lr = self.GetMachine().SystemBus.ReadDoubleWord('
        f'self.SP.RawValue + {CONFIGURE_LR_AT}) & 0xFFFFFFFE; '
        f'{lo:#x} <= lr < {lo + size:#x} and (setattr(self, \'IsHalted\', True), '
        f'self.RemoveHooksAt({at:#x}))"')
    for _ in range(100):                                # restart_into runs every 50 ms
        sim.run_for(ms=1)
        if mon("cpu IsHalted") == "True":
            break
    else:
        pytest.fail("restart_into never called HAL_ADC_ConfigChannel")
    assert int(mon("cpu PC"), 16) == at
    tcb = sim.read_symbol("ams", "CurrentSensorTaskHandle", 4)
    stack = int(mon(f"sysbus ReadDoubleWord {tcb + snapshot.TCB_STACK:#x}"), 16)
    attrs = elf.symbol(image, CURRENT_TASK)[0]
    stack_b = int(mon(f"sysbus ReadDoubleWord {attrs + 20:#x}"), 16)
    depth = stack + stack_b - int(mon('cpu GetRegister "SP"'), 16)
    # The task starts at the top of its stack, 8-byte aligned (port.c).
    assert DEEPEST_B <= depth <= DEEPEST_B + 8, f"halted {depth} B deep"
    mon(f"sysbus WriteDoubleWord {ICSR:#x} {PENDSTSET:#x}")
    mon("cpu IsHalted false")
    rig.run(300)
    assert sim.in_app("ams") and rig.ams() == RUN, f"AMS state {rig.ams()}"
    assert rig.ts_active() == 1
    snap = snapshot.take(sim, "ams")
    task = next((t for t in snap.tasks if t.name == "CurrentSensorTask"), None)
    assert task is not None and task.stack_free is not None, snap.errors
    used = stack_b - task.stack_free
    assert depth + EXC_FRAME_B <= used < stack_b, f"CurrentSensorTask used {used} of {stack_b} B"


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


class StrandedLinkRig(Rig):
    """The DC link with its discharge path (ECU discharge.hpp:3-23, AMS
    acu_discharge_interlock.def:5-16): the pack charges it while AIR- and
    PRE or AIR+ are closed; otherwise the bleed resistor drains it while the
    shutdown circuit is open (TSMS off: the NC discharge relay drops) or the
    ECU secures the discharge (PB6 high: its coil-interrupt relay opens the
    discharge relay's coil); with the SDC closed and the ECU permitting,
    nothing drains it: a stranded link. The bleed is an RC decay,
    BLEED_TAU_MS."""
    BLEED_TAU_MS = 300

    def __init__(self, sim):
        super().__init__(sim)
        self.ecu_io = sim.io("ecu")
        self.secure_pin = self.ecu_io.watch(GPIOB, 6)
        self.tsms = False
        self.volts = 0.0

    def set_tsms(self, on):
        self.tsms = on
        self.ams_io.set_input(GPIOF, TSMS, on)

    def run(self, ms, step_ms=10):
        for _ in range(int(ms // step_ms)):
            self.sim.run_for(ms=step_ms)
            self._check_overflow()
            closed = {n: self.ams_io.level(p) for n, p in self.pins.items()}
            if closed["air_n"] and (closed["pre"] or closed["air_p"]):
                self.volts = PACK_V
            elif not self.tsms or self.ecu_io.level(self.secure_pin):
                self.volts *= 1 - step_ms / self.BLEED_TAU_MS
                if self.volts < 1:
                    self.volts = 0.0
            self._link(int(self.volts))

    def press(self):
        self.ams_io.set_input(GPIOF, DASH_CHG, True)
        self.run(50)
        self.ams_io.set_input(GPIOF, DASH_CHG, False)


@pytest.fixture
def stranded(images):
    with Sim(REPO / "systems" / "ecu-ams.yaml", images("ecu-ams")) as sim:
        rig = StrandedLinkRig(sim)
        sim.wait_for_app()
        rig.run(3000)
        yield rig


def test_a_stranded_link_is_drained_before_the_car_re_arms(stranded):
    """The discharge interlock end to end, AMS and ECU on one bus (AMS
    FMEA.md DISCHARGE-1: 'both halves exist, the pairing is unproven'). The
    shutdown circuit is cycled in Run faster than the bleed: the AMS drops to
    Start (not a fault), the link is left stranded at a few hundred volts.
    The AMS reports fsm_in_start + tsms on 0x021; the ECU, seeing the charged
    link on 0x466, secures the discharge (PB6) and reports discharge_engaged
    on 0x100; the AMS refuses a re-arm while it is engaged (the press is
    spent, state_machine.hpp:267-283); the ECU releases on its own reading
    below DischargeReleaseV = 10 V; then, and only on a new press, the car
    arms and reaches Run again. The plant's RC is uncommissioned (M8, #147):
    sequencing is asserted, not the bleed's timing. The first arm can lose
    the race of IFS08-CE-ECU#259 (the ECU securing into the precharge); the
    test then waits out the ECU's 30 s timeout and goes on."""
    rig = stranded
    rig.set_tsms(True)
    rig.run(100)
    rig.press()
    assert rig.wait_ams(RUN, 1000) is not None, f"AMS state {rig.ams()}"
    assert rig.volts == PACK_V
    if rig.ecu_io.level(rig.secure_pin):
        # IFS08-CE-ECU#259: the arm's stale 0x021 let the ECU secure into the
        # precharge (whether it does is a race of the boards' timing; the
        # deterministic form is a strict xfail in test_ecu_discharge.py). It
        # holds to DischargeTimeoutMs and gives up with a fault, which the
        # SDC opening below clears; the stranded-link contract is checked
        # from there either way.
        for _ in range(320):
            rig.run(100)
            if not rig.ecu_io.level(rig.secure_pin):
                break
        assert not rig.ecu_io.level(rig.secure_pin), "secured past DischargeTimeoutMs"
        assert rig.ams() == RUN
    t_open = rig.sim.now_us()
    rig.set_tsms(False)                                  # SDC opened ...
    rig.run(100)
    rig.set_tsms(True)                                   # ... and closed before the bleed ends
    assert (rig.ams(), rig.ams("g_fault_reason_telemetry")) == (START, 0)
    assert rig.volts > 100, f"link at {rig.volts:.0f} V: not stranded"
    t_closed = rig.sim.now_us()
    for _ in range(50):                                  # 0x021 every 100 ms, plus a tick
        rig.run(10)
        if rig.ecu_io.level(rig.secure_pin):
            break
    edges = rig.ecu_io.edges(rig.secure_pin, since_us=t_open)
    assert rig.ecu_io.level(rig.secure_pin), \
        f"the ECU never secured the stranded link: {edges} (fault {rig.sim.read_symbol('ecu', 'g_discharge_fault')})"
    # Secured within one 0x021 period and a tick of the SDC closing (the
    # edge's own stamp is not compared with t_closed: the boards' clocks
    # meet only at sync points).
    assert rig.sim.now_us() - t_closed <= 150_000, \
        f"secured {(rig.sim.now_us() - t_closed) / 1000} ms after the SDC closed"
    rig.run(30)
    assert rig.acu.last(0x100).data[2] & 1, "0x100 does not report the discharge engaged"
    rig.press()                                          # refused: the bleed is connected
    rig.run(100)
    assert rig.ams() == START, "the AMS armed into a connected bleed resistor"
    for _ in range(300):
        rig.run(10)
        if not rig.ecu_io.level(rig.secure_pin):
            break
    assert not rig.ecu_io.level(rig.secure_pin), "the ECU never released the discharge"
    assert rig.volts < 10, f"released at {rig.volts:.0f} V, above DischargeReleaseV"
    rig.run(300)
    assert not rig.acu.last(0x100).data[2] & 1
    assert rig.ams() == START, "the refused press armed the car by itself"
    rig.press()
    assert rig.wait_ams(RUN, 1000) is not None, f"no re-arm after the drain (AMS {rig.ams()})"
