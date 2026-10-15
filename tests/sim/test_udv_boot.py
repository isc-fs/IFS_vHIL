"""udv-boot: the uDV on its MainLite, through its CAN bootloader to its app,
its CAN traffic, and its road back into the bootloader, in virtual time.

Bootloader facts (isc-fs/stm32-can-bootloader v1.7.0): as in
test_ecu_bootloader.py; the uDV is node 0x3, flashed over FDCAN2 (the role's,
catalog/boards/mainlite.yaml). It jumps after its 2 s auto-jump window with
IRQs masked, VTOR set to 0x08020000 (ARCHITECTURE.md "Boot flow").
uDV app facts (IFS09-DV-uDV, udv@dev since #12):
  - linked at 0x08020000 (STM32H733XG_FLASH.ld); main() clears PRIMASK first
    (main.c USER CODE 1) and never writes VTOR;
  - firmware-info at +0x400: magic 0xF14F1B00, product IFS09-DV-uDV,
    reserved[0] = node 0x3 (firmware_info.c);
  - FDCAN1 (the DV bus): NMT 0x000 [01 00] at RES init (can_interface.cpp:150,
    :573) and again every RES_NMT_REARM_INTERVAL_MS = 500 while the RES is
    silent (can_task.cpp:45, :101-108); the data-logger frames 0x500, 0x501,
    0x502 every DL_TX_INTERVAL_MS = 100 (can_task.cpp:32, :116-120,
    can_interface.cpp:677-740), from a loop that sleeps osDelay(5)
    (can_task.cpp:122);
  - FDCAN2 (the ACU bus): 0x512, the IMU (ax, ay, az int16 mg, gx int16 0.1
    dps), every 8th 400 Hz TIM2 sample, so 50 Hz, after a 300 x 20 ms gyro
    calibration (imu_task.c:45-58, :92-96; can_interface.cpp sendIMU); the
    attitude filter's atan2 runs on the CORDIC (attitude.c);
  - boot trigger 0x002 B0 07 AD 13 (dlc 4) on FDCAN2, honoured in AS OFF only:
    BKP0R = 0xB00710AD, then NVIC_SystemReset (bl_entry.h, bl_entry.c,
    can_interface.cpp rx_dispatch).
The MainLite's BMI088 model lies flat at rest: +1 g on Z (catalog/models/bmi088.yaml).
"""
import struct
import subprocess
from pathlib import Path

import pytest

from vhil import can_bootloader as cb
from vhil.can_bootloader import CanBootloader, FwInfo
from vhil.sim import Sim
from vhil.system import REPO

NODE = 0x3
AUTO_JUMP_MS = 2000
NMT, DL = 0x000, (0x500, 0x501, 0x502)
IMU = 0x512
TRIGGER = (0x002, bytes.fromhex("B007AD13"))
BKP0R, VTOR = 0x58004050, 0xE000ED08     # RTC_BASE + 0x50 (stm32h733xx.h); SCB->VTOR
BOOT_REQ_MAGIC = 0xB00710AD
AS_OFF = 0
GYRO_CAL_MS = 300 * 20                   # imu_task.c:45-58
STEADY_MS = 8000


def _word(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}", board="udv").strip(), 16)


def _in_bootloader(sim):
    return int(sim.monitor("cpu PC", board="udv").strip(), 16) < cb.APP_BASE


def _flat(elf: Path) -> bytes:
    binary = elf.with_suffix(".bin")
    if not binary.exists():
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(binary)], check=True)
    return binary.read_bytes()


def _gaps_ms(frames):
    return [(b.t_us - a.t_us) / 1000 for a, b in zip(frames, frames[1:])]


@pytest.fixture(scope="module")
def booted(make_sim):
    """One uDV from power-on, through the bootloader's window to its app, then
    STEADY_MS of the app: past the gyro calibration, into the 0x512 stream."""
    sim = make_sim("udv", wait_for_app=False)
    sim.run_for(ms=AUTO_JUMP_MS - 100)
    in_window = {"pc_in_bootloader": _in_bootloader(sim),
                 "frames": sim.can("can_dv").count() + sim.can("can_acu").count()}
    t_app = sim.wait_for_app()
    sim.run_for(ms=STEADY_MS)
    return sim, t_app, in_window


def test_it_boots_through_the_bootloader(booted):
    """Invariant 5: the bootloader holds the board, silent on the app's buses,
    for its 2 s window; then the app runs from 0x08020000 with VTOR there."""
    sim, t_app, in_window = booted
    assert in_window == {"pc_in_bootloader": True, "frames": 0}
    assert AUTO_JUMP_MS * 1000 <= t_app <= (AUTO_JUMP_MS + 100) * 1000
    assert not _in_bootloader(sim)
    assert _word(sim, VTOR) == cb.APP_BASE
    assert sim.read_symbol("udv", "g_telemetry_as_state") == AS_OFF


def test_nmt_then_the_data_logger_on_the_dv_bus(booted):
    """The RES bring-up NMT at once, re-sent every 500 ms (no RES on this
    system), and the three data-logger frames every 100 ms."""
    sim, t_app, _ = booted
    bus = sim.can("can_dv")
    nmt = bus.frames([NMT], t_app)
    assert nmt and nmt[0].data == bytes([0x01, 0x00])
    assert (nmt[0].t_us - t_app) / 1000 < 50, "no NMT at RES init"
    # The loop checks the period every 5 ms (osDelay(5)), so a gap is the
    # period up to one loop late; the frames' own bit-time jitter is under 1 ms.
    assert all(499 <= g <= 500 + 6 for g in _gaps_ms(nmt)), _gaps_ms(nmt)
    for can_id in DL:
        frames = bus.frames([can_id], t_app)
        assert len(frames) >= STEADY_MS // 100 - 1, f"{can_id:#x}: {len(frames)} frames"
        assert all(99 <= g <= 100 + 6 for g in _gaps_ms(frames)), (hex(can_id), _gaps_ms(frames))
    assert not sim.can("can_dv").frames([IMU]), "0x512 belongs to the ACU bus"


def test_the_imu_on_the_acu_bus_after_the_gyro_calibration(booted):
    """0x512 at 50 Hz once the 6 s calibration is done; at rest it reads
    0, 0, +1000 mg and no rotation. The attitude filter ran on the CORDIC."""
    sim, t_app, _ = booted
    imu = sim.can("can_acu").frames([IMU], t_app)
    assert imu, "no 0x512 on the ACU bus"
    start_ms = (imu[0].t_us - t_app) / 1000
    assert GYRO_CAL_MS <= start_ms <= GYRO_CAL_MS + 500, start_ms
    gaps = _gaps_ms(imu)
    # The mean period, not assert_cadence: the timer model's update event
    # comes every ARR counts instead of ARR + 1, so 0x512 runs 0.04 % fast and
    # drifts off a 20 ms grid (#292).
    assert len(imu) >= 50 and 19.9 <= sum(gaps) / len(gaps) <= 20.1, (len(imu), gaps[:5])
    assert all(struct.unpack("<4h", f.data) == (0, 0, 1000, 0) for f in imu)
    assert int(sim.monitor("sysbus.cordic Calculations", board="udv").strip(), 0) > 0


# -- the bootloader: its record, and the road back to it -----------------------------

@pytest.fixture
def udv(images):
    with Sim(REPO / "systems" / "udv.yaml", images("udv")) as sim:
        yield sim


def test_the_bootloader_reports_the_uDV_record(udv, firmware):
    """Parked in its window, the bootloader answers as node 0x3 on the ACU bus
    and GET_FW_INFO returns the record the image carries."""
    udv.run_for(ms=300)
    bl = CanBootloader(udv, "can_acu", NODE)
    assert [d.node for d in bl.discover()] == [NODE]
    app = _flat(firmware("udv"))
    info = bl.fw_info()
    assert info == FwInfo.from_image(app)
    assert (info.magic, info.product, info.reserved[0]) == (cb.FWINFO_MAGIC, "IFS09-DV-uDV", NODE)


def test_the_trigger_parks_the_bootloader(udv):
    """In AS OFF, 0x002 B0 07 AD 13 resets into the bootloader, which consumes
    BKP0R (one-shot), stays (no auto-jump, no app frames) and answers as 0x3."""
    udv.wait_for_app()
    udv.run_for(ms=300)
    assert udv.read_symbol("udv", "g_telemetry_as_state") == AS_OFF
    udv.can("can_acu").send(*TRIGGER)
    t = udv.run_for(ms=300)
    assert _in_bootloader(udv), "the trigger did not reset into the bootloader"
    assert _word(udv, BKP0R) == 0
    udv.run_for(ms=AUTO_JUMP_MS + 1000)
    assert _in_bootloader(udv), "auto-jumped after the trigger"
    assert udv.can("can_dv").count(list(DL), t) == 0
    assert [d.node for d in CanBootloader(udv, "can_acu", NODE).discover()] == [NODE]


@pytest.mark.parametrize("bus, can_id, data", [
    ("can_acu", 0x002, "B007AD11"),      # the AMS's payload, same id, same wire
    ("can_acu", 0x002, "B007AD12"),      # the ECU's
    ("can_acu", 0x002, "B007AD1300"),    # dlc 5
    ("can_acu", 0x003, "B007AD13"),      # wrong id
    ("can_dv", 0x002, "B007AD13"),       # the DV bus, not the flash bus
], ids=["ams-payload", "ecu-payload", "dlc", "id", "dv-bus"])
def test_a_near_miss_trigger_is_ignored(udv, bus, can_id, data):
    udv.wait_for_app()
    udv.can(bus).send(can_id, bytes.fromhex(data))
    t = udv.run_for(ms=500)
    assert not _in_bootloader(udv) and _word(udv, BKP0R) != BOOT_REQ_MAGIC
    assert udv.can("can_dv").count([DL[0]], t - 300_000) >= 2, "the app stopped"


def test_the_cordic_computes_phase_and_modulus(udv):
    """The CORDIC model (models/renode/Stm32H7Cordic.cs), as attitude.c drives
    it: PHASE, two q1.31 arguments in, two results out (RM0468 CORDIC; CSR
    FUNC 2, NARGS, NRES). x = y = 0.5 gives atan2 / pi = 0.25 and
    sqrt(0.5). Done on the parked bootloader, which never touches it."""
    udv.run_for(ms=100)
    base, csr = 0x48024400, 2 | 1 << 19 | 1 << 20
    udv.monitor(f"sysbus WriteDoubleWord {base:#x} {csr:#x}", board="udv")
    for arg in (0x40000000, 0x40000000):
        udv.monitor(f"sysbus WriteDoubleWord {base + 4:#x} {arg:#x}", board="udv")
    assert _word(udv, base) >> 31 == 1, "RRDY not set"
    phase, modulus = _word(udv, base + 8), _word(udv, base + 8)
    assert phase == 0x20000000
    assert abs(modulus - round(0.5 ** 0.5 * 2 ** 31)) <= 1
    assert _word(udv, base) >> 31 == 0, "RRDY still set after the last read"
