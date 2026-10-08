"""DMA1 (models/renode/Stm32H7Dma.cs) and DBGMCU (Stm32H7Dbgmcu.cs) at
register level (#204), poked on an ECU at t = 0, before its first instruction.

Facts (RM0468 DMA controller chapter; addresses and bits from stm32h733xx.h):
  DMA1_BASE 0x40020000: LISR 0x00, LIFCR 0x08; stream n at 0x10 + 0x18 n:
    CR, NDTR, PAR, M0AR, M1AR, FCR. Stream flags in LISR at 0/6/16/22: FEIF
    0, DMEIF 2, TEIF 3, HTIF 4, TCIF 5.
  SxCR: EN 0, TEIE 2, HTIE 3, TCIE 4, DIR [7:6] (10 = memory-to-memory),
    PINC 9, MINC 10, PSIZE [12:11], MSIZE [14:13].
  Memory-to-memory moves the block at EN; HTIF at half, TCIF at the end, and
    a normal-mode stream clears EN itself. EN cleared by software sets TCIF.
  A transfer DMA1 cannot make (the DTCM, 0x20000000, is reached only by the
    CPU and the MDMA) sets TEIF and clears EN.
  DBGMCU_BASE 0x5C001000: IDCODE 0x00 read-only (DEV_ID 0x483, REV_ID
    0x1001 rev Z), CR 0x04 read/write (DBG_SLEEPD1 0, STOPD1 1, STANDBYD1 2).
"""
import pytest

from vhil import peripheral_guard as guard
from vhil.sim import Sim
from vhil.system import REPO

DMA1 = 0x40020000
LISR, LIFCR = DMA1 + 0x00, DMA1 + 0x08
EN, TEIE, HTIE, TCIE = 1 << 0, 1 << 2, 1 << 3, 1 << 4
M2M, PINC, MINC, WORDS = 2 << 6, 1 << 9, 1 << 10, (2 << 11) | (2 << 13)
TEIF, HTIF, TCIF = 1 << 3, 1 << 4, 1 << 5
SHIFT = {0: 0, 1: 6, 2: 16, 3: 22}
AXI = 0x24000000                     # AXI SRAM (RAM_D1), reachable by DMA1
DTCM = 0x20000000
DBGMCU = 0x5C001000


def _rd(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}").strip(), 16)


def _wr(sim, address, value):
    sim.monitor(f"sysbus WriteDoubleWord {address:#x} {value:#x}")


def _stream(n, reg):
    return DMA1 + 0x10 + 0x18 * n + {"CR": 0, "NDTR": 4, "PAR": 8, "M0AR": 0xC}[reg]


def _flags(sim, n):
    return (_rd(sim, LISR) >> SHIFT[n]) & 0x3D


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        yield sim


def test_a_memory_to_memory_block_moves_at_enable(ecu):
    src, dst = AXI, AXI + 0x100
    for i in range(4):
        _wr(ecu, src + 4 * i, 0x11111111 * (i + 1))
    _wr(ecu, _stream(2, "PAR"), src)
    _wr(ecu, _stream(2, "M0AR"), dst)
    _wr(ecu, _stream(2, "NDTR"), 4)
    _wr(ecu, _stream(2, "CR"), M2M | PINC | MINC | WORDS | TCIE | HTIE | EN)
    assert [_rd(ecu, dst + 4 * i) for i in range(4)] == [0x11111111 * (i + 1) for i in range(4)]
    assert _flags(ecu, 2) == TCIF | HTIF
    assert _rd(ecu, _stream(2, "CR")) & EN == 0, "normal mode: EN cleared at the end"
    assert _rd(ecu, _stream(2, "NDTR")) == 0
    _wr(ecu, LIFCR, (TCIF | HTIF) << SHIFT[2])
    assert _flags(ecu, 2) == 0


def test_the_dtcm_is_out_of_reach(ecu):
    _wr(ecu, _stream(3, "PAR"), DTCM)
    _wr(ecu, _stream(3, "M0AR"), AXI + 0x200)
    _wr(ecu, _stream(3, "NDTR"), 2)
    _wr(ecu, _stream(3, "CR"), M2M | PINC | MINC | WORDS | TEIE | EN)
    assert _flags(ecu, 3) == TEIF
    assert _rd(ecu, _stream(3, "CR")) & EN == 0
    assert _rd(ecu, _stream(3, "NDTR")) == 2, "nothing moved"


def test_a_software_disable_sets_tcif_and_unlocks_the_stream(ecu):
    """A peripheral-to-memory stream with no request yet: while enabled its
    configuration is write-protected; cleared, it reports TCIF."""
    _wr(ecu, _stream(1, "NDTR"), 8)
    _wr(ecu, _stream(1, "M0AR"), AXI)
    _wr(ecu, _stream(1, "CR"), MINC | TCIE | EN)
    _wr(ecu, _stream(1, "NDTR"), 3)
    assert _rd(ecu, _stream(1, "NDTR")) == 8, "NDTR is read-only while EN is set"
    _wr(ecu, _stream(1, "CR"), MINC)
    assert _flags(ecu, 1) == TCIF
    _wr(ecu, _stream(1, "NDTR"), 3)
    assert _rd(ecu, _stream(1, "NDTR")) == 3


def test_dbgmcu_idcode_is_read_only_and_cr_holds_its_bits(ecu):
    assert _rd(ecu, DBGMCU) == 0x10010483
    _wr(ecu, DBGMCU, 0)
    assert _rd(ecu, DBGMCU) == 0x10010483
    cr = _rd(ecu, DBGMCU + 4)
    _wr(ecu, DBGMCU + 4, cr | 0x7)    # HAL_DBGMCU_EnableDBGSleepMode and friends
    assert _rd(ecu, DBGMCU + 4) == cr | 0x7
    _wr(ecu, DBGMCU + 4, 0xFFFFFFFF)
    assert _rd(ecu, DBGMCU + 4) == 0x10700187, "reserved bits read 0"


def test_none_of_it_is_unmodelled(images, tmp_path):
    log_path = tmp_path / "renode.log"
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu"), log_path=log_path) as sim:
        test_a_memory_to_memory_block_moves_at_enable(sim)
        test_a_software_disable_sets_tcif_and_unlocks_the_stream(sim)
        test_dbgmcu_idcode_is_read_only_and_cr_holds_its_bits(sim)
    log = log_path.read_text(errors="replace")
    findings = guard.unexplained(log, guard.load_rules())
    assert not findings, guard.report(findings)
