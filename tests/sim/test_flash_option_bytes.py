"""flash-option-bytes (#67): the H733's option bytes and sector write
protection (models/renode/Stm32H7Flash.cs), driven by the real CAN
bootloader's one-way sector-0 WRP latch and by a debugger.

Silicon (RM0468 Rev 3):
  - option bytes are non-volatile: the _CUR registers load from them at power-on
    and after an option change; the _PRG registers reload from them at reset
    (§4.4.2, §4.4.3, §4.9.9). Factory values: Table 18 (§4.4.4);
  - change sequence: OPTKEYR 0x08192A3B, 0x4C5D6E7F clears OPTCR.OPTLOCK;
    write the _PRG registers; set OPTSTART; wait for OPT_BUSY to clear; a wrong
    key keeps OPTLOCK set until reset (§4.4.3, §4.5.1, §4.9.7). No reset
    follows: the flash reloads the option registers itself (§4.4.2 item 3);
  - a sector whose WRPSn bit is 0 can be neither erased nor programmed: the
    operation is rejected, WRPERR (SR1 bit 17) is set and nothing changes
    (§4.5.2, §4.7.2).
Bootloader (isc-fs/stm32-can-bootloader v1.7.0):
  - OB_READ (0x50, no session) returns HAL_FLASHEx_OBGetConfig's WRP mask, user
    config, RDP and BOR (bl_proto.c:1065-1082, bl_obyte.c:19-34); GET_HEALTH's
    flags bit 4 is sector 0's WRP, read live (bl_health.c:92-97);
  - OB_APPLY_WRP (0x51, session, token "WRP\\0") ACKs, then programs WPSN_PRG1
    &= ~0x01 through HAL_FLASH_Unlock / HAL_FLASH_OB_Unlock /
    HAL_FLASHEx_OBProgram / HAL_FLASH_OB_Launch (bl_proto.c:1084-1172,
    bl_obyte.c:54-99, stm32h7xx_hal_flash_ex.c:1251-1264);
  - it refuses to erase or write outside sectors 1..6 itself, NACK
    PROTECTED_ADDR (bl_flash.c:31-59, bl_proto.c:841): WRP is the second layer,
    BENCH_TESTS.md Test 4 checks it by calling HAL_FLASH_Program on sector 0
    from a debugger.
The ECU (node 0x1) stands for any board: the bootloader is the same image.
"""
import struct
import subprocess
from pathlib import Path

import pytest

from vhil import can_bootloader as cb
from vhil import elf
from vhil.can_bootloader import CanBootloader
from vhil.sim import Sim
from vhil.system import REPO

NODE = 0x1
AUTO_JUMP_MS = 2000
HEARTBEAT = 0x100
FLASH = 0x52002000                      # FLASH_R_BASE (stm32h733xx.h:2215)
KEYR1, CR1, SR1, CCR1, OPTKEYR, OPTCR = 0x04, 0x0C, 0x10, 0x14, 0x08, 0x18
OPTSR_CUR, OPTSR_PRG, PRAR_CUR1, SCAR_CUR1 = 0x1C, 0x20, 0x28, 0x30
WPSN_CUR1, WPSN_PRG1, BOOT_CUR, OPTSR2_CUR = 0x38, 0x3C, 0x40, 0x70
CR_KEYS = (0x45670123, 0xCDEF89AB)
OPT_KEYS = (0x08192A3B, 0x4C5D6E7F)
OPTLOCK, OPTSTART = 1 << 0, 1 << 1
OPT_BUSY, OPTCHANGEERR = 1 << 0, 1 << 30
CR_SER, CR_START = 1 << 2, 1 << 7
WRPERR = 1 << 17
CONTROLLER = "sysbus.flashController_h7"
# RM0468 Table 18: factory option bytes.
FACTORY = {OPTSR_CUR: 0x179EAAF0, PRAR_CUR1: 0x000000FF, SCAR_CUR1: 0x800000FF,
           WPSN_CUR1: 0x000000FF, BOOT_CUR: 0x1FF00800, OPTSR2_CUR: 0x00000000}
SECTOR0, SECTOR6_TOP = 0x08000000, 0x080DFFE0
HAL_OK, HAL_ERROR = 0, 1
FLASH_TYPEPROGRAM_FLASHWORD = 0x01      # stm32h7xx_hal_flash.h:141
PFLASH_ERRORCODE = 24                   # FLASH_ProcessTypeDef.ErrorCode (stm32h7xx_hal_flash.h)
TRAP, DATA = 0x24040000, 0x24040100     # AXI SRAM the bootloader leaves alone (_end 0x24000C68)


def _flat(elf_path: Path) -> bytes:
    binary = elf_path.with_suffix(".bin")
    if not binary.exists():
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(elf_path), str(binary)],
                       check=True)
    return binary.read_bytes()


def _start(request, images, **kwargs):
    log_dir = request.config.getoption("--sim-log-dir")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        kwargs["log_path"] = Path(log_dir) / f"flash-ob-{request.node.name}.log"
    return Sim(REPO / "systems" / "ecu.yaml", images("ecu"), **kwargs)


@pytest.fixture
def ecu(request, images):
    with _start(request, images) as sim:
        yield sim


@pytest.fixture
def ecu_wrp(request, images):
    """A board whose sector 0 was write-protected before this power-on, as
    bench-01's AMS in slot 2 is: the system file's write_protect."""
    with _start(request, images, write_protect={"ecu": [0]}) as sim:
        yield sim


@pytest.fixture
def app(firmware):
    return _flat(firmware("ecu"))


def _reg(sim, offset):
    return _word(sim, FLASH + offset)


def _set(sim, offset, value):
    sim.monitor(f"sysbus WriteDoubleWord {FLASH + offset:#x} {value:#x}", board="ecu")


def _word(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}", board="ecu").strip(), 16)


def _words(sim, address, n):
    return [_word(sim, address + 4 * i) for i in range(n)]


def _wrp_errors(sim):
    return int(sim.call(CONTROLLER, "WriteProtectionErrors", board="ecu").strip(), 0)


def _protected(sim):
    return int(sim.call(CONTROLLER, "WriteProtectedSectors", board="ecu").strip(), 0)


def _parked(sim):
    """The bootloader in its window, the auto-jump cancelled by DISCOVER."""
    sim.run_for(ms=300)
    bl = CanBootloader(sim, "can_acu", NODE)
    assert [d.node for d in bl.discover()] == [NODE]
    return bl


def _app_boots(sim):
    t = sim.now_us()
    sim.run_for(ms=AUTO_JUMP_MS + 500)
    return sim.can("can_acu").count([HEARTBEAT], t) > 0


def _hal_flash_program(sim, firmware, address, data):
    """BENCH_TESTS.md Test 4, as a debugger runs it: call the bootloader's own
    HAL_FLASH_Program(FLASHWORD, address, data) with the CPU, returning into a
    `b .` in SRAM; returns (HAL status, pFlash.ErrorCode)."""
    bl_elf = firmware("can-bootloader")
    program, _ = elf.symbol(bl_elf, "HAL_FLASH_Program")
    pflash, _ = elf.symbol(bl_elf, "pFlash")
    sim.monitor(f"sysbus WriteWord {TRAP:#x} 0xE7FE", board="ecu")          # b .
    for i in range(0, 32, 4):
        sim.monitor(f"sysbus WriteDoubleWord {DATA + i:#x} "
                    f"{struct.unpack_from('<I', data, i)[0]:#x}", board="ecu")
    for key in CR_KEYS:
        _set(sim, KEYR1, key)
    for reg, value in (("R0", FLASH_TYPEPROGRAM_FLASHWORD), ("R1", address), ("R2", DATA),
                       ("LR", TRAP | 1), ("PC", program & ~1)):
        sim.monitor(f'cpu SetRegister "{reg}" {value:#x}', board="ecu")
    sim.run_for(ms=5)
    assert int(sim.monitor("cpu PC", board="ecu").strip(), 16) == TRAP, "HAL_FLASH_Program did not return"
    status = int(sim.monitor('cpu GetRegister "R0"', board="ecu").strip(), 16)
    return status, _word(sim, pflash + PFLASH_ERRORCODE)


# -- factory state and the change sequence ---------------------------------------

def test_factory_option_bytes_read_back(ecu):
    """Table 18 in the registers, and as the bootloader reports it: no sector
    protected, user config = OPTSR without BOR and RDP."""
    assert {off: _reg(ecu, off) for off in FACTORY} == FACTORY
    assert _reg(ecu, OPTCR) & OPTLOCK
    bl = _parked(ecu)
    status = bl.ob_read()
    assert (status.wrp_sector_mask, status.user_config, status.bor_level) == \
        (0x00, FACTORY[OPTSR_CUR] & ~0xFF0C, 0)
    assert not bl.health().wrp_protected


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/stm32-can-bootloader#191: v1.7.0 bl_obyte.c:31 stores (uint8_t)(ob.RDPLevel & 0xFF); "
    "the HAL's RDPLevel is OB_RDP_LEVEL_x = 0xAA00/0x5500/0xCC00 "
    "(stm32h7xx_hal_flash_ex.h:295-297), so OB_READ's rdp_level is 0 at every level"))
def test_ob_read_reports_the_rdp_level(ecu):
    """bl_obyte.h:41 documents rdp_level as the raw RDP byte: 0xAA at level 0."""
    assert _parked(ecu).ob_read().rdp_level == 0xAA


def test_the_option_byte_change_sequence(ecu):
    """§4.4.3 from a debugger: _PRG locked until OPTKEYR's keys; staged values
    reach _CUR only on OPTSTART and are lost at reset if never launched; the
    programmed bytes survive a warm reset and a power cycle; a wrong key locks
    OPTCR until reset."""
    _set(ecu, WPSN_PRG1, 0xFE)
    assert _reg(ecu, WPSN_PRG1) == 0xFF, "a _PRG register took a write while OPTLOCK was set"
    for key in OPT_KEYS:
        _set(ecu, OPTKEYR, key)
    assert not _reg(ecu, OPTCR) & OPTLOCK
    _set(ecu, WPSN_PRG1, 0xFE)
    assert (_reg(ecu, WPSN_PRG1), _reg(ecu, WPSN_CUR1)) == (0xFE, 0xFF)

    ecu.monitor("machine Reset", board="ecu")       # staged, not launched: lost
    assert (_reg(ecu, WPSN_PRG1), _reg(ecu, WPSN_CUR1)) == (0xFF, 0xFF)
    assert _reg(ecu, OPTCR) & OPTLOCK

    for key in OPT_KEYS:
        _set(ecu, OPTKEYR, key)
    _set(ecu, WPSN_PRG1, 0xFE)
    _set(ecu, OPTCR, _reg(ecu, OPTCR) | OPTSTART)
    assert _reg(ecu, WPSN_CUR1) == 0xFE
    assert _reg(ecu, OPTSR_CUR) & (OPT_BUSY | OPTCHANGEERR) == 0
    assert _protected(ecu) == 0x01

    ecu.monitor("machine Reset", board="ecu")
    assert (_reg(ecu, WPSN_CUR1), _reg(ecu, WPSN_PRG1)) == (0xFE, 0xFE)
    ecu.power_cycle("ecu")
    assert (_reg(ecu, WPSN_CUR1), _reg(ecu, WPSN_PRG1)) == (0xFE, 0xFE)

    _set(ecu, OPTKEYR, 0x12345678)
    for key in OPT_KEYS:
        _set(ecu, OPTKEYR, key)
    assert _reg(ecu, OPTCR) & OPTLOCK, "unlocked after a wrong key"
    _set(ecu, WPSN_PRG1, 0xFF)
    assert _reg(ecu, WPSN_PRG1) == 0xFE


# -- the bootloader's WRP latch -----------------------------------------------------

def test_the_bootloader_latches_wrp_over_can(ecu, app):
    """OB_APPLY_WRP through the HAL path: sector 0 protected at once and
    reported so; after a power cycle it still is, the app still boots, the
    bootloader still refuses sector 0, and the app sectors still flash."""
    bl = _parked(ecu)
    bl.connect()
    bl.apply_wrp()
    ecu.run_for(ms=50)
    assert (_reg(ecu, WPSN_CUR1), _protected(ecu)) == (0xFE, 0x01)
    assert bl.ob_read().wrp_sector_mask == 0x01
    assert bl.health().wrp_protected

    ecu.power_cycle("ecu")
    assert _reg(ecu, WPSN_CUR1) == 0xFE
    assert _app_boots(ecu), "the app no longer boots with sector 0 protected"

    ecu.power_cycle("ecu")
    bl = _parked(ecu)
    assert bl.ob_read().wrp_sector_mask == 0x01 and bl.health().wrp_protected
    bl.connect()
    before = _words(ecu, SECTOR0, 8)
    reply = bl.request(cb.FLASH_ERASE, struct.pack("<II", SECTOR0, cb.SECTOR_SIZE))
    assert (reply.msg_type, reply.code) == (cb.MSG_NACK, cb.NACK_PROTECTED_ADDR)
    assert _words(ecu, SECTOR0, 8) == before
    bl.flash(app, jump=True)
    assert _words(ecu, SECTOR0, 8) == before
    t = ecu.now_us()
    ecu.run_for(ms=1000)
    assert ecu.can("can_acu").count([HEARTBEAT], t) > 0, "the reflashed app did not start"


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/stm32-can-bootloader#192: v1.7.0 bl_obyte.c:90-98 expects HAL_FLASH_OB_Launch to reset "
    "the MCU and returns BL_OB_ERR_HARDWARE when it comes back; on the H7 it returns "
    "HAL_OK with the option bytes programmed (RM0468 §4.4.2-4.4.3, "
    "stm32h7xx_hal_flash.c:973-1000), so every successful latch logs DTC FLASH_HW "
    "(bl_proto.c:1154-1171) and no reset follows (PROVISIONING.md Step 1.6)"))
def test_a_successful_latch_logs_no_flash_fault(ecu):
    bl = _parked(ecu)
    bl.connect()
    dtcs = bl.health().dtc_count
    bl.apply_wrp()
    ecu.run_for(ms=50)
    assert _protected(ecu) == 0x01            # the latch itself worked
    health = bl.health()
    assert health.dtc_count == dtcs and health.last_dtc_code != cb.DTC_FLASH_HW, \
        f"DTC {health.last_dtc_code:#06x} logged ({dtcs} -> {health.dtc_count})"


# -- a board provisioned with sector 0 protected --------------------------------------

def test_a_factory_protected_board_reports_and_keeps_it(ecu_wrp, app):
    """write_protect: [0] in the system: protected from the first instruction,
    reported by the bootloader, the app boots and the app sectors flash."""
    assert (_reg(ecu_wrp, WPSN_CUR1), _reg(ecu_wrp, WPSN_PRG1)) == (0xFE, 0xFE)
    bl = _parked(ecu_wrp)
    assert bl.ob_read().wrp_sector_mask == 0x01 and bl.health().wrp_protected
    bl.connect()
    bl.flash(app, jump=False)
    ecu_wrp.power_cycle("ecu")
    assert _app_boots(ecu_wrp)
    assert _reg(ecu_wrp, WPSN_CUR1) == 0xFE


def test_a_protected_sector_refuses_a_debugger_erase(ecu_wrp):
    """CR1.SER + START on sector 0: WRPERR, nothing erased; CLR_WRPERR clears
    it. The same sequence on unprotected sector 6 erases it."""
    ecu_wrp.run_for(ms=300)
    before = _words(ecu_wrp, SECTOR0, 16)
    for key in CR_KEYS:
        _set(ecu_wrp, KEYR1, key)
    _set(ecu_wrp, CR1, CR_SER | (0 << 8) | CR_START)
    assert _reg(ecu_wrp, SR1) & WRPERR
    assert _words(ecu_wrp, SECTOR0, 16) == before
    assert _wrp_errors(ecu_wrp) == 1
    _set(ecu_wrp, CCR1, WRPERR)
    assert not _reg(ecu_wrp, SR1) & WRPERR

    ecu_wrp.monitor(f"sysbus WriteDoubleWord {SECTOR6_TOP:#x} 0x12345678", board="ecu")
    _set(ecu_wrp, CR1, CR_SER | (6 << 8) | CR_START)
    assert not _reg(ecu_wrp, SR1) & WRPERR
    assert _word(ecu_wrp, SECTOR6_TOP) == 0xFFFFFFFF, "sector 6 not erased"
    assert _words(ecu_wrp, SECTOR0, 16) == before


def test_a_protected_sector_refuses_a_debugger_program(ecu_wrp, firmware):
    """BENCH_TESTS.md Test 4: HAL_FLASH_Program on sector 0 returns HAL_ERROR
    with HAL_FLASH_ERROR_WRP and the bootloader's vector table unchanged; on
    an erased word of sector 6 the same call programs it."""
    ecu_wrp.run_for(ms=300)
    pattern = bytes(range(0xA0, 0xC0))
    before = _words(ecu_wrp, SECTOR0, 8)
    status, error = _hal_flash_program(ecu_wrp, firmware, SECTOR0, pattern)
    assert status == HAL_ERROR and error & WRPERR
    assert _words(ecu_wrp, SECTOR0, 8) == before
    assert _wrp_errors(ecu_wrp) >= 1

    assert _words(ecu_wrp, SECTOR6_TOP, 8) == [0xFFFFFFFF] * 8, "precondition: an erased word"
    status, error = _hal_flash_program(ecu_wrp, firmware, SECTOR6_TOP, pattern)
    assert (status, error) == (HAL_OK, 0)
    assert _words(ecu_wrp, SECTOR6_TOP, 8) == list(struct.unpack("<8I", pattern))
