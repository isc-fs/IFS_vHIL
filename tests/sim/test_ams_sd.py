"""AMS SD logging through the SDMMC1 IDMA model (#21), read back from the card.

Firmware facts (IFS08-CE-AMS):
  FatFs on SDMMC1 by IDMA, single buffer: HAL_SD_ReadBlocks_DMA /
    HAL_SD_WriteBlocks_DMA via sd_diskio.c, bounce buffer in .sd_dma (RAM_D1)
  LOGnnnn.TMP is open while logging; it becomes LOGnnnn.CSV with a LOGnnnn.CRC
    sidecar at rotation (5 min / 4 MiB, ams_config.hpp:295-304) or, after a
    power cut, as an orphan sealed at the next boot (sd_logger_task.cpp:212-250)
  .CRC: 8 ASCII hex digits + newline, CRC-32 ISO-HDLC = zlib.crc32 (crc32.hpp)
  rows at 4 Hz (SafetyTask capture period 250 ms, ams_config.hpp:273); 314
    columns: tick_ms, FSM / pack fields, then c<m>_<cell> (5 x 19) and
    t<m>_<slot> (5 x 40)
  card detect PE3: LOW = card in; with the slot empty the carrier's pull-up
    holds it HIGH and BSP_SD_Init returns before touching SDMMC
    (fatfs_platform.c BSP_PlatformIsDetected); the logger retries the mount
    every tick and never blocks (sd_logger_task.cpp:1-20)
"""
import shutil
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from vhil.sim import Sim
from vhil.system import REPO

CARD_BYTES = 0x8000000          # catalog/models/sd-card.yaml capacity: 128 MiB
LOG_S, AFTER_CUT_S = 8, 6
RUNS, RUN_S = 3, 5
COLUMNS = 314
STATUS, TIMING = 0x4A0, 0x6C1


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


@pytest.fixture(scope="module")
def logged(tmp_path_factory, firmware, request):
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
    with Sim(system, {"ams": firmware("ams")}, params={"sd": {"image": str(img)}},
             log_path=log) as sim:
        sim.monitor("sysbus.spi1.isospi.cells3 SetCell 2 3650", board="ams")       # c1_11
        sim.monitor("sysbus.spi1.isospi.cells6 SetTemperature 16 400", board="ams")  # t3_10
        sim.run_for(ms=1500)                    # CAN up, listening for the arm
        sim.can("can_acu").send(0x7F0, bytes.fromhex("DEADBEEF"))
        sim.run_for(ms=LOG_S * 1000 - 1500)
        status = [f.t_us for f in sim.can("can_acu").frames(STATUS)]
        timing = sim.can("can_acu").last(TIMING).data
        cut_ms = sim.now_us() // 1000
        _cut(sim)
        sim.run_for(ms=AFTER_CUT_S * 1000)
    return SimpleNamespace(img=img, status=status, cut_ms=cut_ms,
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


def test_the_columns_carry_the_seeded_cell_and_temperature(card):
    """T-151: 314 columns; the seeded cell and NTC in their own columns,
    every other cell at the model default."""
    header, rows = _rows(card, "LOG0000.CSV")
    assert len(header) == COLUMNS, f"{len(header)} columns"
    assert all(r["c1_11"] == "3650" and r["c1_10"] == "3700" for r in rows)
    settled = rows[4:]                       # past the first temperature sweep
    assert all(r["t3_10"] == "40" and r["t3_11"] == "25" for r in settled)
    assert all(r["mod_mask"] == "31" for r in rows)


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
def runs(tmp_path_factory, firmware):
    """RUNS boots of RUN_S seconds on one card, each ended by a power cut."""
    img = _card(tmp_path_factory.mktemp("sd-runs"))
    with Sim(REPO / "systems" / "ams.yaml", {"ams": firmware("ams")},
             params={"sd": {"image": str(img)}}) as sim:
        for _ in range(RUNS):
            sim.run_for(ms=RUN_S * 1000)
            _cut(sim)
        sim.run_for(ms=3000)
    return img


def test_one_sealed_log_per_run(runs):
    """U-161, U-162: LOG0000..LOG0002 sealed with a matching .CRC each, the
    next one open, no stray files (AMS#495 fragmentation)."""
    files = _ls(runs)
    sealed = [f"LOG{i:04d}" for i in range(RUNS)]
    expected = sorted([f"{n}.CSV" for n in sealed] + [f"{n}.CRC" for n in sealed] + [f"LOG{RUNS:04d}.TMP"])
    assert files == expected, f"card holds {files}"
    for n in sealed:
        crc = int(_read(runs, f"{n}.CRC").decode().strip(), 16)
        assert crc == zlib.crc32(_read(runs, f"{n}.CSV")), f"{n}.CRC does not match"


def test_every_run_starts_fresh_and_keeps_the_mask(runs):
    """U-160: each log starts at its own boot and the module mask holds."""
    for i in range(RUNS):
        _, rows = _rows(runs, f"LOG{i:04d}.CSV")
        assert int(rows[0]["tick_ms"]) <= 1000, f"LOG{i:04d} starts at {rows[0]['tick_ms']} ms"
        assert len(rows) >= 2 * (RUN_S - 1)
        assert {r["mod_mask"] for r in rows} == {"31"}


def test_an_empty_slot_boots_clean(firmware):
    """S-143: card detect HIGH from reset: the AMS boots to a healthy Start,
    AMS_OK HIGH, telemetry on time, the logger idle."""
    with Sim(REPO / "systems" / "ams.yaml", {"ams": firmware("ams")}) as sim:
        sim.io("ams").set_input("sysbus.gpioPortE", 3, True)
        ok = sim.io("ams").watch("sysbus.gpioPortB", 4)
        sim.run_for(ms=6000)
        assert sim.read_symbol("ams", "g_state_telemetry") == 0
        assert sim.io("ams").level(ok), "AMS_OK low with the slot empty"
        t = [f.t_us for f in sim.can("can_acu").frames(STATUS)]
        assert t and t[0] <= 1_000_000
        assert all(abs((b - a) - 500_000) <= 10_000 for a, b in zip(t, t[1:]))
