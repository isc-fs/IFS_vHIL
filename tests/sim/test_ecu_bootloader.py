"""ecu-bootloader (#49): the ECU's road back into the CAN bootloader and its
reflash, in virtual time, by a host speaking the bootloader's protocol
(vhil/can_bootloader.py).

Bootloader facts (isc-fs/stm32-can-bootloader v1.7.0): as in
test_ams_bootloader.py; the ECU is node 0x1. The bootloader drives FDCAN1, 2
and 3 at once and answers on the bus a request came in on (ARCHITECTURE.md
"Multi-bus FDCAN", bl_fdcan.c).
ECU app facts (IFS08-CE-ECU, ecu@dev):
  - boot trigger 0x002 B0 07 AD 12 (dlc 4) on the ACU bus, FDCAN2
    (ecu_config.hpp:656-658, bootloader.hpp:78-85);
  - honoured only out of the drive ladder: state < R2dDelay, or AmsError
    (bootloader.hpp:72-74); a refusal counts g_boot_trigger_refused
    (can_rx_task.cpp:66-75);
  - request_reboot stamps BKP2R = JumpReason::ManualRequest (1), then BKP0R =
    0xB00710AD, then NVIC_SystemReset (bootloader.cpp:12-27, bootloader.hpp:39);
  - firmware-info at +0x400: magic 0xF14F1B00, product IFS08-CE-ECU,
    reserved[0] = EcuNodeId 1 (firmware_info.cpp:71-85);
  - 0x100 every 10 ms from boot on the ACU bus.
Start-up ladder stimulus as in test_ecu_startup.py (control.cpp:150-186).
"""
import struct
import subprocess
from pathlib import Path

import pytest

from vhil import can_bootloader as cb
from vhil.can_bootloader import CanBootloader, FwInfo
from vhil.sim import Sim
from vhil.system import REPO

NODE = 0x1
AUTO_JUMP_MS = 2000
HEARTBEAT = 0x100
TRIGGER = (0x002, bytes.fromhex("B007AD12"))
MANUAL_REQUEST = 1
BKP0R, BKP2R = 0x58004050, 0x58004058
BOOT_REQ_MAGIC = 0xB00710AD
META = 0x080FFFE0
WAIT_VDC, R2D_DELAY = 0, 3
BRAKE_FIRM_V = 1500 * 3.3 / 4095          # past BrakeArmRaw (750 counts)


def _flat(elf: Path) -> bytes:
    binary = elf.with_suffix(".bin")
    if not binary.exists():
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(binary)], check=True)
    return binary.read_bytes()


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        yield sim


@pytest.fixture
def app(firmware):
    return _flat(firmware("ecu"))


def _word(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}", board="ecu").strip(), 16)


def _in_bootloader(sim):
    return int(sim.monitor("cpu PC", board="ecu").strip(), 16) < cb.APP_BASE


def _beats(sim, since_us):
    return sim.can("can_acu").count([HEARTBEAT], since_us)


def _boot_app(sim):
    t = sim.run_for(ms=AUTO_JUMP_MS + 500)
    assert _beats(sim, t - 200_000) >= 15, "the ECU app is not running"


def _state(sim):
    return sim.read_symbol("ecu", "g_last_ctrl_state")


# -- the bootloader, parked in its window -----------------------------------------

@pytest.fixture
def parked(ecu):
    ecu.run_for(ms=300)
    bl = CanBootloader(ecu, "can_acu", NODE)
    assert bl.discover()
    return bl


def test_a005_firmware_info_is_the_images_record(parked, app):
    """A-005, bootloader side: GET_FW_INFO returns the record the image carries."""
    info = parked.fw_info()
    assert info == FwInfo.from_image(app)
    assert (info.magic, info.product, info.reserved[0]) == (cb.FWINFO_MAGIC, "IFS08-CE-ECU", NODE)
    assert info.record_version >> 16 >= 1


@pytest.mark.parametrize("bus", ["can_inv", "can_dash", "can_acu"])
def test_the_bootloader_answers_on_every_bus(ecu, bus):
    """One image serves FDCAN1/2/3; the reply goes back out the asking bus."""
    ecu.run_for(ms=300)
    found = CanBootloader(ecu, bus, NODE).discover()
    assert [(d.node, d.major, d.minor) for d in found] == [(NODE, cb.PROTO_MAJOR, cb.PROTO_MINOR)]
    others = [b for b in ("can_inv", "can_dash", "can_acu") if b != bus]
    for other in others:
        assert not ecu.can(other).frames([cb.rx_id(NODE)]), f"reply leaked onto {other}"


# -- 0x002: accepted and refused ----------------------------------------------------

def test_a002_the_trigger_parks_the_bootloader(ecu):
    """A-002/A-006: in WaitInvVdcConfig the trigger resets into the bootloader,
    which consumes BKP0R (one-shot), keeps BKP2R = ManualRequest, stays (no
    auto-jump, no 0x100) and answers DISCOVER as node 0x1."""
    _boot_app(ecu)
    assert _state(ecu) == WAIT_VDC
    ecu.can("can_acu").send(*TRIGGER)
    t = ecu.run_for(ms=300)
    assert _in_bootloader(ecu), "the trigger did not reset into the bootloader"
    assert (_word(ecu, BKP0R), _word(ecu, BKP2R)) == (0, MANUAL_REQUEST)
    ecu.run_for(ms=AUTO_JUMP_MS + 1000)
    assert _beats(ecu, t) == 0 and _in_bootloader(ecu), "auto-jumped after the trigger"
    assert [d.node for d in CanBootloader(ecu, "can_acu", NODE).discover()] == [NODE]


@pytest.mark.parametrize("can_id, data", [
    (0x002, "B007AD11"),          # the AMS's payload
    (0x002, "B007AD1200"),        # dlc 5
    (0x003, "B007AD12"),          # wrong ID
], ids=["ams-payload", "dlc", "id"])
def test_a_near_miss_trigger_is_ignored(ecu, can_id, data):
    _boot_app(ecu)
    ecu.can("can_acu").send(can_id, bytes.fromhex(data))
    t = ecu.run_for(ms=500)
    assert _beats(ecu, t - 300_000) >= 25 and not _in_bootloader(ecu)
    assert _word(ecu, BKP0R) != BOOT_REQ_MAGIC


def test_the_trigger_is_refused_in_the_drive_ladder(ecu):
    """In R2dDelay the trigger is counted, not honoured: the heartbeat the
    AMS watches never stops (bootloader.hpp:14-25)."""
    ecu.can("can_inv").send_periodic("vdc", 0x466, bytes([0, 0, 0x5E, 0x01, 0, 0]), 10)
    ecu.can("can_acu").send_periodic("ams", 0x020, bytes([1]), 10)
    _boot_app(ecu)
    io = ecu.io("ecu")
    io.set_voltage("PF7", BRAKE_FIRM_V)
    io.set_input("sysbus.gpioPortB", 5, True)
    ecu.run_for(ms=200)
    io.set_input("sysbus.gpioPortB", 5, False)
    assert _state(ecu) == R2D_DELAY
    ecu.can("can_acu").send(*TRIGGER)
    t = ecu.run_for(ms=500)
    beats = ecu.can("can_acu").frames([HEARTBEAT], t - 500_000)
    assert len(beats) >= 45 and max(b.t_us - a.t_us for a, b in zip(beats, beats[1:])) <= 20_000, \
        "the heartbeat stopped: the trigger was honoured mid-ladder"
    assert not _in_bootloader(ecu) and _word(ecu, BKP0R) != BOOT_REQ_MAGIC
    assert ecu.read_symbol("ecu", "g_boot_trigger_refused", 4) == 1


# -- flash + verify + jump ------------------------------------------------------------

def test_a003_flash_verify_jump(ecu, app):
    """A-003 / J-002: trigger, erase + write + read back + verify the image,
    JUMP; the new app streams 0x100 at once (BL_BOOT_APP_MAGIC reset, not the
    2 s window). BKP2R keeps ManualRequest through the warm resets; a power
    cycle (no VBAT) wipes it and boots the image through the window again.

    The flash is first made to hold a different image (its last word changed),
    so only a real erase + write makes it valid again."""
    _boot_app(ecu)
    last = cb.APP_BASE + len(app) - 4
    ecu.monitor(f"sysbus WriteDoubleWord {last:#x} {_word(ecu, last) ^ 0xFFFFFFFF:#x}", board="ecu")
    ecu.can("can_acu").send(*TRIGGER)
    ecu.run_for(ms=300)
    assert _in_bootloader(ecu)

    bl = CanBootloader(ecu, "can_acu", NODE)
    info = FwInfo.from_image(app)
    report = bl.flash(app, jump=True)
    t_jump = ecu.now_us()
    assert report.sectors == cb.sectors_of(cb.APP_BASE, len(app))
    assert _word(ecu, last) == struct.unpack_from("<I", app, len(app) - 4)[0], "image not rewritten"
    assert [_word(ecu, META + 4 * i) for i in range(5)] == \
        [0xB007C0DE, len(app), cb.crc32(app), cb.APP_BASE, info.packed_version]

    ecu.run_for(ms=AUTO_JUMP_MS // 2)
    beats = ecu.can("can_acu").frames([HEARTBEAT], t_jump)
    assert beats, "the app did not come up within 1 s of the JUMP"
    assert (_word(ecu, BKP0R), _word(ecu, BKP2R)) == (0, MANUAL_REQUEST)

    ecu.power_cycle("ecu")
    t = ecu.now_us()
    ecu.run_for(ms=AUTO_JUMP_MS + 500)
    beats = ecu.can("can_acu").frames([HEARTBEAT], t)
    assert beats and AUTO_JUMP_MS * 0.9 <= (beats[0].t_us - t) / 1000 <= AUTO_JUMP_MS + 600
    assert _word(ecu, BKP2R) == 0
