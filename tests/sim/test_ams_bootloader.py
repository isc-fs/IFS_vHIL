"""ams-bootloader (#36): the AMS reflashed over CAN through the real bootloader,
in virtual time, by a host speaking its protocol (vhil/can_bootloader.py).

Bootloader facts (isc-fs/stm32-can-bootloader v1.7.0):
  - serves its protocol on FDCAN1, FDCAN2 and FDCAN3 at once
    (ARCHITECTURE.md "Multi-bus FDCAN"); the AMS's FDCAN1 is the ACU bus;
  - node ID from NVM, seeded at provisioning (bl_node_id.c): the AMS is 0x2;
  - a valid app arms a 2000 ms auto-jump window (main.c:684-688); any valid
    frame for this node, a broadcast DISCOVER included, cancels it
    (main.c:810-816);
  - BKP0R = BL_BOOT_REQ_MAGIC 0xB00710AD: stay, one-shot, cleared at boot
    (main.c:1134-1139); FLASH_* need a CONNECTed session (bl_proto.c:853-860);
    only sectors 1..6 may be erased or written (bl_flash.c:31-59);
  - FLASH_VERIFY CRC-32s [0x08020000, +size) and stamps the metadata record
    {0xB007C0DE, size, crc, base, version} at 0x080FFFE0 (bl_proto.c:978-1030,
    bl_flash.c:265-282); JUMP after a write this boot goes through a reset
    with BKP0R = BL_BOOT_APP_MAGIC, consumed at boot (bl_proto.c:724-742,
    main.c:1160-1170).
AMS app facts (IFS08-CE-AMS):
  - boot trigger 0x002 B0 07 AD 11 on the ACU bus, honoured in Start and Error
    (bootloader.hpp:61-74, ams_config.hpp:968-970); it stamps JumpReason
    CanTrigger 0x4A554D50 into BKP2R first (bootloader.cpp:24-27);
  - 0x4A0 byte 0 = FSM state (ams_status.def), every TelemetryPeriodMs = 500
    (ams_config.hpp:260); Start = 0, Error = 5;
  - pit diag armed by 0x7F0 DE AD BE EF (ams_config.hpp:605, 642): 0x6C4[0..3]
    = BKP2R (acu_can_task.cpp:346-352), 0x6C6 = fw major, minor, patch,
    git hash[0..3], BL node id, all from the firmware-info record
    (pit_fw_id.def, firmware_info.cpp:100-112);
  - card detect PE3 (MICROSD_DET, no internal pull: main.c:716-720) reads high
    with no card (the MainLite's pull-up); the BSP checks it before touching
    SDMMC (fatfs_platform.c:21-31, bsp_driver_sd.c:46); with no card the
    logger's g_log_state is 1 (sd_logger_task.cpp:117, 660-669).
"""
import struct
import subprocess
from pathlib import Path

import pytest

from vhil import can_bootloader as cb
from vhil.can_bootloader import CanBootloader, FwInfo
from vhil.sim import Sim
from vhil.system import REPO

NODE = 0x2
AUTO_JUMP_MS = 2000
TELEM_MS = 500
STATUS, PIT_ARM, BOOT_DIAG, FW_ID = 0x4A0, 0x7F0, 0x6C4, 0x6C6
TRIGGER = (0x002, bytes.fromhex("B007AD11"))
START, ERROR = 0, 5
JUMP_REASON_CAN_TRIGGER = 0x4A554D50
BKP0R, BKP2R = 0x58004050, 0x58004058
META = 0x080FFFE0
LOG_NO_CARD = 1                       # g_log_state (sd_logger_task.cpp:117, 668)
# Globals in anonymous namespaces: their linker names are mangled.
LOG_STATE = "_ZN12_GLOBAL__N_111g_log_stateE"                       # sd_logger_task.cpp:117
RX_DROPPED_UNKNOWN = "_ZN12_GLOBAL__N_124g_acu_rx_dropped_unknownE"  # acu_can_task.cpp:119


def _flat(elf: Path) -> bytes:
    """The app image as the flasher sends it: the ELF's flat binary."""
    binary = elf.with_suffix(".bin")
    if not binary.exists():
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(binary)], check=True)
    return binary.read_bytes()


def _start(firmware, card=True):
    sim = Sim(REPO / "systems" / "ams-bl.yaml",
              {"ams": firmware("ams"), "ams.bootloader": firmware("can-bootloader")})
    if not card:
        del sim.system.devices["sd"]
    sim.start()
    if not card:
        # The empty slot: the MainLite's pull-up holds card detect high from
        # power-on, through every reset.
        sim.io("ams").set_input("sysbus.gpioPortE", 3, True)
    return sim


@pytest.fixture
def ams(firmware):
    sim = _start(firmware)
    try:
        yield sim
    finally:
        sim.stop()


@pytest.fixture
def app(firmware):
    return _flat(firmware("ams"))


def _word(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}", board="ams").strip(), 16)


def _in_bootloader(sim):
    return int(sim.monitor("cpu PC", board="ams").strip(), 16) < cb.APP_BASE


def _statuses(sim, since_us):
    return sim.can("can_acu").frames([STATUS], since_us)


def _boot_app(sim):
    """Cold boot: through the auto-jump window into the app, running."""
    t = sim.run_for(ms=AUTO_JUMP_MS + 2 * TELEM_MS)
    assert _statuses(sim, t - TELEM_MS * 1000 - 1), "the AMS app is not running"


def _trigger_into_bootloader(sim):
    """0x002 from the running app: a warm reset that parks the bootloader."""
    sim.can("can_acu").send(*TRIGGER)
    t = sim.run_for(ms=300)
    assert _in_bootloader(sim), "the trigger did not reset into the bootloader"
    return t


def _pit(sim):
    """Arm the pit stream and return the next (0x6C4, 0x6C6)."""
    can = sim.can("can_acu")
    t = sim.run_for(ms=10)
    can.send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    sim.run_for(ms=1200)
    return can.last(BOOT_DIAG, t), can.last(FW_ID, t)


def _first_status_after(sim, t_us, within_ms):
    sim.run_for(ms=within_ms)
    frames = _statuses(sim, t_us)
    assert frames, f"no 0x4A0 within {within_ms} ms: the app did not come up"
    return frames[0]


# -- discover / boot ------------------------------------------------------------

def test_a002_the_bootloader_answers_discover_as_node_2(ams):
    """A-002: inside the window, one node answers DISCOVER: 0x2, protocol 0.2.
    The broadcast is a valid frame for the node, so the board stays."""
    ams.run_for(ms=300)
    bl = CanBootloader(ams, "can_acu", NODE)
    found = bl.discover()
    assert [(d.node, d.major, d.minor) for d in found] == [(NODE, cb.PROTO_MAJOR, cb.PROTO_MINOR)]
    ams.run_for(ms=AUTO_JUMP_MS + 1000)
    assert not _statuses(ams, 0) and _in_bootloader(ams), "auto-jumped after a DISCOVER"


def test_d050_a_cold_boot_reaches_the_app_after_the_window(ams):
    """D-050: power-on -> bootloader -> app, the first 0x4A0 right after the
    2 s window. (IFS_HIL's plan says "under 2 s", which the window itself
    rules out; its test allows 3 s.)"""
    ams.run_for(ms=AUTO_JUMP_MS + 2 * TELEM_MS)
    first = _statuses(ams, 0)[0]
    assert AUTO_JUMP_MS <= first.t_ms <= AUTO_JUMP_MS + TELEM_MS + 100, \
        f"first 0x4A0 at {first.t_ms:.0f} ms"
    assert first.data[0] == START


# -- the protocol's guards, on a parked bootloader --------------------------------

@pytest.fixture
def parked(ams):
    """The bootloader in its window, kept there by a DISCOVER."""
    ams.run_for(ms=300)
    bl = CanBootloader(ams, "can_acu", NODE)
    assert bl.discover()
    return bl


def test_flash_commands_need_a_session(parked):
    for opcode, args in [(cb.FLASH_ERASE, struct.pack("<II", cb.APP_BASE, cb.SECTOR_SIZE)),
                         (cb.FLASH_WRITE, struct.pack("<I", cb.APP_BASE) + bytes(32)),
                         (cb.FLASH_VERIFY, bytes(12))]:
        r = parked.request(opcode, args)
        assert (r.msg_type, r.code) == (cb.MSG_NACK, cb.NACK_BAD_SESSION), f"opcode {opcode:#x}: {r}"


def test_the_bootloader_refuses_to_erase_itself_or_its_nvm(parked, firmware):
    """Even a host that asks (this client never would: check_app_range): the
    bootloader NACKs sector 0 and sector 7 as protected, and both survive."""
    sim = parked.sim
    sector0 = sim.monitor(f"sysbus ReadBytes {cb.FLASH_BASE:#x} 64", board="ams")
    meta = [_word(sim, META + 4 * i) for i in range(8)]
    parked.connect()
    for start in (cb.FLASH_BASE, 0x080E0000):
        r = parked.request(cb.FLASH_ERASE, struct.pack("<II", start, cb.SECTOR_SIZE))
        assert (r.msg_type, r.code) == (cb.MSG_NACK, cb.NACK_PROTECTED_ADDR), f"{start:#x}: {r}"
    r = parked.request(cb.FLASH_WRITE, struct.pack("<I", cb.FLASH_BASE) + bytes(32))
    assert (r.msg_type, r.code) == (cb.MSG_NACK, cb.NACK_PROTECTED_ADDR)
    assert sim.monitor(f"sysbus ReadBytes {cb.FLASH_BASE:#x} 64", board="ams") == sector0
    assert [_word(sim, META + 4 * i) for i in range(8)] == meta


def test_a_verify_with_the_wrong_crc_commits_nothing(parked, app):
    sim = parked.sim
    meta = [_word(sim, META + 4 * i) for i in range(8)]
    parked.connect()
    r = parked.request(cb.FLASH_VERIFY, struct.pack("<III", cb.crc32(app) ^ 1, len(app), 0))
    assert (r.msg_type, r.code) == (cb.MSG_NACK, cb.NACK_CRC_MISMATCH)
    assert [_word(sim, META + 4 * i) for i in range(8)] == meta


def test_a005_firmware_info_is_the_images_record(parked, app):
    """GET_FW_INFO returns the 64 bytes the image carries at +0x400, the
    product IFS08-CE-AMS and node 0x2 in reserved[0] (firmware_info.cpp)."""
    info = parked.fw_info()
    assert info == FwInfo.from_image(app)
    assert (info.magic, info.product, info.reserved[0]) == (cb.FWINFO_MAGIC, "IFS08-CE-AMS", NODE)


# -- trigger, flash, verify, jump -------------------------------------------------

def test_a003_trigger_flash_verify_jump(ams, app):
    """A-003 / F-071 / D-052 / F-075: from the running app, the trigger parks
    the bootloader; the image is erased, written, read back and verified over
    CAN, the record stamped; JUMP reboots into the new app, which reports the
    CAN-trigger jump reason and exactly the flashed image's identity.

    Before the trigger the flash is made to hold a different image (its last
    word changed), so only a real erase + write makes it valid again."""
    _boot_app(ams)
    last = cb.APP_BASE + len(app) - 4
    ams.monitor(f"sysbus WriteDoubleWord {last:#x} {_word(ams, last) ^ 0xFFFFFFFF:#x}", board="ams")
    _trigger_into_bootloader(ams)
    assert _word(ams, BKP0R) == 0, "BL_BOOT_REQ_MAGIC not consumed (one-shot)"
    assert _word(ams, BKP2R) == JUMP_REASON_CAN_TRIGGER

    bl = CanBootloader(ams, "can_acu", NODE)
    assert [d.node for d in bl.discover()] == [NODE]
    info = FwInfo.from_image(app)
    report = bl.flash(app, jump=True)
    t_jump = ams.now_us()
    assert report.sectors == cb.sectors_of(cb.APP_BASE, len(app))
    assert _word(ams, last) == struct.unpack_from("<I", app, len(app) - 4)[0], "image not rewritten"
    assert [_word(ams, META + 4 * i) for i in range(5)] == \
        [0xB007C0DE, len(app), cb.crc32(app), cb.APP_BASE, info.packed_version]

    first = _first_status_after(ams, t_jump, within_ms=AUTO_JUMP_MS)
    assert first.data[0] == START
    assert (first.t_us - t_jump) / 1000 < AUTO_JUMP_MS / 2, \
        "app took a whole auto-jump window: the post-write JUMP did not boot it"
    assert _word(ams, BKP0R) == 0, "BL_BOOT_APP_MAGIC not consumed"
    boot, fwid = _pit(ams)
    assert int.from_bytes(boot.data[0:4], "little") == JUMP_REASON_CAN_TRIGGER, "D-052"
    assert fwid.data == bytes([info.major, info.minor, info.patch]) + info.git_hash[:4] + \
        bytes([info.reserved[0]]), "F-075: 0x6C6 is not the flashed image's identity"


def test_f074_flash_under_bus_load(ams, app):
    """F-074: the same flash with the ACU bus never idle. Filler as IFS_HIL's
    (200 frames/s, test_block_f_flash_endurance.py:84-112), plus IDs aimed
    at the bootloader's filter: 0x502 shares node 2's low five bits (the
    #154 alias: the filter must match all 11), and an extended 0x002."""
    _boot_app(ams)
    _trigger_into_bootloader(ams)
    can = ams.can("can_acu")
    can.send_periodic("fill", 0x5A5, bytes.fromhex("0011223344556677"), 10)
    can.send_periodic("alias", 0x502, bytes.fromhex("1111111111111111"), 10)
    can.send_periodic("ext", 0x002, bytes.fromhex("B007AD11"), 20, extended=True)
    bl = CanBootloader(ams, "can_acu", NODE)
    bl.flash(app, jump=True)
    t_jump = ams.now_us()
    # The filler runs on into the new app, which takes every standard frame
    # and rejects extended ones in hardware (app_init_task.cpp:96-107): the
    # extended 0x002 must not re-trigger it, and the app's count of unknown
    # IDs (acu_can_task.cpp:577-580) proves the load was on the bus.
    first = _first_status_after(ams, t_jump, within_ms=AUTO_JUMP_MS)
    assert first.data[0] in (START, ERROR)
    t = ams.run_for(ms=1000)
    for key in ("fill", "alias", "ext"):
        can.stop_periodic(key)
    assert _statuses(ams, t - 600_000) and not _in_bootloader(ams), "the app did not stay up"
    assert ams.read_symbol("ams", RX_DROPPED_UNKNOWN, 4) >= 100, "no filler on the bus"


def test_m05_flash_without_jump_then_a_cold_boot(ams, app):
    """M-05 / F-072: flash without JUMP, DISCONNECT: the board stays in the
    bootloader (the trigger disarmed the auto-jump); a power cycle then boots
    the new image through the window, with no jump reason (BKP wiped)."""
    _boot_app(ams)
    _trigger_into_bootloader(ams)
    bl = CanBootloader(ams, "can_acu", NODE)
    bl.flash(app, jump=False)
    t = ams.now_us()
    ams.run_for(ms=AUTO_JUMP_MS + 1000)
    assert not _statuses(ams, t) and _in_bootloader(ams), "left the bootloader without a JUMP"
    ams.power_cycle("ams")
    t = ams.now_us()
    first = _first_status_after(ams, t, within_ms=AUTO_JUMP_MS + 2 * TELEM_MS)
    assert AUTO_JUMP_MS <= (first.t_us - t) / 1000 <= AUTO_JUMP_MS + TELEM_MS + 100
    boot, _ = _pit(ams)
    assert int.from_bytes(boot.data[0:4], "little") == 0


def test_s142_with_no_card_the_trigger_still_reaches_the_bootloader(firmware, app):
    """S-142: no card in the slot (detect high, no card model): the AMS boots,
    honours the trigger, and is reflashed."""
    sim = _start(firmware, card=False)
    try:
        _boot_app(sim)
        assert sim.can("can_acu").last(STATUS).data[0] == START
        assert sim.read_symbol("ams", LOG_STATE) == LOG_NO_CARD, "the AMS saw a card"
        _trigger_into_bootloader(sim)
        bl = CanBootloader(sim, "can_acu", NODE)
        assert [d.node for d in bl.discover()] == [NODE]
        bl.flash(app, jump=True)
        t = sim.now_us()
        assert _first_status_after(sim, t, within_ms=AUTO_JUMP_MS).data[0] == START
        sim.run_for(ms=1000)
        assert sim.read_symbol("ams", LOG_STATE) == LOG_NO_CARD, "the reflashed AMS saw a card"
    finally:
        sim.stop()
