"""Boards provisioned with the real CAN bootloader (#4): the boot chain as the
car runs it, from a provisioned flash image (vhil/flash_image.py).

Bootloader facts (isc-fs/stm32-can-bootloader v1.7.0, ARCHITECTURE.md
"Boot flow"):
  - the board resets into the bootloader; with a valid application metadata
    record (magic, size, CRC-32 over the image, base) it arms a 2 s
    auto-jump window, then jumps to the app at 0x08020000;
  - an invalid record means no jump: it stays in CAN-listen mode;
  - BKP0R = BL_BOOT_REQ_MAGIC (the app's 0x002 boot trigger) means stay in
    the bootloader, no auto-jump;
  - the node ID comes from NVM, seeded on first boot from the provisioning
    FLASHWORD at 0x080FFFC0 (bl_provision.c), cached in s_node_id.
App facts: the ECU sends 0x100 every 10 ms from boot; the AMS's boot trigger
is 0x002 B0 07 AD 11 on the ACU bus, honoured in Start (bootloader.hpp).
"""
import subprocess

import pytest

from vhil.sim import Sim
from vhil.system import REPO

AUTO_JUMP_MS = 2000
META_CRC = 0x080FFFE8                 # metadata word 2: image_crc32


def _bl_firmware(firmware, board):
    return {board: firmware(board), f"{board}.bootloader": firmware("can-bootloader")}


@pytest.fixture
def ecu_bl(firmware):
    with Sim(REPO / "systems" / "ecu-bl.yaml", _bl_firmware(firmware, "ecu")) as sim:
        yield sim


@pytest.fixture
def ams_bl(firmware):
    with Sim(REPO / "systems" / "ams-bl.yaml", _bl_firmware(firmware, "ams")) as sim:
        yield sim


def test_the_board_boots_through_the_bootloader(ecu_bl):
    """Bootloader first, its auto-jump window, then the application."""
    ecu_bl.run_for(ms=500)
    pc = ecu_bl.monitor("sysbus FindSymbolAt `cpu PC`", board="ecu")
    assert int(ecu_bl.monitor("cpu PC", board="ecu").strip(), 16) < 0x08020000, \
        f"not in the bootloader at 500 ms: {pc}"
    ecu_bl.run_for(ms=2500)
    beats = ecu_bl.can("can_acu").frames([0x100])
    assert beats, "the application never started"
    first_ms = beats[0].t_ms
    assert AUTO_JUMP_MS * 0.9 <= first_ms <= AUTO_JUMP_MS + 600, \
        f"first 0x100 at {first_ms:.0f} ms, expected just after the {AUTO_JUMP_MS} ms window"


def test_a_bad_image_is_never_launched(ecu_bl):
    """A metadata CRC that doesn't match the image: no jump, ever."""
    ecu_bl.monitor(f"sysbus WriteDoubleWord {META_CRC:#x} 0xDEADBEEF", board="ecu")
    ecu_bl.run_for(ms=5000)
    assert ecu_bl.can("can_acu").count([0x100]) == 0, "launched an image with a bad CRC"
    assert int(ecu_bl.monitor("cpu PC", board="ecu").strip(), 16) < 0x08020000


def _local_symbol(elf, name):
    """Address of a file-local symbol, which Renode's lookup doesn't index."""
    out = subprocess.run(["arm-none-eabi-nm", str(elf)], check=True, capture_output=True,
                         text=True).stdout
    return next(int(line.split()[0], 16) for line in out.splitlines() if line.endswith(f" {name}"))


def test_the_seeded_node_id_is_used(ams_bl, firmware):
    """The provisioning seed's node ID (0x2) reaches the bootloader through NVM
    (the compile-time default would be BL_NODE_ID)."""
    address = _local_symbol(firmware("can-bootloader"), "s_node_id")
    ams_bl.run_for(ms=1000)                          # inside the auto-jump window
    value = int(ams_bl.monitor(f"sysbus ReadByte {address:#x}", board="ams").strip(), 16)
    assert value == 2


def test_the_boot_trigger_keeps_the_board_in_the_bootloader(ams_bl):
    """0x002 from the app: warm reset with BKP0R set, the bootloader stays;
    a power cycle (no VBAT: BKP0R wiped) boots the app again."""
    can = ams_bl.can("can_acu")
    t = ams_bl.run_for(ms=AUTO_JUMP_MS + 2500)
    assert can.count([0x4A0], since_us=t - 1_000_000) >= 1, "AMS app not running"
    can.send(0x002, bytes.fromhex("B007AD11"))
    t = ams_bl.run_for(ms=5000)
    assert can.count([0x4A0], since_us=t - 4_000_000) == 0, "the app came back: no stay-in-BL"
    assert int(ams_bl.monitor("cpu PC", board="ams").strip(), 16) < 0x08020000
    ams_bl.power_cycle("ams")
    t = ams_bl.run_for(ms=AUTO_JUMP_MS + 2500)
    assert can.count([0x4A0], since_us=t - 1_000_000) >= 1, "app not back after a power cycle"
