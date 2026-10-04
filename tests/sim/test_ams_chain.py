"""ams-chain (#29): the isoSPI daisy chain failing and recovering, in virtual
time, with faults injected on the LTC6811 models.

AMS facts (IFS08-CE-AMS bms_service.cpp, safety_predicates.hpp, ams_config.hpp):
  An IC whose four RDCV groups are not all PEC-clean keeps its previous cells
  and counts a PEC error (g_ltc_pec_err_count[ic], bms_service.cpp:281-284);
  a module is fresh only on a poll where both its ICs were clean, and drops
  from module_online_mask once older than BmsStaleMs (350 ms, :312-338).
  VoltPollRetries = 2: a poll re-reads up to twice before it counts as failed.
  BmsModuleOffline (reason 2, detail = online mask) is checked before
  BmsStale (3), so a silent module always reports 2 (safety_predicates.hpp:
  176-183). FS rule: losing a cell measurement opens the SDC in < 500 ms.
  Pit-diag (armed by 0x7F0 DE AD BE EF, 1 Hz): 0x680.. cells (BE u16, cell
  index = 4 * (id - 0x680) + slot, module = index / 19); 0x6C0 byte 0 state,
  6 reason, 7 detail; 0x6C1 bytes 0-1 / 2-3 last / max voltage-poll ms (BE);
  0x6C7 / 0x6C8 per-IC PEC counts (saturating u8, ICs 0..7 / 8..9).
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

PIT_ARM, PIT_CELLS, PIT_FSM, PIT_TIMING, PEC_A, PEC_B = 0x7F0, 0x680, 0x6C0, 0x6C1, 0x6C7, 0x6C8
START, ERROR = 0, 5
OFFLINE = 2


@pytest.fixture
def ams(firmware):
    with Sim(REPO / "systems" / "ams.yaml", {"ams": firmware("ams")}) as sim:
        sim.run_for(ms=1500)
        sim.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))
        sim.run_for(ms=2500)                              # past boot grace
        yield sim


def _chip(ams, k, command):
    return ams.monitor(f"sysbus.spi1.isospi.cells{k} {command}", board="ams")


def _fsm(ams, ms=1500):
    ams.run_for(ms=ms)
    f = ams.can("can_acu").last(PIT_FSM)
    return f.data[0], f.data[6], f.data[7]


def _pec_counts(ams):
    can = ams.can("can_acu")
    return list(can.last(PEC_A).data) + list(can.last(PEC_B).data[:2])


def _cell(ams, module, cell):
    index = module * 19 + cell
    f = ams.can("can_acu").last(PIT_CELLS + index // 4)
    slot = index % 4
    return int.from_bytes(f.data[2 * slot:2 * slot + 2], "big")


def _ms_to_error(ams, limit_ms=500, step_ms=25):
    for elapsed in range(step_ms, limit_ms + step_ms, step_ms):
        ams.run_for(ms=step_ms)
        if ams.read_symbol("ams", "g_state_telemetry") == ERROR:
            return elapsed
    return None


def test_a_healthy_chain_polls_clean_and_fast(ams):
    """E-061, E-063: no PEC errors on any IC, and the voltage poll (ten
    chips, retries included) stays inside its 50 ms budget."""
    ams.run_for(ms=3000)
    assert _fsm(ams) == (START, 0, 0)
    assert _pec_counts(ams) == [0] * 10
    timing = ams.can("can_acu").last(PIT_TIMING).data
    last, worst = int.from_bytes(timing[0:2], "big"), int.from_bytes(timing[2:4], "big")
    assert 0 < last <= worst < 50, f"voltage poll last {last} ms, max {worst} ms"


@pytest.mark.parametrize("chips, online", [
    ([4], 0x1B),            # B-029-stale: one chip of module 2
    ([4, 5], 0x1B),         # E-066: the whole module
    ([9], 0x0F),            # the far end of the chain
], ids=["chip4", "module2", "chip9"])
def test_a_silent_chip_takes_its_module_offline_in_time(ams, chips, online):
    """B-022, E-065, E-066: reason 2 (never BmsStale: offline is checked
    first) with the online mask, inside the FS 500 ms. IFS_HIL asserts
    reason 3 for B-029-stale: drift."""
    for k in chips:
        _chip(ams, k, "Respond false")
    elapsed = _ms_to_error(ams)
    assert elapsed is not None, "no Error within 500 ms of the module going silent"
    assert ams.read_symbol("ams", "g_fault_reason_telemetry") == OFFLINE
    assert _fsm(ams) == (ERROR, OFFLINE, online)


def test_a_cut_link_silences_everything_beyond_it(ams):
    """G-102: cutting the cable after chip 5 loses modules 3 and 4; chip 5
    itself still answers, so module 2 stays online."""
    _chip(ams, 5, "BreakDownstream true")
    assert _ms_to_error(ams) is not None, "no Error within 500 ms of the cut"
    assert _fsm(ams) == (ERROR, OFFLINE, 0x07)


def test_the_chain_recovers_when_the_link_returns(ams):
    """Behind a cut the far modules' cells read 0xFFFF (no data) on 0x680;
    once it heals they report fresh cells again (the FSM stays latched in
    Error; recovery is the data, not the state)."""
    _chip(ams, 7, "BreakDownstream true")
    _chip(ams, 8, "SetCell 0 3650")
    ams.run_for(ms=1500)
    assert _cell(ams, 4, 0) == 0xFFFF, "module 4 not reported as no-data behind a cut link"
    _chip(ams, 7, "BreakDownstream false")
    ams.run_for(ms=1500)
    assert _cell(ams, 4, 0) == 3650, "module 4 did not recover after the link returned"
    assert _fsm(ams)[:2] == (ERROR, OFFLINE), "the latch cleared"


def test_pec_errors_are_counted_on_the_faulty_chip_only(ams):
    """E-067: a chip whose reply PEC is always bad counts errors alone, and
    its module goes offline like a silent one."""
    _chip(ams, 7, "CorruptPec true")
    ams.run_for(ms=1500)
    counts = _pec_counts(ams)
    assert counts[7] > 0, f"no PEC errors on chip 7: {counts}"
    assert [c for i, c in enumerate(counts) if i != 7] == [0] * 9, f"PEC errors leaked: {counts}"
    assert _fsm(ams)[:2] == (ERROR, OFFLINE)
    assert _fsm(ams)[2] == 0x1F & ~(1 << 3)


def test_a_transient_pec_burst_is_absorbed_by_the_retries(ams):
    """VoltPollRetries = 2: two corrupted reads in a row are re-read inside
    the same poll; the error is counted, the module never goes stale."""
    for _ in range(5):
        _chip(ams, 3, "CorruptNextReplies 2")
        ams.run_for(ms=400)
    assert _pec_counts(ams)[3] > 0, "the corruption never reached the host"
    assert _fsm(ams) == (START, 0, 0), "a transient burst faulted the AMS"
