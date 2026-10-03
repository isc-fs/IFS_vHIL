"""AMS SD logging through the SDMMC1 IDMA model (#21), read back from the card.

Firmware facts (IFS08-CE-AMS):
  FatFs on SDMMC1 by IDMA, single buffer: HAL_SD_ReadBlocks_DMA /
    HAL_SD_WriteBlocks_DMA via sd_diskio.c, bounce buffer in .sd_dma (RAM_D1)
  LOGnnnn.TMP is open while logging; it becomes LOGnnnn.CSV with a LOGnnnn.CRC
    sidecar at rotation (5 min / 4 MiB, ams_config.hpp:295-304) or, after a
    power cut, as an orphan sealed at the next boot (sd_logger_task.cpp:212-250)
  .CRC: 8 ASCII hex digits + newline, CRC-32 ISO-HDLC = zlib.crc32 (crc32.hpp)
  rows at 4 Hz (SafetyTask capture period 250 ms, ams_config.hpp:273)
"""
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from vhil.sim import Sim
from vhil.system import REPO

CARD_BYTES = 0x8000000          # catalog/models/sd-card.yaml capacity: 128 MiB
LOG_S, AFTER_CUT_S = 8, 6


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


@pytest.fixture(scope="module")
def card(tmp_path_factory, firmware, request):
    """Log for LOG_S on a fresh card, cut the power, boot again so the
    orphan .TMP is sealed, then stop: the card image is the result."""
    img = _card(tmp_path_factory.mktemp("sd"))
    system = REPO / "systems" / "ams.yaml"
    log_dir = request.config.getoption("--sim-log-dir")
    log = None
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log = Path(log_dir) / "ams-sd.log"
    with Sim(system, {"ams": firmware("ams")}, params={"sd": {"image": str(img)}},
             log_path=log) as sim:
        sim.run_for(ms=LOG_S * 1000)
        # Power cut as the virtual broker does it (vhil/broker.py).
        sim.monitor("machine Reset", board="ams")
        sim.monitor('cpu SetRegister "BasePri" 0x0', board="ams")
        sim.run_for(ms=AFTER_CUT_S * 1000)
    return img


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
