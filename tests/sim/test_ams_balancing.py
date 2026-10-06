"""ams-balancing (#33): passive balancing as the LTC6811s see it, the discharge
(DCC) bits read straight from the models, in virtual time.

AMS facts (IFS08-CE-AMS balance_controller.hpp, vehicle_service.cpp, ams_config.hpp):
  0x103 (DLC 4): "BALO" Off, "BALN" On in any state, "BALX" Auto (Charge
    only); any other payload is ignored and keeps the previous command.
    Never seen, or older than BalanceOverrideFreshMs (5 s): Off
    (vehicle_service.cpp effective_balance_cmd).
  0x104 "BALM" + module mask (DLC 5): modules to balance, 5 s fresh.
  A cell balances when above the pack's second-lowest cell by
    BalanceDeltaMv (50) and stops at BalanceStopDeltaMv (20); at most
    BalanceMaxActive = 8 per module, never two physically adjacent cells of
    one LTC; nothing while the hottest NTC is above BalanceTempMax (50 C).
  The mask is recomputed every BalanceUpdatePolls = 4 voltage polls (800 ms)
    and DCC is cleared for BalanceQuiesceMs before each ADCV.
  Module m = chips 2m (cells 0..8) and 2m+1 (cells 9..18); DCC bit i =
    cell i of that chip.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

BAL = 0x103
BALM = 0x104
UPDATE_MS = 2000            # two mask updates, whatever the phase


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=3000)
        yield sim


def _chip(ams, k, command):
    return ams.monitor(f"sysbus.spi1.isospi.cells{k} {command}", board="ams").strip()


def _dcc(ams, k):
    """DCC bits of chip k. The firmware clears them for ~2 ms around each
    ADCV, so take the widest of a few looks a few ms apart."""
    seen = 0
    for _ in range(3):
        seen |= int(_chip(ams, k, "DischargeBits"), 0)
        ams.run_for(ms=7)
    return seen


def _all_dcc(ams):
    return [_dcc(ams, k) for k in range(10)]


def _command(ams, magic: bytes):
    ams.can("can_acu").send_periodic("bal", BAL, magic, period_ms=1000)


def test_no_command_means_no_balancing(ams):
    """BAL-04: with 0x103 never seen the effective command is Off (IFS_HIL
    expects Auto: drift)."""
    _chip(ams, 2, "SetCell 4 3800")
    ams.run_for(ms=UPDATE_MS)
    assert _all_dcc(ams) == [0] * 10


def test_baln_discharges_the_high_cell_only(ams):
    """BALN balances in Start: +100 mV on chip 2 cell 4 sets that one bit."""
    _chip(ams, 2, "SetCell 4 3800")
    _command(ams, b"BALN")
    ams.run_for(ms=UPDATE_MS)
    expected = [0] * 10
    expected[2] = 1 << 4
    assert _all_dcc(ams) == expected


def test_balancing_has_hysteresis(ams):
    """Starts above +50 mV, holds down to +20 mV, stops at or below it."""
    _command(ams, b"BALN")
    _chip(ams, 3, "SetCell 2 3740")             # +40: never started
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 3) == 0
    _chip(ams, 3, "SetCell 2 3760")             # +60: starts
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 3) == 1 << 2
    _chip(ams, 3, "SetCell 2 3730")             # +30: holds
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 3) == 1 << 2
    _chip(ams, 3, "SetCell 2 3715")             # +15: stops
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 3) == 0


def test_at_most_eight_per_module_and_never_adjacent(ams):
    """A whole module 100 mV high: eight cells at most, no two neighbours on
    the same LTC."""
    _chip(ams, 2, "SetAllCells 3800")
    _chip(ams, 3, "SetAllCells 3800")
    _command(ams, b"BALN")
    ams.run_for(ms=UPDATE_MS)
    upper, lower = _dcc(ams, 2), _dcc(ams, 3)
    assert 0 < bin(upper).count("1") + bin(lower).count("1") <= 8, (bin(upper), bin(lower))
    for bits in (upper, lower):
        assert bits & (bits >> 1) == 0, f"adjacent cells discharging: {bits:#b}"
    assert [d for k, d in enumerate(_all_dcc(ams)) if k not in (2, 3)] == [0] * 8


def test_a_hot_pack_locks_balancing_out(ams):
    """Above 50 C anywhere: every switch off; back at 45 C it resumes."""
    _chip(ams, 2, "SetCell 4 3800")
    _command(ams, b"BALN")
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 2) == 1 << 4
    _chip(ams, 7, "SetTemperature 3 550")
    ams.run_for(ms=UPDATE_MS)
    assert _all_dcc(ams) == [0] * 10
    _chip(ams, 7, "SetTemperature 3 450")
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 2) == 1 << 4


def test_balo_stops_and_wrong_magic_keeps_the_command(ams):
    """Replaces IFS_HIL BAL B-05 (wrong magic); BAL-02: BALO clears every
    switch; an unknown payload changes nothing, so BALN stays in force
    after it."""
    _chip(ams, 2, "SetCell 4 3800")
    _command(ams, b"BALN")
    ams.run_for(ms=UPDATE_MS)
    ams.can("can_acu").stop_periodic("bal")
    ams.can("can_acu").send(BAL, b"BALZ")
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 2) == 1 << 4, "an unknown payload changed the command"
    ams.can("can_acu").send(BAL, b"BALO")
    ams.run_for(ms=UPDATE_MS)
    assert _all_dcc(ams) == [0] * 10


def test_auto_waits_for_charge(ams):
    """BAL-01, BAL-06: BALX outside Charge balances nothing."""
    _chip(ams, 2, "SetCell 4 3800")
    _command(ams, b"BALX")
    ams.run_for(ms=UPDATE_MS)
    assert _all_dcc(ams) == [0] * 10


def test_a_stale_command_falls_back_to_off(ams):
    """BAL-04: BALN sent once, then silence: Off after 5 s."""
    _chip(ams, 2, "SetCell 4 3800")
    ams.can("can_acu").send(BAL, b"BALN")
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 2) == 1 << 4
    ams.run_for(ms=5000)
    assert _all_dcc(ams) == [0] * 10


def test_balm_limits_balancing_to_its_modules(ams):
    """0x104 BALM without module 1: its high cell stays on, module 3's
    discharges."""
    _chip(ams, 2, "SetCell 4 3800")
    _chip(ams, 6, "SetCell 4 3800")
    _command(ams, b"BALN")
    ams.can("can_acu").send_periodic("balm", BALM, b"BALM" + bytes([0x1F & ~(1 << 1)]), period_ms=1000)
    ams.run_for(ms=UPDATE_MS)
    assert _dcc(ams, 2) == 0 and _dcc(ams, 6) == 1 << 4


def test_every_conversion_runs_with_the_switches_open(ams):
    """Quiesce: no ADCV ever reaches a chip with a discharge switch on."""
    _chip(ams, 2, "SetCell 4 3800")
    _command(ams, b"BALN")
    ams.run_for(ms=5000)
    assert _dcc(ams, 2) == 1 << 4
    assert int(_chip(ams, 2, "CommandCount 0x360"), 0) >= 20, "no cell conversions seen"
    assert int(_chip(ams, 2, "AdcvWhileDischarging"), 0) == 0
