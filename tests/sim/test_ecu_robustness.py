"""ecu-robustness (#47): what the ECU does when something inside it, or on its
buses, goes wrong: a wedged ControlTask, a CPU fault, a TX FIFO that cannot
keep up, malformed and unexpected frames.

ECU facts (IFS08-CE-ECU):
  IWDG1: prescaler 32, reload 500 on the 32 kHz LSI = 500 ms (iwdg.c:41-43).
    ControlTask is the only kicker, once per 10 ms tick, after it has posted
    the tick's frames (control_task.cpp:423-427); a wedged ControlTask is meant
    to end in a watchdog reset (watchdog.hpp:3-7).
  0x704 (DiagTask, 1 s, ungated): byte 5 b0-b2 reset_cause (1 PowerOn, 2 Pin,
    3 Software, 4 IWDG), byte 6 uptime_s, byte 7 last_fault, both captured once
    at DiagTask start (diag_task.cpp:30-31, pit_diag_health.def:28-32,74-75).
  Fault latch: HardFault/MemManage/BusFault/UsageFault handlers stamp
    0xFA1700F1..F4 into RTC BKP1R and spin (stm32h7xx_it.c:90-145); the stack-
    overflow and malloc-failed hooks stamp F5/F6 (freertos.c:145-152,156-169).
    The latch survives warm resets, not a power cut: no VBAT on the carrier
    (error_latch.hpp:3-11, catalog/boards/mlc-carrier.yaml). Only a bench build
    (ECU_HIL_CLEAR_ERROR_LATCH) clears it at boot (app_init_task.cpp:65-71).
  configCHECK_FOR_STACK_OVERFLOW = 2 (FreeRTOSConfig.h:79): at each switch-out
    FreeRTOS checks the 16 bytes at the bottom of the task's stack still hold
    the 0xA5 fill.
  TX: every frame goes through can_tx_queue (32 deep, freertos.c:199) to
    CanTxTask, which drops a frame the HAL refuses without counting it
    (can_tx_task.cpp:50-52); only a full queue counts, into g_can_tx_dropped,
    the sticky tx_dropped bit of 0x700 (can_tx_task.cpp:59-72,
    pit_diag.cpp:48). FDCAN2's (ACU) hardware TX FIFO is 16 deep (fdcan.c:115).
    A pit-diag tick posts 13 ACU frames (control_task.cpp:377-396) after 0x100
    and 0x506, and the 100 ms uDV tick adds 0x504/0x505/0x511
    (control_task.cpp:305-338): 18 frames when the two ticks coincide.
  RX: both buses accept standard IDs only, reject extended and remote frames
    in hardware (app_init_task.cpp:47-55); the ISR drains FIFO0 into the 32-deep
    can_rx_queue, counting drops in g_can_rx_isr_drop (can_isr.cpp:39-60).
    Each parser rejects a short DLC: 0x466 < 4 (vehicle_service.cpp:220-221),
    0x463 < 8 (:163-164), 0x4A0 < 8 (:259-260); the pit-diag arm 0x7E0 needs
    DLC >= 4 (can_rx_task.cpp:79-80), the boot trigger 0x002 exactly DLC 4,
    0xB007AD12, on the ACU bus (bootloader.hpp:79-85), and reboots only below
    R2dDelay or in AmsError (bootloader.hpp:73-75, can_rx_task.cpp:66-75).
  0x463 rpm: 20-bit signed at frame bit 44, sign-extended from bit 19
    (vehicle_service.cpp:96-105); 0x506 = erpm / MotorPolePairs (10),
    truncated toward zero, s32 LE (udv_tx.cpp:42-52, ecu_config.hpp:318).

Fault injection. Nothing here patches the image; two faults are forced on the
CPU through a Renode execution hook, each a failure the firmware is written
to survive:
  - a wedge: ControlTask's thread is sent into a branch-to-self (the startup
    file's Default_Handler loop, startup_stm32h733zgtx.s:115-117) at the top
    of a tick. That is any non-terminating loop in the realtime task: a
    peripheral poll with no timeout, a corrupted loop bound. At realtime
    priority it starves every lower task, as a real one would.
  - a corrupted return address: Watchdog::refresh's saved LR loses its Thumb
    bit, so its `pop {r7, pc}` takes an INVSTATE UsageFault, escalated to
    HardFault (UsageFault is not enabled): a stack overwrite of a return
    address, the commonest way firmware ends up in its fault handler.
A stack overflow is modelled by its first physical effect: the canary words at
the bottom of DiagTask's stack overwritten, nothing above them.
"""
import pytest

from vhil import elf
from vhil.sim import Sim
from vhil.system import REPO

HEARTBEAT, HEALTH, PIT_STATUS = 0x100, 0x704, 0x700
PIT_ARM, PIT_ACK, UDV_TICK, MOTOR_RPM = 0x7E0, 0x7E1, 0x504, 0x506
BOOT_TRIGGER, VDC, INV_RPM, AMS_STATUS = 0x002, 0x466, 0x463, 0x4A0
ARM = bytes.fromhex("DEADBEEF")
TRIGGER = bytes.fromhex("B007AD12")

TICK_MS, IWDG_MS, PIT_MS = 10, 500, 100
POWER_ON, PIN, SOFTWARE, IWDG = 1, 2, 3, 4
NO_FAULT, HARD_FAULT, STACK_OVERFLOW = 0x00, 0xF1, 0xF5
WAIT_VDC, PRECHARGE, AMS_ERROR = 0, 1, 6
# 0x700-0x70D but 0x704 (DiagTask's): one of each per pit-diag tick.
PIT_DIAG_IDS = [i for i in range(0x700, 0x70E) if i != HEALTH]

TCB_PX_STACK, TCB_NAME = 48, 52        # FreeRTOS tasks.c TCB_t, no MPU/list checks
BRANCH_TO_SELF = 0xE7FE                # Thumb `b .`
VDC_FRAME = bytes([0, 0, 0x5E, 0x01, 0, 0])     # 350 V


@pytest.fixture
def ecu(firmware):
    with Sim(REPO / "systems" / "ecu.yaml", {"ecu": firmware("ecu")}) as sim:
        sim.run_for(ms=1500)
        yield sim


# -- helpers --------------------------------------------------------------------

def _addr(sim, symbol):
    return elf.symbol(sim.firmware["ecu"], symbol)[0] & ~1


def _pc(sim):
    return int(sim.monitor("cpu PC").strip(), 16)


def _health(sim, since_us=0):
    """[(t_us, reset_cause, last_fault, uptime_s)] of every 0x704 since."""
    return [(f.t_us, f.data[5] & 0x7, f.data[7], f.data[6])
            for f in sim.can("can_acu").frames(HEALTH, since_us)]


def _reset_seen(sim):
    """RCC_RSR holds a reset's flags until DiagTask clears them at boot
    (reset_cause.cpp:23); between, it reads 0."""
    return int(sim.monitor("vhil_reset_ecu Rsr").strip(), 16) != 0


def _heartbeat_gaps_ms(sim, since_us):
    """Heartbeat intervals over 1.5 ticks: a reset shows as one (a software
    reboot's is about 21 ms: osDelay(10) in request_reboot, then the boot)."""
    hb = sim.can("can_acu").frames(HEARTBEAT, since_us)
    return [(b.t_us - a.t_us) / 1000 for a, b in zip(hb, hb[1:]) if b.t_us - a.t_us > 1.5 * TICK_MS * 1000]


def _hook_once(sim, at, python, done, timeout_ms=50):
    """Run `python` on the CPU when it reaches `at`, until done() holds; then
    take the hook off, so the board's next boot runs clean."""
    sim.monitor(f'cpu AddHook {at:#x} "from Antmicro.Renode.Peripherals.CPU import RegisterValue; {python}"')
    try:
        sim.run_until(done, timeout_ms=timeout_ms, step_ms=1)
    finally:
        sim.monitor(f"cpu RemoveHooksAt {at:#x}")


def _wedge_control_task(sim):
    """Send ControlTask into a branch-to-self at the top of its next tick."""
    loop = int(sim.monitor('sysbus GetSymbolAddress "Default_Handler"').strip(), 16)
    assert int(sim.monitor(f"sysbus ReadWord {loop:#x}").strip(), 16) == BRANCH_TO_SELF
    _hook_once(sim, _addr(sim, "_ZN3ecu9IoSignals4readERNS_8IoInputsE"),
               f"self.PC = RegisterValue.Create({loop:#x}, 32)", lambda: _pc(sim) == loop)


def _corrupt_return_address(sim):
    """Clear the Thumb bit of Watchdog::refresh's return address."""
    handler = _addr(sim, "HardFault_Handler")
    _hook_once(sim, _addr(sim, "_ZN3ecu8Watchdog7refreshEv"),
               "self.LR = RegisterValue.Create(self.LR.RawValue & 0xFFFFFFFE, 32)",
               lambda: handler <= _pc(sim) < handler + 16)


def _overflow_diag_task_stack(sim):
    """Overwrite the first canary word at the bottom of DiagTask's stack."""
    tcb = sim.read_symbol("ecu", "DiagTaskHandle", 4)
    name = bytes(int(sim.monitor(f"sysbus ReadByte {tcb + TCB_NAME + i:#x}").strip(), 16)
                 for i in range(8))
    assert name == b"DiagTask", f"TCB layout: name reads {name!r}"
    bottom = int(sim.monitor(f"sysbus ReadDoubleWord {tcb + TCB_PX_STACK:#x}").strip(), 16)
    assert sim.monitor(f"sysbus ReadDoubleWord {bottom:#x}").strip() == "0xA5A5A5A5"
    sim.monitor(f"sysbus WriteDoubleWord {bottom:#x} 0x0")


def _arm_pit_diag(sim, after_udv_ms):
    """Arm pit-diag so its 100 ms tick lands after_udv_ms after the uDV one
    (0 = on it): the arm is handled within a tick and the first pit-diag tick
    follows at once, so send it a few ms before."""
    acu = sim.can("can_acu")
    last = acu.last(UDV_TICK).t_us
    now = sim.now_us()
    target = last + ((now - last) // (PIT_MS * 1000) + 2) * PIT_MS * 1000 + after_udv_ms * 1000
    sim.run_for(us=target - 5000 - now)
    acu.send(PIT_ARM, ARM)


def _rpm_frame(raw, dlc=8):
    d = bytearray(8)
    d[5], d[6], d[7] = (raw & 0xF) << 4, (raw >> 4) & 0xFF, (raw >> 12) & 0xFF
    return bytes(d[:dlc])


# -- watchdog -------------------------------------------------------------------

def test_a_wedged_control_task_trips_the_iwdg_in_500_ms(ecu):
    """I-002 / gap 9: no kick for 500 ms -> IWDG reset; the next boot reports
    it, with no fault latched (a wedge is not a CPU fault), and the heartbeat
    is back. No 0x704 says so during the stall: DiagTask is starved too, and
    its 1 s period is longer than the dog's 500 ms anyway."""
    t0 = ecu.now_us()
    _wedge_control_task(ecu)
    acu = ecu.can("can_acu")
    last_kick = acu.last(HEARTBEAT).t_us       # posted just before that tick's kick
    ecu.run_for(us=last_kick + (IWDG_MS - 20) * 1000 - ecu.now_us())
    assert not _reset_seen(ecu), "reset before the watchdog period"
    assert acu.count([HEARTBEAT, HEALTH], since_us=last_kick + 1) == 0, "the stalled ECU kept talking"
    reset = ecu.run_until(lambda: _reset_seen(ecu), timeout_ms=60, step_ms=1)
    assert abs((reset - last_kick) / 1000 - IWDG_MS) <= TICK_MS
    ecu.run_for(ms=1500)
    _, cause, fault, uptime = _health(ecu, since_us=reset)[0]
    assert (cause, fault, uptime) == (IWDG, NO_FAULT, 0)
    assert acu.count([HEARTBEAT], since_us=ecu.now_us() - 1_000_000) >= 99
    assert len(_heartbeat_gaps_ms(ecu, t0 - 2 * TICK_MS * 1000)) == 1


def test_the_dog_stays_fed_in_amserror(ecu):
    """watchdog.hpp:4-6: ControlTask kicks on its fault path too, so a car
    parked in AmsError stays up to report it instead of reset-looping."""
    ecu.can("can_acu").send_periodic("ams", AMS_STATUS, bytes([5, 0, 0x1F, 0, 0x0E, 0x74, 0x0E, 0x74]), 10)
    t0 = ecu.run_for(ms=100)
    assert ecu.read_symbol("ecu", "g_last_ctrl_state") == AMS_ERROR
    ecu.run_for(ms=3000)
    assert _heartbeat_gaps_ms(ecu, t0) == []
    uptimes = [h[3] for h in _health(ecu, since_us=t0)]
    assert uptimes == sorted(uptimes) and uptimes[-1] >= 4, f"uptime {uptimes}"


# -- fault latch across reset ----------------------------------------------------

def test_a_hard_fault_names_itself_after_the_watchdog_reset(ecu):
    """Gap 10 / deferred I-004: the handler latches 0xF1 and spins; the dog
    resets the board and the next boot's 0x704 carries the fault."""
    _corrupt_return_address(ecu)
    reset = ecu.run_until(lambda: _reset_seen(ecu), timeout_ms=IWDG_MS + 50, step_ms=5)
    ecu.run_for(ms=1500)
    _, cause, fault, _ = _health(ecu, since_us=reset)[0]
    assert (cause, fault) == (IWDG, HARD_FAULT)


def test_the_fault_latch_outlives_warm_resets_not_a_power_cut(ecu):
    """A flight image never clears the latch (app_init_task.cpp:65-71): a pin
    reset still reports the fault; a power cut wipes BKP1R (no VBAT)."""
    _corrupt_return_address(ecu)
    ecu.run_for(ms=IWDG_MS + 1500)
    t = ecu.now_us()
    ecu.monitor("vhil_reset_ecu PinReset")
    ecu.run_for(ms=1500)
    _, cause, fault, _ = _health(ecu, since_us=t)[0]
    assert (cause, fault) == (PIN, HARD_FAULT)
    t = ecu.now_us()
    ecu.power_cycle("ecu")
    ecu.run_for(ms=1500)
    assert _health(ecu, since_us=t)[0][2] == NO_FAULT


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-ECU#252: vApplicationStackOverflowHook (freertos.c:145-152) "
    "latches 0xF5 and returns, so the ECU runs on with a corrupted stack and never "
    "resets; DiagTask only reads the latch at boot (diag_task.cpp:31), so the "
    "overflow is never reported. error_latch.hpp:4-5 says the hooks spin into the "
    "watchdog reset"))
def test_a_stack_overflow_resets_and_names_itself(ecu):
    t = ecu.now_us()
    _overflow_diag_task_stack(ecu)
    ecu.run_for(ms=3000)                       # next DiagTask switch-out <= 1 s, + the dog
    assert [h[1:3] for h in _health(ecu, since_us=t)][-1] == (IWDG, STACK_OVERFLOW)


# -- TX overload -------------------------------------------------------------------

IFS_HIL_128 = pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-ECU#251, root cause of IFS_HIL#128: a pit-diag tick that "
    "coincides with the uDV tick posts 18 ACU frames (control_task.cpp:305-338,"
    "377-396) into FDCAN2's 16-deep TX FIFO (fdcan.c:115) faster than the bus "
    "drains it; CanTxTask ignores the refusal (can_tx_task.cpp:50-52, whose comment "
    "assumes 32 deep), so 0x703 and 0x705 never reach the bus and tx_dropped stays 0"))


@pytest.mark.parametrize("after_udv_ms", [
    pytest.param(0, marks=IFS_HIL_128, id="on-the-udv-tick"),
    pytest.param(PIT_MS // 2, id="between-udv-ticks"),
])
def test_a_pit_diag_tick_reaches_the_bus_whole(ecu, after_udv_ms):
    """Gap 15 / IFS_HIL#128, with the bus's real drain rate (FDCAN WireTiming):
    every pit-diag frame once per tick, or, if one was dropped, tx_dropped
    set. Between uDV ticks a pit-diag tick is 15 ACU frames and fits."""
    ecu.monitor("sysbus.fdcan2_h7 WireTiming true")
    _arm_pit_diag(ecu, after_udv_ms)
    t = ecu.run_for(ms=1050)
    acu = ecu.can("can_acu")
    counts = {hex(i): acu.count([i], since_us=t - 1_000_000) for i in PIT_DIAG_IDS}
    tx_dropped = (acu.last(PIT_STATUS).data[2] >> 6) & 1
    assert all(n == 10 for n in counts.values()) or tx_dropped, f"per-ID counts in 1 s: {counts}"


def test_a_full_acu_tx_fifo_never_stalls_control(ecu):
    """A refused add returns at once (can_tx_task.cpp:52): with FDCAN2's TX FIFO
    full, nothing reaches the ACU bus but ControlTask keeps ticking and
    kicking. The heartbeat is back within a tick of the FIFO freeing, with no
    reset in between."""
    acu = ecu.can("can_acu")
    ecu.monitor("sysbus.fdcan2_h7 TxFifoFull true")
    t = ecu.run_for(ms=2 * IWDG_MS)
    assert acu.count(since_us=t - 2 * IWDG_MS * 1000 + TICK_MS * 1000) == 0
    ecu.monitor("sysbus.fdcan2_h7 TxFifoFull false")
    ecu.run_for(ms=2 * TICK_MS)
    assert acu.count([HEARTBEAT], since_us=t) >= 1
    ecu.run_for(ms=1500)
    assert not any(cause == IWDG or uptime == 0 for _, cause, _, uptime in _health(ecu, since_us=t))


# -- malformed and unexpected frames --------------------------------------------------

@pytest.mark.parametrize("bus, can_id, short, full, state", [
    pytest.param("can_inv", VDC, VDC_FRAME[:3], VDC_FRAME[:4], PRECHARGE, id="0x466-dlc3"),
    pytest.param("can_acu", AMS_STATUS, bytes([5, 0, 0x1F, 0, 0x0E, 0x74, 0x0E]),
                 bytes([5, 0, 0x1F, 0, 0x0E, 0x74, 0x0E, 0x74]), AMS_ERROR, id="0x4A0-dlc7"),
])
def test_short_frames_change_nothing(ecu, bus, can_id, short, full, state):
    """Gap 17: one byte short of the parser's guard, the frame is ignored
    (no gate opens, no AmsError); the full frame acts, so the guard is what
    stopped it."""
    can = ecu.can(bus)
    can.send_periodic("short", can_id, short, TICK_MS)
    ecu.run_for(ms=300)
    assert ecu.read_symbol("ecu", "g_last_ctrl_state") == WAIT_VDC
    can.stop_periodic("short")
    can.send_periodic("full", can_id, full, TICK_MS)
    ecu.run_for(ms=50)
    assert ecu.read_symbol("ecu", "g_last_ctrl_state") == state


def test_extended_and_short_frames_are_not_commands(ecu):
    """The hardware filter drops extended IDs before any parser sees them:
    an extended 0x466 opens no gate and an extended 0x7E0 arms nothing; a
    3-byte 0x7E0 isn't the magic. The real arm is acked."""
    acu, inv = ecu.can("can_acu"), ecu.can("can_inv")
    t = ecu.now_us()
    inv.send_periodic("vdc", VDC, VDC_FRAME, TICK_MS, extended=True)
    acu.send(PIT_ARM, ARM, extended=True)
    acu.send(PIT_ARM, ARM[:3])
    ecu.run_for(ms=300)
    assert ecu.read_symbol("ecu", "g_last_ctrl_state") == WAIT_VDC
    assert acu.count([PIT_ACK, PIT_STATUS], since_us=t) == 0
    acu.send(PIT_ARM, ARM)
    ecu.run_for(ms=2 * TICK_MS)
    assert acu.count([PIT_ACK], since_us=t) == 1


def test_only_the_exact_boot_trigger_reboots(ecu):
    """In WaitInvVdcConfig (reboot allowed) a trigger one byte short or long,
    extended, with a wrong byte or on the inverter bus is no trigger: no
    reset, not even counted as refused. The exact frame is a software reset."""
    acu = ecu.can("can_acu")
    t = ecu.now_us()
    for frame in (TRIGGER[:3], TRIGGER + b"\x00", bytes.fromhex("B007AD11")):
        acu.send(BOOT_TRIGGER, frame)
    acu.send(BOOT_TRIGGER, TRIGGER, extended=True)
    ecu.can("can_inv").send(BOOT_TRIGGER, TRIGGER)
    ecu.run_for(ms=1100)
    assert _heartbeat_gaps_ms(ecu, t) == []
    assert ecu.read_symbol("ecu", "g_boot_trigger_refused", 4) == 0
    t = ecu.now_us()
    acu.send(BOOT_TRIGGER, TRIGGER)
    ecu.run_for(ms=1500)
    assert len(_heartbeat_gaps_ms(ecu, t)) == 1
    _, cause, _, uptime = _health(ecu, since_us=t)[0]
    assert (cause, uptime) == (SOFTWARE, 0)


@pytest.mark.parametrize("raw, dlc, rpm", [
    (0x7FFFF, 8, 52428),        # +524287 erpm, the largest
    (0x80000, 8, -52428),       # -524288, sign bit alone: truncates toward zero
    (0xFFFFF, 8, 0),            # -1 erpm
    (0x00064, 8, 10),           # 100 erpm
    (0x7FFFF, 7, 0),            # one byte short: ignored, rpm stays at boot 0
])
def test_motor_rpm_at_the_20_bit_limits(ecu, raw, dlc, rpm):
    """Gap 17: 0x463's 20-bit field sign-extends at its limits; a short frame
    is ignored. Read back on 0x506 (erpm / 10)."""
    ecu.can("can_inv").send(INV_RPM, _rpm_frame(raw, dlc))
    t = ecu.run_for(ms=3 * TICK_MS)
    frame = ecu.can("can_acu").last(MOTOR_RPM, since_us=t - TICK_MS * 1000)
    assert int.from_bytes(frame.data[0:4], "little", signed=True) == rpm


def test_an_rx_flood_does_not_disturb_control(ecu):
    """A babbling node at ~90 % of the ACU bus (4000 8-byte frames/s, lowest
    priority ID) with pit-diag streaming: the heartbeat keeps its exact 10 ms,
    nothing is dropped on the RX path, a command still gets through, and the
    board never resets."""
    acu = ecu.can("can_acu")
    acu.send(PIT_ARM, ARM)
    for k in range(4):
        acu.send_periodic(f"flood{k}", 0x7FF, bytes(range(8)), 1, start_us=ecu.now_us() + 250 * k)
    t = ecu.run_for(ms=500)
    acu.send(PIT_ARM, bytes(4))                # disarm: acked with 0
    ecu.run_for(ms=1500)
    hb = acu.frames(HEARTBEAT, since_us=t)
    assert {b.t_us - a.t_us for a, b in zip(hb, hb[1:])} == {TICK_MS * 1000}
    assert [f.data[0] for f in acu.frames(PIT_ACK, since_us=t)] == [0]
    assert ecu.read_symbol("ecu", "g_can_rx_isr_drop", 4) == 0
    assert not any(cause == IWDG or uptime == 0 for _, cause, _, uptime in _health(ecu, since_us=t))
