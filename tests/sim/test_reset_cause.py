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
  Software reset: the AMS's 0x002 B0 07 AD 11 boot trigger (NVIC_SystemReset);
    on the ECU, AIRCR.SYSRESETREQ written directly, which is what
    NVIC_SystemReset does.
  IWDG: unlock (KR 0x5555) and set RLR = 1: it expires between refreshes.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

POWER_ON, PIN, SOFTWARE, IWDG = 1, 2, 3, 4
BOARDS = {"ams": ("ams.yaml", 0x6CA), "ecu": ("ecu.yaml", 0x704)}
ECU_POR_AS_PIN = pytest.mark.xfail(strict=True, reason=(
    "ECU firmware: reset_cause.cpp checks PINRSTF before PORRSTF, and a power-on "
    "sets both (RM0468 Table 52 row 1), so it reports Pin"))


def _software_reset(sim, board):
    if board == "ams":
        sim.can("can_acu").send(0x002, bytes.fromhex("B007AD11"))
    else:
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


@pytest.mark.parametrize("board, reset", [
    pytest.param(b, r, marks=ECU_POR_AS_PIN if (b, r) == ("ecu", "power-on") else ())
    for b in BOARDS for r in RESETS
])
def test_each_reset_reports_its_cause(firmware, board, reset):
    system, _ = BOARDS[board]
    with Sim(REPO / "systems" / system, {board: firmware(board)}) as sim:
        sim.run_for(ms=2500)
        RESETS[reset](sim, board)
        sim.run_for(ms=2500)
        assert _cause(sim, board) == EXPECTED[reset]


@pytest.mark.parametrize("board", [
    "ams", pytest.param("ecu", marks=ECU_POR_AS_PIN)])
def test_first_boot_is_a_power_on(firmware, board):
    system, _ = BOARDS[board]
    with Sim(REPO / "systems" / system, {board: firmware(board)}) as sim:
        sim.run_for(ms=2500)
        assert _cause(sim, board) == POWER_ON
