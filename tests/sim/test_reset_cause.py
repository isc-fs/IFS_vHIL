"""Reset causes as each firmware reports them (#23), with RCC_RSR modelled per
RM0468 Rev 3 Table 52 (models/renode/VhilResetFlags.cs).

Firmware facts:
  Both report byte 5 (b0-b2 on the ECU): 1 PowerOn, 2 Pin, 3 Software, 4 IWDG.
    AMS: 0x6CA, decoded IWDG > WWDG > LowPower > Software > PowerOn > Pin
      (IFS08-CE-AMS fw_health.hpp:30-40).
    ECU: 0x704, always on, decoded LowPower > WWDG > IWDG > Software > Pin >
      PowerOn (IFS08-CE-ECU reset_cause.cpp:16-21).
  RM0468 Table 52: a power-on sets PORRSTF *and* PINRSTF, so the ECU's order
    reports a power-on as Pin.
  Software reset: AIRCR.SYSRESETREQ written directly, which is what
    NVIC_SystemReset does. (Not the AMS's 0x002 boot trigger: behind the
    bootloader it parks the board in CAN-listen mode.)
  Every board boots through the CAN bootloader, which reads RCC_RSR and
    clears it before it jumps (stm32-can-bootloader v1.7.0 bl_health.c:59-63):
    on the car neither app sees a reset flag, so every case here fails.
  IWDG: unlock (KR 0x5555) and set RLR = 1: it expires between refreshes.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

POWER_ON, PIN, SOFTWARE, IWDG = 1, 2, 3, 4
BOARDS = {"ams": ("ams.yaml", 0x6CA), "ecu": ("ecu.yaml", 0x704)}
# Every case: the bootloader clears the flags first. Behind that, the ECU's
# power-on case also hits IFS08-CE-ECU#245 (reset_cause.cpp checks PINRSTF
# before PORRSTF, and a power-on sets both, RM0468 Table 52 row 1: Pin).
pytestmark = pytest.mark.xfail(strict=True, reason=(
    "stm32-can-bootloader v1.7.0 bl_health.c:59-63 clears RCC_RSR before the "
    "jump: the app reads no reset flag and reports cause 0 (Unknown)"))


def _software_reset(sim, board):
    # SYSRESETREQ (AIRCR, VECTKEY 0x05FA). Not the AMS's 0x002 boot trigger:
    # behind the bootloader that parks it in CAN-listen mode (BKP0R =
    # BL_BOOT_REQ_MAGIC), and the app only returns when a host jumps to it.
    sim.monitor("sysbus WriteDoubleWord 0xE000ED0C 0x05FA0004", board=board)


def _iwdg_expire(sim, board):
    for address, value in ((0x58004800, 0x5555), (0x58004808, 0x1)):
        sim.monitor(f"sysbus WriteDoubleWord {address:#x} {value:#x}", board=board)


RESETS = {
    "power-on": lambda sim, board: sim.power_cycle(board),
    "pin": lambda sim, board: sim.monitor(f"vhil_reset_{board} PinReset", board=board),
    "software": _software_reset,
    "iwdg": _iwdg_expire,
}
EXPECTED = {"power-on": POWER_ON, "pin": PIN, "software": SOFTWARE, "iwdg": IWDG}


def _cause(sim, board):
    frame = sim.can("can_acu").last(BOARDS[board][1])
    assert frame is not None, "no health frame"
    return frame.data[5] & 0x7


@pytest.mark.parametrize("board, reset", [(b, r) for b in BOARDS for r in RESETS])
def test_each_reset_reports_its_cause(images, board, reset):
    system, _ = BOARDS[board]
    with Sim(REPO / "systems" / system, images(system.removesuffix(".yaml"))) as sim:
        sim.wait_for_app()
        sim.run_for(ms=2500)
        RESETS[reset](sim, board)
        sim.run_for(ms=10)                      # the reset happens (IWDG: ~1 LSI tick)
        sim.wait_for_app()                      # through the bootloader again
        sim.run_for(ms=2500)
        assert _cause(sim, board) == EXPECTED[reset]


@pytest.mark.parametrize("board", ["ams", "ecu"])
def test_first_boot_is_a_power_on(images, board):
    system, _ = BOARDS[board]
    with Sim(REPO / "systems" / system, images(system.removesuffix(".yaml"))) as sim:
        sim.wait_for_app()
        sim.run_for(ms=2500)
        assert _cause(sim, board) == POWER_ON
