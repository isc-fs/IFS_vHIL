"""The AMS's ImuTask against the MainLite's BMI088 on I2C2 (#59).

Firmware facts (IFS08-CE-AMS dev, 1508d13):
  ImuTask: init_sensor reads ACC_CHIP_ID (0x18, reg 0x00, want 0x1E) then
    GYRO_CHIP_ID (0x68, reg 0x00, want 0x0F) with blocking HAL_I2C_Mem_Read,
    powers the accelerometer up (ACC_PWR_CONF 0x7C <- 0x00, 5 ms, ACC_PWR_CTRL
    0x7D <- 0x04, 50 ms), writes ACC_CONF 0x8A, ACC_RANGE 0x01 (+/-6 g),
    GYRO_RANGE 0x02 (+/-500 dps), GYRO_BANDWIDTH 0x03, GYRO_LPM1 0x00, and
    reads the four back (GYRO_BANDWIDTH masked 0x7F)     imu_task.cpp:97-130
  then every 10 ms two 6-byte HAL_I2C_Mem_Read_DMA bursts (accel 0x12,
    gyro 0x02), RX on DMA1 Stream0                       imu_task.cpp:196-212,
                                                         stm32h7xx_hal_msp.c:311
  any failure: reset_bus and re-init after ImuRetryPeriodMs = 1000 ms, a
    5 ms transfer timeout              imu_task.cpp:181-205, ams_config.hpp:357-360
  ImuState: 0 Init, 1 Running, 2 NotFound, 3 BusError; fail step 1 = the
    accelerometer chip ID                       imu_task.h:22-27, imu_task.cpp:59-63
  samples go to IMUnnnn.BIN beside LOGnnnn.CSV (split-rate logging,
    IFS08-CE-AMS #596/#598/#601/#603; dev): a 512-byte header, then one
    16-byte ImuSample per sample (tick_ms u32, acc i16 x3, gyr i16 x3, raw
    counts; scales +/-6 g and +/-500 dps over +/-32768 in the schema)
                                              imu_record.hpp:28-33,
                                              bin_log.hpp:104-117
  sealed to .BIN with a .CRC sidecar over the whole file, header included;
    an orphan .TMP is sealed at the next mount
                                              sd_logger_task.cpp:227-290,
                                              :342-385, ams_config.hpp:367-369
  HAL_I2C_ERROR_AF 0x04 (NACK), HAL_I2C_ERROR_TIMEOUT 0x20
                                              stm32h7xx_hal_i2c.h:166, 169
"""
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

import ams_binlog as binlog
from vhil import peripheral_guard as guard
from vhil.sim import Sim
from vhil.system import REPO

ACC, GYR = "sysbus.i2c2_h7.imu_acc", "sysbus.i2c2_h7.imu_gyr"
INIT, RUNNING, NOT_FOUND, BUS_ERROR = range(4)
STEP_ACC_CHIP_ID = 1
HAL_I2C_ERROR_AF, HAL_I2C_ERROR_TIMEOUT = 0x04, 0x20
CARD_BYTES = 0x8000000          # catalog/models/sd-card.yaml capacity

# The IMU's anonymous-namespace globals (imu_task.cpp:46-57), by linker name.
_SYMBOLS = {
    "samples": ("_ZN12_GLOBAL__N_113g_imu_samplesE", 4),
    "read_errors": ("_ZN12_GLOBAL__N_117g_imu_read_errorsE", 4),
    "inits": ("_ZN12_GLOBAL__N_111g_imu_initsE", 4),
    "state": ("_ZN12_GLOBAL__N_111g_imu_stateE", 1),
    "fail_step": ("_ZN12_GLOBAL__N_115g_imu_fail_stepE", 1),
    "i2c_error": ("_ZN12_GLOBAL__N_115g_imu_i2c_errorE", 4),
}

# What the car does in each phase of the logged run: (mg x, y, z), (dps x, y, z).
MOTION_1 = ((250, -500, 1000), (10, -20, 100))
MOTION_2 = ((-1500, 0, 2000), (-250, 0, 45.5))
PHASE_1_MS, PHASE_2_MS, AFTER_CUT_MS = 3000, 2500, 3000


def imu(sim) -> dict:
    return {k: sim.read_symbol("ams", s, n) for k, (s, n) in _SYMBOLS.items()}


def call(sim, die, method, *args) -> int:
    return int(sim.call(die, method, *args).strip(), 0)


def move(sim, motion):
    (ax, ay, az), (gx, gy, gz) = motion
    sim.call(ACC, "SetAcceleration", ax, ay, az)
    sim.call(GYR, "SetAngularRate", gx, gy, gz)


def respond(sim, alive: bool):
    sim.call(ACC, "Respond", alive)
    sim.call(GYR, "Respond", alive)


# -- what the firmware should log ----------------------------------------------

def expected_counts(motion) -> list[int]:
    """The six raw counts for a motion, as the BMI088 reports them at +/-6 g
    and +/-500 dps (65.536 LSB/dps): what an IMU record carries."""
    (mg, dps) = motion
    acc = [max(-32768, min(32767, round(a / 6000 * 32768))) for a in mg]
    gyr = [max(-32768, min(32767, round(r / 500 * 32768))) for r in dps]
    return acc + gyr


# -- a logged run on a card ----------------------------------------------------

def _tool(name):
    path = shutil.which(name)
    if path is None:
        pytest.fail(f"{name} not installed (dosfstools / mtools)")
    return path


def _card(tmp_path):
    img = tmp_path / "card.img"
    with open(img, "wb") as f:
        f.truncate(CARD_BYTES)
    subprocess.run([_tool("mkfs.fat"), "-F", "32", "-s", "1", "-n", "AMS", str(img)],
                   check=True, capture_output=True)
    return img


def _read(img, name):
    return subprocess.run([_tool("mtype"), "-i", str(img), f"::/{name}"],
                          check=True, capture_output=True).stdout


def _ls(img):
    out = subprocess.run([_tool("mdir"), "-i", str(img), "-b", "::"],
                         check=True, capture_output=True, text=True).stdout
    return sorted(line.strip().lstrip(":/").upper() for line in out.splitlines() if line.strip())


@pytest.fixture(scope="module")
def run(tmp_path_factory, images, request):
    """Boot with the IMU in MOTION_1, switch to MOTION_2, cut the power and
    boot again so the orphaned IMU0000.TMP is sealed. Returns what was seen
    along the way and the card."""
    work = tmp_path_factory.mktemp("imu")
    img = _card(work)
    # Always logged: the peripheral guard reads it.
    log_dir = request.config.getoption("--sim-log-dir")
    log = work / "ams-imu.log"
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log = Path(log_dir) / "ams-imu.log"
    seen = {}
    with Sim(REPO / "systems" / "ams.yaml", images("ams"),
             params={"sd": {"image": str(img)}}, card_dirs=[img.parent], log_path=log) as sim:
        seen["split_rate"] = binlog.split_rate(sim)
        move(sim, MOTION_1)
        sim.wait_for_app()
        sim.run_for(ms=PHASE_1_MS)
        seen["phase1"] = imu(sim)
        seen["chip_id_reads"] = (call(sim, ACC, "ReadCount", 0x00), call(sim, GYR, "ReadCount", 0x00))
        seen["bursts"] = (call(sim, ACC, "ReadCount", 0x12), call(sim, GYR, "ReadCount", 0x02))
        seen["nacks"] = call(sim, "sysbus.i2c2_h7", "NackCount")
        seen["registers"] = {name: call(sim, die, "RegisterValue", reg) for name, (die, reg) in {
            "ACC_PWR_CONF": (ACC, 0x7C), "ACC_PWR_CTRL": (ACC, 0x7D), "ACC_CONF": (ACC, 0x40),
            "ACC_RANGE": (ACC, 0x41), "GYRO_RANGE": (GYR, 0x0F),
            "GYRO_BANDWIDTH": (GYR, 0x10), "GYRO_LPM1": (GYR, 0x11)}.items()}
        # In the app's clock, as the rows' tick_ms: its HAL tick (the app
        # starts after the bootloader's window, not at 0).
        seen["switch_ms"] = sim.read_symbol("ams", "uwTick", 4)
        move(sim, MOTION_2)
        sim.run_for(ms=PHASE_2_MS)
        # Power cut as the virtual broker does it (vhil/broker.py).
        sim.monitor("machine Reset", board="ams")
        sim.monitor('cpu SetRegister "BasePri" 0x0', board="ams")
        sim.wait_for_app()
        sim.run_for(ms=AFTER_CUT_MS)
        seen["after_cut"] = imu(sim)
    seen["card"] = img
    seen["log"] = log
    return seen


def test_the_imu_task_reads_both_chip_ids_and_runs(run):
    s = run["phase1"]
    assert s["state"] == RUNNING and s["fail_step"] == 0, s
    assert s["inits"] == 1, f"{s['inits']} bring-ups in {PHASE_1_MS} ms"
    assert run["chip_id_reads"] == (1, 1), "each chip ID read once, at bring-up"
    assert run["nacks"] == 0


def test_the_sensor_is_configured_as_the_firmware_intends(run):
    """imu_record.hpp:55-71: accelerometer active and on, OSR4 at 400 Hz,
    +/-6 g; gyroscope +/-500 dps, 400 Hz / 47 Hz, normal mode."""
    assert run["registers"] == {"ACC_PWR_CONF": 0x00, "ACC_PWR_CTRL": 0x04, "ACC_CONF": 0x8A,
                                "ACC_RANGE": 0x01, "GYRO_RANGE": 0x02,
                                "GYRO_BANDWIDTH": 0x83, "GYRO_LPM1": 0x00}


def test_it_samples_both_dies_at_100_hz_without_errors(run):
    s = run["phase1"]
    assert s["read_errors"] == 0
    # 100 Hz from the end of the ~55 ms bring-up.
    assert (PHASE_1_MS - 100) // 10 <= s["samples"] <= PHASE_1_MS // 10, s["samples"]
    assert run["bursts"] == (s["samples"], s["samples"]), "one burst per die per sample"


def test_a_power_cut_costs_one_fresh_bring_up(run):
    s = run["after_cut"]
    assert s["state"] == RUNNING and s["inits"] == 1 and s["read_errors"] == 0, s


def test_the_imu_path_touches_only_modelled_hardware(run):
    """configs/peripherals.yaml no longer excuses I2C2 (the KNOWN GAP #59
    entries went with this model): the bring-up, the DMA reads and the
    model's own registers must all be modelled or explained."""
    findings = guard.unexplained(run["log"].read_text(errors="replace"), guard.load_rules())
    assert not findings, guard.report(findings)


@pytest.fixture(scope="module")
def imu_log(run):
    if not run["split_rate"]:
        pytest.skip("this AMS build logs the IMU to IMUnnnn.CSV (pre split-rate, main)")
    return binlog.decode(_read(run["card"], "IMU0000.BIN"))


def test_the_card_holds_the_imu_log_with_its_crc(run, imu_log):
    files = _ls(run["card"])
    assert {"IMU0000.BIN", "IMU0000.CRC", "LOG0000.CSV"} <= set(files), files
    data = _read(run["card"], "IMU0000.BIN")
    crc = _read(run["card"], "IMU0000.CRC").decode().strip()
    assert int(crc, 16) == zlib.crc32(data), f"IMU0000.CRC {crc} vs {zlib.crc32(data):08X}"
    h = imu_log
    assert (h.version, h.stream, h.index, h.record_size) == (binlog.FORMAT_VERSION, "IMU", 0, 16)
    assert h.schema == binlog.IMU_SCHEMA, h.schema
    assert h.tail == 0, f"{h.tail} bytes of a partial record"


def test_the_logged_rows_carry_what_the_sensor_measured(run, imu_log):
    rows = imu_log.records
    ticks = [r["tick_ms"] for r in rows]
    assert ticks == sorted(ticks) and set(b - a for a, b in zip(ticks, ticks[1:])) == {10}, \
        "records every 10 ms with none missing"
    assert imu_log.open_tick_ms <= ticks[0]
    switch = run["switch_ms"]
    before = {tuple(r["a"] + r["g"]) for r in rows if r["tick_ms"] < switch}
    after = {tuple(r["a"] + r["g"]) for r in rows if r["tick_ms"] > switch + 10}
    assert before == {tuple(expected_counts(MOTION_1))}, before
    assert after == {tuple(expected_counts(MOTION_2))}, after
    # The schema's scales give back the motion, to one count.
    last = rows[-1]
    (mg, dps) = MOTION_2
    for got, want in zip(binlog.scaled(imu_log, "a", last["a"]), mg):
        assert abs(got - want / 1000) <= 6 / 32768, (got, want)
    for got, want in zip(binlog.scaled(imu_log, "g", last["g"]), dps):
        assert abs(got - want * 3.141592653589793 / 180) <= 8.73 / 32768, (got, want)
    # Synced at least once (LogSyncPeriodMs) after the switch, before the cut.
    assert max(ticks) > switch + 1000, f"last record at {max(ticks)} ms, switch at {switch} ms"


# -- a dead IMU, and one that dies and comes back --------------------------------

@pytest.fixture(scope="module")
def dead(make_sim):
    sim = make_sim("ams", wait_for_app=False)
    respond(sim, False)                         # dead from power-on
    sim.wait_for_app()
    return sim


def test_a_dead_imu_costs_one_nacked_address_per_second(dead):
    """An unpowered or missing BMI088 NACKs its address: the HAL fails at
    once with AF, not after its 5 ms timeout, and only the first chip ID is
    ever tried (imu_task.cpp:101)."""
    dead.run_for(ms=5500)
    s = imu(dead)
    assert s["state"] == NOT_FOUND and s["fail_step"] == STEP_ACC_CHIP_ID, s
    assert s["i2c_error"] & HAL_I2C_ERROR_AF and not s["i2c_error"] & HAL_I2C_ERROR_TIMEOUT, \
        f"HAL I2C error 0x{s['i2c_error']:X}"
    assert s["samples"] == 0 and s["inits"] == 0
    tries = call(dead, ACC, "NackCount")
    assert 5 <= tries <= 7, f"{tries} attempts in 5.5 s (one per ImuRetryPeriodMs)"
    assert call(dead, GYR, "NackCount") == 0


def test_it_comes_up_within_a_retry_period_once_the_imu_answers(dead):
    respond(dead, True)
    dead.run_for(ms=1200)
    s = imu(dead)
    assert s["state"] == RUNNING and s["inits"] == 1 and s["samples"] > 0, s


def test_losing_the_imu_mid_run_fails_the_dma_read_and_recovers(dead):
    """A NACK inside HAL_I2C_Mem_Read_DMA reaches the task through
    HAL_I2C_ErrorCallback: one read error, back to bring-up, NotFound while
    the chip is gone, Running again once it answers."""
    before = imu(dead)
    respond(dead, False)
    dead.run_for(ms=30)
    s = imu(dead)
    assert s["read_errors"] == before["read_errors"] + 1 and s["state"] == BUS_ERROR, s
    dead.run_for(ms=1100)
    assert imu(dead)["state"] == NOT_FOUND
    respond(dead, True)
    dead.run_for(ms=1100)
    s = imu(dead)
    assert s["state"] == RUNNING and s["inits"] == before["inits"] + 1, s
