"""ams-cells (#27): the cell-voltage predicates at their boundaries, in virtual
time, with cells set on the LTC6811 models.

AMS facts (IFS08-CE-AMS ams_config.hpp, safety_predicates.hpp):
  CellUnderVoltageMv = 2800 (reason 4), CellOverVoltageMv = 4200 (reason 5);
    the detail byte names the module (B-029: module 3 -> (4, 3)).
  CellFaultConfirmTicks = 25 (~250 ms) against the 200 ms voltage poll: a
    reading must stay bad across two polls to latch.
  Tap-artifact guard: two adjacent cells, one beyond CellImplausibleMax/Min
    (4400 / 1500 mV), split by >= TapArtifactMinSplitMv (800) with their sum
    conserved, are averaged into the aggregates, not faulted; a single
    glitch beside a normal neighbour still faults.
  BmsModuleOffline = reason 2, detail = module_online_mask: the modules
    still ONLINE (safety_predicates.hpp:176-178).
  Chip k is module k // 2 (upper IC even, lower odd); 0x6C0 (1 Hz once 0x7F0
  DE AD BE EF arms it): byte 0 state (5 = Error), 6 reason, 7 detail.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

PIT_ARM, PIT_FSM = 0x7F0, 0x6C0
START, ERROR = 0, 5
UNDER, OVER, OFFLINE = 4, 5, 2


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=1500)
        sim.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))
        sim.run_for(ms=2500)                              # past boot grace
        yield sim


def _chip(ams, k, command):
    return ams.monitor(f"sysbus.spi1.isospi.cells{k} {command}", board="ams")


def _fsm(ams, ms=1500):
    """(state, reason, detail) from a fresh 0x6C0 after ms."""
    ams.run_for(ms=ms)
    f = ams.can("can_acu").last(PIT_FSM)
    return f.data[0], f.data[6], f.data[7]


def test_a_healthy_pack_reads_no_fault(ams):
    assert _fsm(ams) == (START, 0, 0)


@pytest.mark.parametrize("mv, expected", [
    (4190, (START, 0)), (4210, (ERROR, OVER)),
    (2810, (START, 0)), (2790, (ERROR, UNDER)),
], ids=["4190", "4210", "2810", "2790"])
def test_cell_limits_and_reason(ams, mv, expected):
    """Replaces IFS_HIL B-026 (over/undervoltage) and B-029 (cell_under/
    overvoltage_reason): just inside the limit is healthy, just past it
    faults with its reason and the module in the detail byte (chip 6 =
    module 3)."""
    _chip(ams, 6, f"SetCell 4 {mv}")
    state, reason, detail = _fsm(ams)
    assert (state, reason) == expected
    if state == ERROR:
        assert detail == 3, f"detail {detail}, expected module 3"


def test_a_dip_shorter_than_a_poll_does_not_latch(ams):
    """Replaces IFS_HIL B-028 (transient dip): under-voltage seen by at most
    one 200 ms poll stays below the ~250 ms confirmation."""
    _chip(ams, 2, "SetCell 1 2600")
    ams.run_for(ms=150)
    _chip(ams, 2, "SetCell 1 3700")
    assert _fsm(ams)[:2] == (START, 0)


def test_a_sustained_dip_latches(ams):
    """Replaces IFS_HIL B-028 (sustained dip)."""
    _chip(ams, 2, "SetCell 1 2600")
    ams.run_for(ms=600)                                   # three polls
    _chip(ams, 2, "SetCell 1 3700")
    assert _fsm(ams)[:2] == (ERROR, UNDER), "a sustained dip did not latch"
    assert _fsm(ams)[0] == ERROR, "the latch did not stick"


def test_a_shifted_tap_between_adjacent_cells_is_averaged(ams):
    """Tap-artifact guard: 4500 + 2900 (sum conserved, split 1600) on adjacent
    cells reads as two 3700 cells: no fault."""
    _chip(ams, 4, "SetCell 2 4500")
    _chip(ams, 4, "SetCell 3 2900")
    assert _fsm(ams, ms=2000)[:2] == (START, 0)


def test_a_single_glitch_beside_a_normal_cell_still_faults(ams):
    """The guard needs the pair's sum conserved: 4500 beside 3700 faults OV."""
    _chip(ams, 4, "SetCell 2 4500")
    assert _fsm(ams, ms=2000)[:2] == (ERROR, OVER)


def test_a_silent_chip_is_reported_offline(ams):
    """B-029 (BMS stale): a chip that stops answering takes its module
    offline: reason 2, and the detail lists the modules still online, all
    but module 2 (IFS_HIL asserts reason 3: drift)."""
    _chip(ams, 4, "Respond false")
    state, reason, detail = _fsm(ams, ms=2000)
    assert (state, reason) == (ERROR, OFFLINE)
    assert detail == 0x1F & ~(1 << 2), f"online mask 0x{detail:02X}"
