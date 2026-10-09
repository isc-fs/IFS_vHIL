"""AMS SD logging through the SDMMC1 IDMA model (#21), read back from the card.

Firmware facts (IFS08-CE-AMS dev, 1508d13):
  FatFs on SDMMC1 by IDMA, single buffer: HAL_SD_ReadBlocks_DMA /
    HAL_SD_WriteBlocks_DMA via sd_diskio.c, bounce buffer in .sd_dma (RAM_D1)
  LOGnnnn.TMP is open while logging; it becomes LOGnnnn.CSV with a LOGnnnn.CRC
    sidecar at rotation (5 min / 4 MiB, ams_config.hpp:300, :313) or, after a
    power cut, as an orphan sealed at the next boot (sd_logger_task.cpp:248-290)
  .CRC: 8 ASCII hex digits + newline, CRC-32 ISO-HDLC = zlib.crc32 (crc32.hpp,
    sd_logger_task.cpp:227-246)
  rows at 4 Hz (SafetyTask capture period 250 ms, ams_config.hpp:282); 331
    columns: 18 head scalars, c<m>_<cell> (5 x 19), t<m>_<slot> (5 x 40), 18
    tail scalars (log_record.hpp:15-21, :116-164); cell, temperature and BMS
    summary fields are empty until every module has reported (bms_valid,
    log_record.hpp:71-75)
  split-rate logging (IFS08-CE-AMS #596/#598/#601/#603): each rotation index
    also owns IMUnnnn, CELnnnn and ELEnnnn .TMP -> .BIN + .CRC, sealed with
    the LOG (binary first, LOG last) (log_names.hpp:5-10,
    sd_logger_task.cpp:283-290, :389-399); format in ams_binlog.py
  CEL: one 5 x 19 cell frame per cell-voltage read (bin_log.hpp:119-146);
    ELE: one record per 10 ms window of the oversampled pack current, its
    mean / min / max (bin_log.hpp:178-202)
  card detect PE3: LOW = card in; with the slot empty the MainLite's pull-up
    holds it HIGH and BSP_SD_Init returns before touching SDMMC
    (fatfs_platform.c BSP_PlatformIsDetected); the logger retries the mount
    every tick and never blocks (sd_logger_task.cpp:1-20)
  g_log_state 0=boot 1=no_card 2=logging 3=io_error (sd_logger_task.cpp:140):
    1 when f_mount fails (:759-764), 3 on teardown after an I/O error
    (:405-415)
  a dead card (detect LOW, no answer): BSP_SD_Init -> HAL_SD_Init
    (bsp_driver_sd.c:42-62) runs the card identification, whose commands
    time out on the controller (Stm32H7Sdmmc.cs header, RM0468 60.5.4); the
    logger is SdLoggerTask at osPriorityLow (main.c:112-115)
"""
import shutil
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import ams_binlog as binlog
from vhil.sim import Sim
from vhil.system import REPO

CARD_BYTES = 0x8000000          # catalog/models/sd-card.yaml capacity: 128 MiB
LOG_S, AFTER_CUT_S = 8, 6
RUNS, RUN_S = 3, 5
COLUMNS_MAIN = 314                                  # a pre split-rate build's LOG.CSV
# LOG.CSV's columns (log_record.hpp:116-154): head, cells, temperatures, tail.
HEAD = ["tick_ms", "fsm", "mode", "ams_ok", "fault", "detail", "tsms", "dash_chg", "mod_mask",
        "pack_mV", "I_raw_mA", "I_filt_mA", "dcbus_V", "vmin_mV", "vmax_mV", "tmin_C", "tmax_C",
        "tavg_C"]
TAIL = ["bal_state", "bal_inhibit", "bal_active", "bms_valid", "bms_age_ms", "soc_ppm",
        "soc_sig_ppm", "soc_flags", "soc_seeds", "q_dis_mAs", "q_chg_mAs", "q_gaps",
        "dcbus_age_ms", "veh_flags", "chg_age_ms", "pec_err", "spi_err", "chain_rec"]
LOG_COLUMNS = (HEAD + [f"c{m}_{n}" for m in range(5) for n in range(19)]
               + [f"t{m}_{n}" for m in range(5) for n in range(40)] + TAIL)
BIN_KINDS = {"IMU": binlog.IMU_SCHEMA, "CEL": binlog.CEL_SCHEMA, "ELE": binlog.ELE_SCHEMA}
STATUS, TIMING = 0x4A0, 0x6C1
SDMMC, CARD = "sysbus.sdmmc1", "sysbus.sdmmc1.sd"   # catalog/boards/mainlite.yaml
LOG_STATE = "_ZN12_GLOBAL__N_111g_log_stateE"      # sd_logger_task.cpp:117
LOG_NO_CARD, LOG_LOGGING = 1, 2                    # sd_logger_task.cpp:117, 666, 668
AMS_OK = ("sysbus.gpioPortB", 4)


def _tool(name):
    path = shutil.which(name)
    if path is None:
        pytest.fail(f"{name} not installed (dosfstools / mtools)")
    return path


def _card(tmp_path):
    """A freshly formatted FAT32 card image, as a new SDHC card comes."""
    img = tmp_path / "card.img"
    with open(img, "wb") as f:
        f.truncate(CARD_BYTES)
    subprocess.run([_tool("mkfs.fat"), "-F", "32", "-s", "1", "-n", "AMS", str(img)],
                   check=True, capture_output=True)
    return img


def _ls(img):
    out = subprocess.run([_tool("mdir"), "-i", str(img), "-b", "::"],
                         check=True, capture_output=True, text=True).stdout
    return sorted(line.strip().lstrip(":/").upper() for line in out.splitlines() if line.strip())


def _read(img, name):
    return subprocess.run([_tool("mtype"), "-i", str(img), f"::/{name}"],
                          check=True, capture_output=True).stdout


def _cut(sim):
    """Power cut as the virtual broker does it (vhil/broker.py)."""
    sim.monitor("machine Reset", board="ams")
    sim.monitor('cpu SetRegister "BasePri" 0x0', board="ams")
    sim.wait_for_app()                          # through the bootloader again


@pytest.fixture(scope="module")
def logged(tmp_path_factory, images, request):
    """Log for LOG_S on a fresh card with one cell and one NTC seeded, cut
    the power, boot again so the orphan .TMP is sealed, then stop. Keeps the
    card image and what the bus showed while logging."""
    img = _card(tmp_path_factory.mktemp("sd"))
    system = REPO / "systems" / "ams.yaml"
    log_dir = request.config.getoption("--sim-log-dir")
    log = None
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log = Path(log_dir) / "ams-sd.log"
    with Sim(system, images("ams"), params={"sd": {"image": str(img)}},
             card_dirs=[img.parent],
             log_path=log) as sim:
        sim.monitor("sysbus.spi1.isospi.cells3 SetCell 2 3650", board="ams")       # c1_11
        sim.monitor("sysbus.spi1.isospi.cells6 SetTemperature 16 400", board="ams")  # t3_10
        sim.wait_for_app()
        sim.run_for(ms=1500)                    # CAN up, listening for the arm
        sim.can("can_acu").send(0x7F0, bytes.fromhex("DEADBEEF"))
        sim.run_for(ms=LOG_S * 1000 - 1500)
        status = [f.t_us for f in sim.can("can_acu").frames(STATUS)]
        timing = sim.can("can_acu").last(TIMING).data
        cut_ms = sim.read_symbol("ams", "uwTick", 4)   # the app's clock, as tick_ms
        split = binlog.split_rate(sim)
        _cut(sim)
        sim.run_for(ms=AFTER_CUT_S * 1000)
    return SimpleNamespace(img=img, status=status, cut_ms=cut_ms, split=split,
                           poll_max_ms=int.from_bytes(timing[2:4], "big"))


@pytest.fixture(scope="module")
def card(logged):
    return logged.img


def _rows(img, name):
    lines = _read(img, name).decode().splitlines()
    header = lines[0].split(",")
    return header, [dict(zip(header, r.split(","))) for r in lines[1:]]


def test_the_card_holds_a_sealed_log(card):
    files = _ls(card)
    assert "LOG0000.CSV" in files and "LOG0000.CRC" in files, f"card holds {files}"
    assert "LOG0001.TMP" in files, f"no new log after the reboot: {files}"


def test_the_sidecar_crc_matches_the_log(card):
    csv = _read(card, "LOG0000.CSV")
    crc = _read(card, "LOG0000.CRC").decode().strip()
    assert int(crc, 16) == zlib.crc32(csv), f"LOG0000.CRC {crc} vs zlib.crc32 {zlib.crc32(csv):08X}"


def test_the_log_has_a_header_and_rows_at_4_hz(card):
    lines = _read(card, "LOG0000.CSV").decode().splitlines()
    header, rows = lines[0].split(","), lines[1:]
    assert all(len(r.split(",")) == len(header) for r in rows), "ragged rows"
    # 4 Hz over the logging window, less boot and the last unsynced rows.
    assert 2 * LOG_S <= len(rows) <= 4 * LOG_S + 2, f"{len(rows)} rows in {LOG_S} s"


def test_the_columns_carry_the_seeded_cell_and_temperature(logged):
    """T-151: the firmware's columns, in its order; the seeded cell and NTC
    in their own columns, every other cell at the model default, once every
    module has reported (bms_valid)."""
    header, rows = _rows(logged.img, "LOG0000.CSV")
    if not logged.split:
        assert len(header) == COLUMNS_MAIN, f"{len(header)} columns"
        valid = rows
    else:
        assert header == LOG_COLUMNS, f"{len(header)} columns: {header[:18]} ... {header[-18:]}"
        valid = [r for r in rows if r["bms_valid"] == "1"]
        assert len(valid) >= len(rows) - 2, f"{len(rows) - len(valid)} rows before the first full poll"
        assert all(r["c1_11"] == "" for r in rows if r["bms_valid"] != "1"), "seed values logged"
    assert all(r["c1_11"] == "3650" and r["c1_10"] == "3700" for r in valid)
    settled = valid[4:]                      # past the first temperature sweep
    assert all(r["t3_10"] == "40" and r["t3_11"] == "25" for r in settled)
    assert all(r["mod_mask"] == "31" for r in valid)


def _bin(img, name):
    data = _read(img, name)
    log = binlog.decode(data)
    assert log.tail == 0, f"{name}: {log.tail} bytes of a partial record"
    return log


@pytest.fixture(scope="module")
def split(logged):
    if not logged.split:
        pytest.skip("this AMS build has no split-rate .BIN logs (main)")
    return logged


def test_the_cell_frames_carry_every_read_of_the_seeded_cell(split):
    """CEL0000.BIN: one frame per cell-voltage read, numbered without a gap,
    each with the 5 x 19 matrix as the chain converted it; the seeded cell
    and its neighbour in their slots (bin_log.hpp:119-176)."""
    log = _bin(split.img, "CEL0000.BIN")
    assert (log.stream, log.index, log.schema) == ("CEL", 0, binlog.CEL_SCHEMA)
    frames = log.records
    assert frames, "no cell frames"
    seq = [f["seq"] for f in frames]
    assert all((b - a) % 0x10000 == 1 for a, b in zip(seq, seq[1:])), f"dropped frames: {seq}"
    t = [f["t_adcv_ms"] for f in frames]
    assert t == sorted(t) and t[0] >= log.open_tick_ms
    clean = [f for f in frames if f["ltc_ok"] == 0x3FF]      # all 10 ICs PEC-clean
    assert len(clean) >= len(frames) - 2, f"{len(frames) - len(clean)} frames with a PEC miss"
    assert all(f["c"][1][11] == 3650 and f["c"][1][10] == 3700 for f in clean)
    assert all(v == 3700 for f in clean for m, row in enumerate(f["c"]) for n, v in enumerate(row)
               if (m, n) != (1, 11))
    # The logged window: from boot to within ~1 s of the cut.
    assert split.cut_ms - t[-1] <= 1250, f"last frame at {t[-1]} ms, cut at {split.cut_ms} ms"


def test_the_current_windows_run_at_100_hz_and_agree_with_the_log(split):
    """ELE0000.BIN: a record per 10 ms window of the 12.5 kHz oversampled
    capture, numbered without a gap, mean within [min, max]; at a constant
    input every window's mean is the current LOG.CSV reports
    (bin_log.hpp:178-230, ams_config.hpp:408)."""
    log = _bin(split.img, "ELE0000.BIN")
    assert (log.stream, log.index, log.schema) == ("ELE", 0, binlog.ELE_SCHEMA)
    recs = log.records
    assert len(recs) >= 100 * (LOG_S - 2), f"{len(recs)} windows in {LOG_S} s"
    seq = [r["seq"] for r in recs]
    assert all((b - a) % 0x10000 == 1 for a, b in zip(seq, seq[1:])), "dropped windows"
    ticks = [r["tick_ms"] for r in recs]
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert all(9 <= g <= 11 for g in gaps), f"window ends {sorted(set(gaps))} ms apart"
    assert all(100 <= r["n"] <= 126 for r in recs), sorted({r["n"] for r in recs})
    assert all(r["flags"] == 0 for r in recs), sorted({r["flags"] for r in recs})
    assert all(r["i_min"] <= r["i_mean"] <= r["i_max"] for r in recs)
    _, rows = _rows(split.img, "LOG0000.CSV")
    raw = {int(r["I_raw_mA"]) for r in rows}
    means = {r["i_mean"] for r in recs}
    assert max(raw) - min(raw) <= 50 and max(means) - min(means) <= 50, (raw, means)
    assert abs(sum(means) / len(means) - sum(raw) / len(raw)) <= 50, (raw, means)


def test_a_power_cut_loses_at_most_a_second(logged):
    """T-152: the sealed log reaches to within ~1 s of the cut."""
    _, rows = _rows(logged.img, "LOG0000.CSV")
    last = int(rows[-1]["tick_ms"])
    assert logged.cut_ms - last <= 1250, f"last row at {last} ms, cut at {logged.cut_ms} ms"


def test_logging_never_disturbs_the_main_task(logged):
    """T-150: 0x4A0 keeps its 500 ms while the card is written, and the
    voltage poll stays in budget."""
    t = logged.status[1:]
    deltas = [b - a for a, b in zip(t, t[1:])]
    assert deltas and all(abs(d - 500_000) <= 10_000 for d in deltas), deltas
    assert logged.poll_max_ms < 50, f"voltage poll max {logged.poll_max_ms} ms"


@pytest.fixture(scope="module")
def runs(tmp_path_factory, images):
    """RUNS boots of RUN_S seconds on one card, each ended by a power cut."""
    img = _card(tmp_path_factory.mktemp("sd-runs"))
    with Sim(REPO / "systems" / "ams.yaml", images("ams"),
             params={"sd": {"image": str(img)}}, card_dirs=[img.parent]) as sim:
        split = binlog.split_rate(sim)
        for _ in range(RUNS):
            sim.wait_for_app()
            sim.run_for(ms=RUN_S * 1000)
            _cut(sim)
        sim.run_for(ms=3000)
    return img, split


def test_one_sealed_log_per_run(runs):
    """U-161, U-162: per run, the 4 Hz LOG and the IMU, CEL and ELE binary
    logs of the same rotation index (log_names.hpp:5-10), each sealed with a
    matching .CRC, the next set open, no stray files (AMS#495
    fragmentation). Every .BIN decodes: its header names its stream and run,
    carries the firmware's schema and whole records."""
    img, split = runs
    if not split:
        pytest.skip("this AMS build has no split-rate .BIN logs (main)")
    files = _ls(img)
    sealed = [f"LOG{i:04d}.CSV" for i in range(RUNS)]
    sealed += [f"{kind}{i:04d}.BIN" for kind in BIN_KINDS for i in range(RUNS)]
    expected = sorted(sealed + [f[:-4] + ".CRC" for f in sealed]
                      + [f"{kind}{RUNS:04d}.TMP" for kind in ("LOG", *BIN_KINDS)])
    assert files == expected, f"card holds {files}"
    for f in sealed:
        data = _read(img, f)
        crc = int(_read(img, f[:-4] + ".CRC").decode().strip(), 16)
        assert crc == zlib.crc32(data), f"{f[:-4]}.CRC does not match"
        if f.endswith(".BIN"):
            log = _bin(img, f)
            assert (log.stream, log.index, log.schema) == (f[:3], int(f[3:7]), BIN_KINDS[f[:3]]), f
            assert log.record_size == binlog.RECORD_BYTES[f[:3]] and log.records, f


def test_every_run_starts_fresh_and_keeps_the_mask(runs):
    """U-160: each log starts at its own boot and the module mask holds."""
    img, _ = runs
    for i in range(RUNS):
        _, rows = _rows(img, f"LOG{i:04d}.CSV")
        assert int(rows[0]["tick_ms"]) <= 1000, f"LOG{i:04d} starts at {rows[0]['tick_ms']} ms"
        assert len(rows) >= 2 * (RUN_S - 1)
        assert {r["mod_mask"] for r in rows} == {"31"}


def test_an_empty_slot_boots_clean(images):
    """S-143: card detect HIGH from reset: the AMS boots to a healthy Start,
    AMS_OK HIGH, telemetry on time, the logger idle."""
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.io("ams").set_input("sysbus.gpioPortE", 3, True)
        ok = sim.io("ams").watch("sysbus.gpioPortB", 4)
        sim.wait_for_app()
        sim.run_for(ms=6000)
        assert sim.read_symbol("ams", "g_state_telemetry") == 0
        assert sim.io("ams").level(ok), "AMS_OK low with the slot empty"
        t = [f.t_us for f in sim.can("can_acu").frames(STATUS)]
        assert t and t[0] - sim.app_started["ams"] <= 1_000_000
        assert all(abs((b - a) - 500_000) <= 10_000 for a, b in zip(t, t[1:]))


def _unanswered(sim):
    return int(sim.monitor(f"{SDMMC} UnansweredCommands", board="ams").strip(), 0)


def _health(sim, ok, since_us=0):
    """What the AMS shows of itself: FSM state, AMS_OK, the 0x4A0 cadence
    since `since_us` and the voltage poll's worst time (0x6C1, armed)."""
    t = [f.t_us for f in sim.can("can_acu").frames(STATUS) if f.t_us >= since_us]
    timing = sim.can("can_acu").last(TIMING)
    return SimpleNamespace(
        state=sim.read_symbol("ams", "g_state_telemetry"), ok=sim.io("ams").level(ok),
        deltas=[b - a for a, b in zip(t, t[1:])],
        poll_max_ms=int.from_bytes(timing.data[2:4], "big") if timing else None)


def _assert_healthy(h, what):
    assert h.state == 0, f"{what}: FSM state {h.state}, not Start"
    assert h.ok, f"{what}: AMS_OK low"
    assert len(h.deltas) >= 4 and all(abs(d - 500_000) <= 10_000 for d in h.deltas), \
        f"{what}: 0x4A0 gaps {h.deltas}"
    assert h.poll_max_ms is not None and h.poll_max_ms < 50, \
        f"{what}: voltage poll max {h.poll_max_ms} ms"


def _boot_armed(sim, ms):
    """Run to `ms` into the app (it starts after the bootloader's window),
    arming the pit stream at 1.5 s."""
    t0 = sim.wait_for_app()
    sim.run_for(us=t0 + 1_500_000 - sim.now_us())   # CAN up, listening for the arm
    sim.can("can_acu").send(0x7F0, bytes.fromhex("DEADBEEF"))
    sim.run_for(us=t0 + ms * 1000 - sim.now_us())


@pytest.fixture(scope="module")
def dead(tmp_path_factory, images):
    """A card in the slot (detect LOW) that never answers from power-on, for
    6 s; then it answers, for 4 s more. Keeps what the AMS showed in each."""
    img = _card(tmp_path_factory.mktemp("sd-dead"))
    with Sim(REPO / "systems" / "ams.yaml", images("ams"),
             params={"sd": {"image": str(img), "dead": True}},
             card_dirs=[img.parent]) as sim:
        ok = sim.io("ams").watch(*AMS_OK)
        _boot_armed(sim, 5000)
        tries_at_5s = _unanswered(sim)
        sim.run_for(ms=1000)
        dead = _health(sim, ok)
        dead.log_state = sim.read_symbol("ams", LOG_STATE)
        dead.tries = (tries_at_5s, _unanswered(sim))
        sim.monitor(f"{CARD} Respond true", board="ams")
        t = sim.now_us()
        sim.run_for(ms=4000)
        alive = _health(sim, ok, since_us=t)
        alive.log_state = sim.read_symbol("ams", LOG_STATE)
    alive.files = _ls(img)
    return SimpleNamespace(dead=dead, alive=alive)


def test_a_dead_card_boots_clean(dead):
    """S-143 (dead card): detect LOW but no answer from power-on: the AMS
    boots to a healthy Start, AMS_OK HIGH, 0x4A0 on its 500 ms and the
    voltage poll in budget; the card identification never stalls a task."""
    _assert_healthy(dead.dead, "dead card")


def test_the_logger_reports_no_card_and_keeps_retrying(dead):
    """The failed mount leaves g_log_state at no_card (sd_logger_task.cpp:668)
    and the next drain tick tries again (:662): the controller keeps seeing
    unanswered commands."""
    assert dead.dead.log_state == LOG_NO_CARD
    before, after = dead.dead.tries
    assert after > before > 0, f"unanswered commands {before} -> {after}: no mount retries"


def test_a_card_that_comes_alive_keeps_the_ams_healthy(dead):
    _assert_healthy(dead.alive, "card answering again")


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-AMS#620: a card that failed identification is never mounted until a "
    "reboot. HAL_SD_InitCard ORs each failure into hsd1.ErrorCode "
    "(stm32h7xx_hal_sd.c:534/:543) and nothing clears it before the retry: "
    "BSP_SD_Init calls HAL_SD_Init again without HAL_SD_DeInit "
    "(bsp_driver_sd.c:51), HAL_SD_Init only zeroes ErrorCode on success "
    "(stm32h7xx_hal_sd.c:451), and HAL_SD_ConfigWideBusOperation fails on any "
    "stale bit (stm32h7xx_hal_sd.c:2449). Seen: hsd1.ErrorCode = 0x4 "
    "(SDMMC_ERROR_CMD_RSP_TIMEOUT) after the card answers, g_log_state stuck at 1."))
def test_a_card_that_comes_alive_is_mounted_and_logged(dead):
    """The logger mounts the card once it answers and starts LOG0000."""
    assert dead.alive.log_state == LOG_LOGGING, f"g_log_state {dead.alive.log_state}"
    assert "LOG0000.TMP" in dead.alive.files, f"card holds {dead.alive.files}"


@pytest.fixture(scope="module")
def dies(tmp_path_factory, images):
    """Logging on a good card for 5 s, then the card stops answering; 3 s
    more. (The logger's card-status busy-wait makes these 3 s slow to
    emulate.)"""
    img = _card(tmp_path_factory.mktemp("sd-dies"))
    with Sim(REPO / "systems" / "ams.yaml", images("ams"),
             params={"sd": {"image": str(img)}}, card_dirs=[img.parent]) as sim:
        ok = sim.io("ams").watch(*AMS_OK)
        _boot_armed(sim, 5000)
        logging = sim.read_symbol("ams", LOG_STATE)
        t = sim.now_us()
        sim.monitor(f"{CARD} Respond false", board="ams")
        sim.run_for(ms=3000)
        after = _health(sim, ok, since_us=t)
        after.log_state = sim.read_symbol("ams", LOG_STATE)
        after.ok_edges = sim.io("ams").edges(ok, since_us=t)
        after.fault = sim.read_symbol("ams", "g_fault_reason_telemetry")
    return SimpleNamespace(logging=logging, after=after)


def test_a_card_that_dies_mid_run_is_torn_down(dies):
    """The first write after the card dies fails and the logger tears down
    to io_error (sd_logger_task.cpp:356-365, :687/:750) instead of hanging
    in the transfer."""
    assert dies.logging == LOG_LOGGING
    assert dies.after.log_state in (3, LOG_NO_CARD), f"g_log_state {dies.after.log_state}"


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-AMS#619: a card that dies mid-run trips BmsStale. The failing write "
    "enters SD_CheckStatusWithTimeout (FATFS/Target/sd_diskio.c:138-156, from "
    "SD_write :431), which polls CMD13 without blocking for SD_TIMEOUT = 30 s "
    "(:57); the same wait follows on every remount, since FatFs keeps the "
    "drive initialised. SdLoggerTask is osPriorityLow (main.c:112-115) but the "
    "timer service task is lower (configTIMER_TASK_PRIORITY 2, "
    "FreeRTOSConfig.h:96), so the BMS poll timers (bms_poll_task.cpp:194-200, "
    ":800-807) stop firing, BmsPollTask waits forever on bms_events (:814-815) "
    "and SafetyTask latches Error, reason 3 = BmsStale (BmsStaleMs 350, "
    "ams_config.hpp:162): AMS_OK falls ~600 ms after the card dies."))
def test_a_card_that_dies_mid_run_leaves_the_ams_healthy(dies):
    assert dies.after.ok_edges == [], f"AMS_OK edges {dies.after.ok_edges}, fault {dies.after.fault}"
    _assert_healthy(dies.after, "card died mid-run")


def test_a_board_with_no_sd_device_boots_clean(images):
    """No card model at all and card detect left LOW: commands find no card
    and time out (CMDSENT / CTIMEOUT), so nothing spins on a missing CMDSENT
    (formerly CLAUDE.md invariant 8): healthy Start, logger at no_card."""
    sim = Sim(REPO / "systems" / "ams.yaml", images("ams"))
    del sim.system.devices["sd"]
    with sim:
        ok = sim.io("ams").watch(*AMS_OK)
        _boot_armed(sim, 6000)
        _assert_healthy(_health(sim, ok), "no sd device")
        assert sim.read_symbol("ams", LOG_STATE) == LOG_NO_CARD
        assert _unanswered(sim) > 0
