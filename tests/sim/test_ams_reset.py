"""AMS state across resets (#23): the backup domain survives a warm reset and
is lost on a power cut (the MainLite has no VBAT).

Firmware facts (IFS08-CE-AMS):
  ErrorLatch in RTC BKP1R: set when Error latches, read at boot so the AMS
    comes up in Error (safety_task.cpp:130-135); flight build, no clear.
  Boot trigger 0x002 B0 07 AD 11 on the ACU bus, honoured in Start and Error
    (bootloader.hpp:61-74, ams_config.hpp:968-970): stamps the jump reason
    into BKP2R and resets (NVIC_SystemReset): a warm reset.
  0x6C4 bytes 0-3 = jump reason ('JUMP' 0x4A554D50 for the CAN trigger),
    0x6C0 byte 0 = state (5 = Error), 6 = reason; both once 0x7F0 DE AD BE EF
    arms the pit stream (re-armed after every boot: it lives in .bss).
  CellOverVoltageMv = 4200 (ams_config.hpp:23), fault reason 5.
"""
import pytest

from vhil import elf
from vhil.sim import Sim
from vhil.can_bootloader import CanBootloader
from vhil.system import REPO

PIT_ARM, PIT_FSM, BOOT_DIAG = 0x7F0, 0x6C0, 0x6C4
BOOT_TRIGGER = (0x002, bytes.fromhex("B007AD11"))
START, ERROR, CELL_OVER_VOLTAGE = 0, 5, 5
JUMP = 0x4A554D50
NODE, AUTO_JUMP_MS = 0x2, 2000      # the AMS role's bootloader node (catalog/boards/mainlite.yaml)


def _arm_and_read(sim, ms=2500):
    """Wait for the boot, arm the pit stream, return the last 0x6C0, 0x6C4."""
    can = sim.can("can_acu")
    sim.wait_for_app()
    sim.run_for(ms=1500)
    can.send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    sim.run_for(ms=ms)
    return can.last(PIT_FSM), can.last(BOOT_DIAG)


@pytest.fixture(scope="module")
def latched(images):
    """An AMS that latched Error on a transient over-voltage, now gone."""
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        status, _ = _arm_and_read(sim)
        assert status.data[0] == START, "AMS not healthy before the fault"
        sim.monitor("sysbus.spi1.isospi.cells2 SetCell 3 4300", board="ams")
        sim.run_for(ms=1500)
        sim.monitor("sysbus.spi1.isospi.cells2 SetCell 3 3700", board="ams")
        status = sim.can("can_acu").last(PIT_FSM)
        assert (status.data[0], status.data[6]) == (ERROR, CELL_OVER_VOLTAGE)
        yield sim


def _back_to_the_app(sim):
    """After the boot trigger the bootloader stays (BKP0R = BL_BOOT_REQ_MAGIC,
    no auto-jump: stm32-can-bootloader ARCHITECTURE.md "Boot flow"), as on the
    car: the app only comes back when a host jumps to it over CAN, as `cf`
    does (CONNECT, JUMP)."""
    sim.run_for(ms=AUTO_JUMP_MS + 500)
    assert not sim.in_app("ams"), "the bootloader did not stay after the trigger"
    bl = CanBootloader(sim, "can_acu", NODE)
    bl.connect()
    bl.jump()


def test_error_latch_survives_a_warm_reset(latched):
    """F-076 (flight build): the boot trigger's warm reset keeps BKP1R, so
    the AMS comes back in Error though the cell is healthy again."""
    latched.can("can_acu").send(*BOOT_TRIGGER)
    _back_to_the_app(latched)
    status, boot = _arm_and_read(latched)
    assert status.data[0] == ERROR, f"AMS state {status.data[0]} after a warm reset"
    assert int.from_bytes(boot.data[0:4], "little") == JUMP, "jump reason lost across the reset"


def test_a_power_cycle_clears_the_latch(latched):
    """No VBAT: the power cut wipes BKP1R and BKP2R, and the AMS boots clean."""
    latched.power_cycle("ams")
    status, boot = _arm_and_read(latched)
    assert (status.data[0], status.data[6]) == (START, 0), f"0x6C0 {status.data.hex()}"
    assert int.from_bytes(boot.data[0:4], "little") == 0, "jump reason survived a power cut"


# -- the boot trigger: honoured, refused, ignored -------------------------------
#
# bootloader.hpp:61-74: honoured only in Start and Error, only for exactly
# 0x002 B0 07 AD 11 (DLC 4) on the ACU bus; it stamps BKP0R = BlBootReqMagic
# (0xB00710AD, ams_config.hpp:958) and resets. Refused in an energised state,
# reported in 0x6C0 byte 2 bit 3 (boot_trigger_refused, pit_fsm_status.def).
# Energising needs TSMS (PF9) high, a DASH_CHG (PF10) rising edge, and in Car
# mode a fresh VCU heartbeat with the DC link discharged (rearm_permitted,
# state_machine.hpp): ams.yaml has no ECU, so 0x100 is a stimulus here.

BKP0R = 0x58004050
BL_BOOT_REQ_MAGIC = 0xB00710AD
HEALTH = 0x6CA
PRECHARGE = 1


def _bkp0r(sim):
    return int(sim.monitor(f"sysbus ReadDoubleWord {BKP0R:#x}", board="ams").strip(), 16)


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        _arm_and_read(sim)
        yield sim


def test_the_trigger_in_start_stamps_bkp0r_and_resets(ams):
    """D-051: BKP0R = BlBootReqMagic, then a software reset. The bootloader
    consumes BKP0R at boot (one-shot, main.c:1134-1139) and so stays: the
    board is parked in it past the auto-jump window, no app running. (The
    app's own report of the software reset can't show it: the bootloader
    clears RCC_RSR first, bl_health.c:59-63, test_reset_cause.py.)"""
    ams.can("can_acu").send(*BOOT_TRIGGER)
    ams.run_for(ms=AUTO_JUMP_MS + 500)
    assert not ams.in_app("ams"), "no reset into a parked bootloader after the trigger"
    assert _bkp0r(ams) == 0, "BL_BOOT_REQ_MAGIC not consumed"
    assert ams.can("can_acu").count([HEALTH], since_us=ams.now_us() - 2_000_000) == 0


@pytest.mark.parametrize("can_id, data", [
    (0x003, "B007AD11"),          # wrong ID
    (0x002, "B007AD"),            # wrong DLC
    (0x002, "B007AD12"),          # wrong payload
], ids=["id", "dlc", "payload"])
def test_a_near_miss_trigger_is_ignored(ams, can_id, data):
    """D-045 (the wrong-bus case needs the AMS's FDCAN2 on a bus)."""
    ams.can("can_acu").send(can_id, bytes.fromhex(data))
    ams.run_for(ms=2500)
    assert _bkp0r(ams) != BL_BOOT_REQ_MAGIC and ams.in_app("ams"), "parked in the bootloader"


def test_the_trigger_is_refused_while_energised(ams):
    """Refused in Precharge (energised): no reset, reported in 0x6C0."""
    can, io = ams.can("can_acu"), ams.io("ams")
    can.send_periodic("vcu", 0x100, bytes([0, 0, 0x02]), 10)     # 0 V, dc_bus_valid
    io.set_input("sysbus.gpioPortF", 9, True)                    # TSMS
    ams.run_for(ms=200)
    io.set_input("sysbus.gpioPortF", 10, True)                   # DASH_CHG edge
    ams.run_for(ms=100)
    io.set_input("sysbus.gpioPortF", 10, False)
    ams.run_for(ms=1100)                                         # 0x6C0 is 1 Hz
    assert can.last(PIT_FSM).data[0] == PRECHARGE, "AMS did not energise"
    can.send(*BOOT_TRIGGER)                                      # well inside PrechargeMaxMs (5 s)
    ams.run_for(ms=1200)
    status = can.last(PIT_FSM)
    assert status.data[0] == PRECHARGE and status.data[2] & 0x08, \
        f"0x6C0 {status.data.hex()}: refusal not reported, or left Precharge"
    assert _bkp0r(ams) != BL_BOOT_REQ_MAGIC and ams.in_app("ams"), "rebooted while energised"


# -- a watchdog reset out of a masked context -----------------------------------
#
# freertos.c:77-98: vApplicationStackOverflowHook latches Error (BKP1R) and
# spins for the IWDG (~100 ms, main.c MX_IWDG1_Init). The scheduler calls it
# from vTaskSwitchContext (tasks.c:3112), inside PendSV with BASEPRI =
# configMAX_SYSCALL_INTERRUPT_PRIORITY (ARM_CM4F port.c:480-484;
# FreeRTOSConfig.h:149,156: 5 << 4), so that reset comes with BASEPRI raised.

MAX_SYSCALL_BASEPRI = 0x50


def test_a_watchdog_reset_from_the_overflow_hook_boots_back_in_error(ams):
    """On the chip the reset clears BASEPRI, the bootloader auto-jumps after
    its 2 s window and the app comes up in Error from the latch. Renode 1.17
    kept the mask (renode/renode#1021): the bootloader's HAL tick never ran and
    the board stayed in it for good. Every reset now clears it
    (catalog/platforms/stm32h733.yaml, renode.reset)."""
    hook, _ = elf.symbol(ams.firmware["ams"], "vApplicationStackOverflowHook")
    ams.monitor(f'cpu SetRegister "BasePri" {MAX_SYSCALL_BASEPRI:#x}', board="ams")
    ams.monitor(f"cpu PC {hook & ~1:#x}", board="ams")      # Thumb bit off
    ams.run_until(lambda: not ams.in_app("ams"), timeout_ms=500)
    ams.wait_for_app("ams", timeout_ms=AUTO_JUMP_MS + 1000)
    status, _ = _arm_and_read(ams)
    assert status.data[0] == ERROR, f"AMS state {status.data[0]} after the watchdog reset"
