"""AMS state across resets (#23): the backup domain survives a warm reset and
is lost on a power cut (the MLC carrier has no VBAT).

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

from vhil.sim import Sim
from vhil.system import REPO

PIT_ARM, PIT_FSM, BOOT_DIAG = 0x7F0, 0x6C0, 0x6C4
BOOT_TRIGGER = (0x002, bytes.fromhex("B007AD11"))
START, ERROR, CELL_OVER_VOLTAGE = 0, 5, 5
JUMP = 0x4A554D50


def _arm_and_read(sim, ms=2500):
    """Wait for the boot, arm the pit stream, return the last 0x6C0, 0x6C4."""
    can = sim.can("can_acu")
    sim.run_for(ms=1500)
    can.send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    sim.run_for(ms=ms)
    return can.last(PIT_FSM), can.last(BOOT_DIAG)


@pytest.fixture(scope="module")
def latched(firmware):
    """An AMS that latched Error on a transient over-voltage, now gone."""
    with Sim(REPO / "systems" / "ams.yaml", {"ams": firmware("ams")}) as sim:
        status, _ = _arm_and_read(sim)
        assert status.data[0] == START, "AMS not healthy before the fault"
        sim.monitor("sysbus.spi1.isospi.cells2 SetCell 3 4300", board="ams")
        sim.run_for(ms=1500)
        sim.monitor("sysbus.spi1.isospi.cells2 SetCell 3 3700", board="ams")
        status = sim.can("can_acu").last(PIT_FSM)
        assert (status.data[0], status.data[6]) == (ERROR, CELL_OVER_VOLTAGE)
        yield sim


def test_error_latch_survives_a_warm_reset(latched):
    """F-076 (flight build): the boot trigger's warm reset keeps BKP1R, so
    the AMS comes back in Error though the cell is healthy again."""
    latched.can("can_acu").send(*BOOT_TRIGGER)
    status, boot = _arm_and_read(latched)
    assert status.data[0] == ERROR, f"AMS state {status.data[0]} after a warm reset"
    assert int.from_bytes(boot.data[0:4], "little") == JUMP, "jump reason lost across the reset"


def test_a_power_cycle_clears_the_latch(latched):
    """No VBAT: the power cut wipes BKP1R and BKP2R, and the AMS boots clean."""
    latched.power_cycle("ams")
    status, boot = _arm_and_read(latched)
    assert (status.data[0], status.data[6]) == (START, 0), f"0x6C0 {status.data.hex()}"
    assert int.from_bytes(boot.data[0:4], "little") == 0, "jump reason survived a power cut"
