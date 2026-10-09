"""ams-spi-faults (#196 section 1): SPI1 / isoSPI transfers that fail, and the
firmware's reaction, in virtual time. Nothing in the image is patched: the
SPI1 model stalls a transfer (models/renode/Stm32H7Spi.cs, SCK never runs, EOT
never sets, the HAL times out), or an LTC6811 rejects a WRCFGA's data
(models/renode/IsoSpi.cs, CorruptNextConfigWrites). A CPU hook at the entry
of a HAL or driver function only decides WHEN the hardware fails: it arms the
model for the transfer about to start, as test_ecu_robustness.py's hooks do.

AMS facts (IFS08-CE-AMS dev ec8ab44, Core/Src/app/ unless shown):
  ltc6820.cpp:113-143 Bus::transfer, :149-181 read_register_group: a HAL
    status other than HAL_OK fails the call; SpiTimeoutMs = 10 (:26).
  bms_poll_task.cpp attempt_voltage_poll: a failed ADCV (:301-304), RDCFGA
    warm-up (:324-328) or RDCVA..D (:341-349) bumps g_ltc_spi_err_count and
    returns {false, 0} without digesting ("partial reply -> don't poison
    BmsService state"); run_voltage_poll retries up to VoltPollRetries = 2
    (:593-597, ams_config.hpp:1016). adow_pass (:446, :454, :463) fails the
    same way; attempt_open_wire_poll retries OpenWireRetries = 1 (:487-497).
  Two failed polls in a row and recover_chain (:203-222) re-wakes the chain
    and rewrites CFGR before every later poll (:574-576),
    g_ltc_chain_recover_count.
  run_temperature_poll: a failed WRCOMM / STCOMM / ADAX / RDAUXA (:827-853)
    bumps g_ltc_spi_err_count, marks the channel in the sweep's failure mask
    and skips to the next channel; g_temp_sweep_last_mask is the last
    sweep's, g_temp_sweep_sticky_mask OR-accumulates (:756-771, :870-871).
  quiesce_balancing (:372-413): with balancing active, WRCFGA all-zero DCC,
    retried once; both failing sets g_balance_quiesce_fail, bumps
    g_balance_quiesce_fail_count and measures anyway (:400-408, deliberate).
    maybe_run_balance_update then holds the previous mask for one window
    (:650-655): balance State::Holding = 3, inhibit QuiesceFailHold = 0x80
    (balance_controller.hpp:103, 318-324, 401-404).
  restore_balancing (:416-425): a failed restore leaves balancing off until
    the next update (s_balance_active = false), "it simply resumes next cycle".
  maybe_run_balance_update's WRCFGA (:721-725): a failed mask write bumps the
    error count and returns before caching the mask, so the previous one
    stays the restore's (:727-733); the next update re-derives it.
  FS rule: losing a cell measurement opens the SDC in < 500 ms; BmsStaleMs =
    350, BmsModuleOffline = reason 2 (test_ams_chain.py).
"""
import re

import pytest

from vhil import elf
from vhil.sim import Sim
from vhil.system import REPO

BAL = 0x103
STATUS = 0x4A0                  # byte 2: module_online_mask (500 ms, ungated)
START, ERROR = 0, 5
STALE = 3
ACTIVE, HOLDING = 2, 3          # balance::State
QUIESCE_FAIL_HOLD = 0x80        # balance::inhibit::QuiesceFailHold

WRCFGA = 0x001
# LTC6811 opcodes, and the bits fixed in each (IsoSpi.cs IsAdcv/IsAdow/IsAdax)
ADCV = ("(op & 0x668) == 0x260", 0x360)
ADOW = ("(op & 0x628) == 0x228", 0x368)
ADAX = ("(op & 0x678) == 0x460", 0x561)
RDCFGA, RDCVA, RDCVB, RDCVC, RDCVD, RDAUXA = 0x002, 0x004, 0x006, 0x008, 0x00A, 0x00C
WRCOMM, STCOMM = 0x721, 0x723

BALANCED_CHIP, BALANCED_BIT = 2, 1 << 4


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=4000)                                # past boot grace
        yield sim


@pytest.fixture
def balancing(ams):
    """Chip 2 cell 4 100 mV high and BALN in force: that one switch bleeds."""
    _chip(ams, BALANCED_CHIP, "SetCell 4 3800")
    ams.can("can_acu").send_periodic("bal", BAL, b"BALN", period_ms=1000)
    ams.run_for(ms=2000)
    assert _dcc_seen(ams, BALANCED_CHIP) == BALANCED_BIT, "balancing never started"
    return ams


# -- helpers -------------------------------------------------------------------

def _chip(ams, k, command):
    return ams.monitor(f"sysbus.spi1.isospi.cells{k} {command}", board="ams").strip()


def _int(ams, path_command):
    return int(ams.monitor(path_command, board="ams").strip(), 0)


def _sym(ams, name, size=4):
    return ams.read_symbol("ams", name, size)


def _fsm(ams):
    return _sym(ams, "g_state_telemetry", 1), _sym(ams, "g_fault_reason_telemetry", 1)


def _dcc_seen(ams, k):
    """DCC bits of chip k, the widest of a few looks: the firmware clears them
    for ~2 ms around each conversion."""
    seen = 0
    for _ in range(3):
        seen |= int(_chip(ams, k, "DischargeBits"), 0)
        ams.run_for(ms=7)
    return seen


def _addr(ams, symbol):
    return elf.symbol(ams.firmware["ams"], symbol)[0] & ~1


def _hook(ams, symbol, python):
    # No "(name) " in the command: the monitor client takes "(word) " at the
    # end of what it has read so far for Renode's prompt (vhil/renode.py
    # _PROMPT), so its echo could end the read early. Python needs no space
    # after a closing parenthesis.
    python = re.sub(r"\)\s+", ")", python)
    at = _addr(ams, symbol)
    ams.monitor(f'cpu AddHook {at:#x} "{python}"', board="ams")
    return at


def _unhook(ams, *addresses):
    for at in addresses:
        ams.monitor(f"cpu RemoveHooksAt {at:#x}", board="ams")


def _stalled(ams):
    return _int(ams, "sysbus.spi1 StalledTransfers")


SPI = "machine['sysbus.spi1']"


def _stall_once_if(cond, limit):
    """Hook expression: stall the transfer about to start if `cond` holds and
    the model has stalled fewer than `limit` transfers so far (the count is
    the model's, so the hook keeps no state)."""
    return (f"{SPI}.StallNextTransfers(1) if ({cond}) and "
            f"{SPI}.StalledTransfers + {SPI}.PendingStalls < {limit} else None")


def _stall_command_once(ams, cond):
    """Arm a stall on the next HAL SPI transfer whose LTC6811 command (the
    frame's first two bytes, r1 = pTxData in both HAL calls) meets `cond`."""
    decode = ("b = machine.SystemBus; p = self.GetRegister(1).RawValue; "
              "op = ((b.ReadByte(p) & 7) << 8) | b.ReadByte(p + 1); ")
    python = decode + _stall_once_if(cond, _stalled(ams) + 1)
    return [_hook(ams, "HAL_SPI_Transmit", python), _hook(ams, "HAL_SPI_TransmitReceive", python)]


def _wrcfga_hook(ams, cond, action):
    """A hook on Bus::write_chain_command (r1 = cmd, r2 = the per-IC payloads,
    6 bytes each; DCC in payload bytes 4 and 5) that runs `action` for a
    WRCFGA meeting `cond`. `dcc` is the OR of every IC's DCC bits, `upd`
    whether the caller is maybe_run_balance_update (the mask write) rather
    than the voltage poll (quiesce, restore, chain recovery)."""
    u0, size = elf.symbol(ams.firmware["ams"], "_ZN12_GLOBAL__N_124maybe_run_balance_updateEv")
    u0 &= ~1
    python = ("b = machine.SystemBus; q = self.GetRegister(2).RawValue; lr = self.LR.RawValue & ~1; "
              f"upd = {u0:#x} <= lr < {u0 + size:#x}; "
              "dcc = sum(b.ReadByte(q + 6 * i + 4) | ((b.ReadByte(q + 6 * i + 5) & 15) << 8) for i in range(10)); "
              f"({action}) if self.GetRegister(1).RawValue == {WRCFGA} and ({cond}) else None")
    return _hook(ams, "_ZN3ams7ltc68203Bus19write_chain_commandEtPA6_Kh", python)


def _wait_for(ams, k, opcode, timeout_ms=400):
    """Run until chip k has just seen `opcode` once more."""
    before = int(_chip(ams, k, f"CommandCount {opcode:#x}"), 0)
    ams.run_until(lambda: int(_chip(ams, k, f"CommandCount {opcode:#x}"), 0) > before,
                  timeout_ms=timeout_ms, step_ms=0.5)


def _after_one_stall(ams, hooks, before, timeout_ms=600):
    """Run until the armed stall has happened and the firmware counted it,
    then take the hooks off; returns g_ltc_spi_err_count's increase."""
    errors = _sym(ams, "g_ltc_spi_err_count")
    try:
        ams.run_until(lambda: _stalled(ams) > before, timeout_ms=timeout_ms, step_ms=1)
        ams.run_for(ms=15)                                  # past the HAL's 10 ms timeout
    finally:
        _unhook(ams, *hooks)
    return _sym(ams, "g_ltc_spi_err_count") - errors


# -- the hook itself -------------------------------------------------------------

def test_a_stalled_transfer_never_reaches_the_chain(ams):
    """The SPI1 model's fault: SCK never runs, so the LTC6820 sees its chip
    select toggle with no clocks (a wake pulse) and no chip decodes the
    command; the HAL times out, so the driver counts an error."""
    before = int(_chip(ams, 0, "CommandCount 0x360"), 0)
    wakes = int(ams.monitor("sysbus.spi1.isospi Stats", board="ams").split("wakes=")[1].split()[0])
    hooks = _stall_command_once(ams, ADCV[0])
    assert _after_one_stall(ams, hooks, _stalled(ams)) == 1
    assert _stalled(ams) >= 1
    after = int(ams.monitor("sysbus.spi1.isospi Stats", board="ams").split("wakes=")[1].split()[0])
    assert after == wakes + 1, "the stalled ADCV's chip-select window was not a wake"
    # The retry's ADCV went through: one conversion per poll, as before.
    assert int(_chip(ams, 0, "CommandCount 0x360"), 0) > before


# -- every bus-error branch, one stalled transfer at a time ----------------------

VOLTAGE_POLL = {
    "adcv": ADCV[0],
    "rdcfga_warmup": f"op == {RDCFGA}",
    "rdcva": f"op == {RDCVA}",
    "rdcvb": f"op == {RDCVB}",
    "rdcvc": f"op == {RDCVC}",
    "rdcvd": f"op == {RDCVD}",
    "adow": ADOW[0],
}


@pytest.mark.parametrize("cond", VOLTAGE_POLL.values(), ids=VOLTAGE_POLL.keys())
def test_one_failed_voltage_poll_transfer_is_retried_inside_the_poll(ams, cond):
    """Each bus error in attempt_voltage_poll (ADCV :301-304, the RDCFGA
    warm-up :324-328, RDCVA..D :341-349) and in the open-wire scan's
    adow_pass (:446) is counted once and retried inside the same poll
    (VoltPollRetries, OpenWireRetries): no module goes stale and the AMS
    stays in Start. Armed just after a poll's ADCV, so the transfer hit is
    the next poll's (adcv) or this poll's."""
    _wait_for(ams, 0, ADCV[1])
    hooks = _stall_command_once(ams, cond)
    assert _after_one_stall(ams, hooks, _stalled(ams)) == 1
    ams.run_for(ms=1500)
    assert _fsm(ams) == (START, 0)
    assert ams.can("can_acu").last(STATUS).data[2] == 0x1F, "a module went offline"


# Never the sweep's warm-up select of S32 (:803-813), whose errors the
# firmware ignores on purpose: WRCOMM's own payload (COMM0/COMM1 of the first
# IC, bytes 4-5 of the frame) or, for STCOMM, what chip 0's COMM holds.
WARM_UP = 31
TEMPERATURE_SWEEP = {
    "wrcomm": f"op == {WRCOMM} and ((((b.ReadByte(p + 4) & 15) << 4) | (b.ReadByte(p + 5) >> 4)) & 31) != {WARM_UP}",
    "stcomm": f"op == {STCOMM} and machine['sysbus.spi1.isospi.cells0'].CommMuxAddress != {WARM_UP}",
    "adax": ADAX[0],
    "rdauxa": f"op == {RDAUXA}",
}


@pytest.mark.parametrize("cond", TEMPERATURE_SWEEP.values(), ids=TEMPERATURE_SWEEP.keys())
def test_one_failed_sweep_transfer_skips_its_channel_only(ams, cond):
    """A bus error in the temperature sweep (WRCOMM :827-829, STCOMM :831-833,
    ADAX :842-844, RDAUXA :851-853) costs exactly that channel this sweep:
    counted once, one bit in the sweep's failure mask (sticky too), the next
    sweep clean again, and no fault. The transfer hit is a channel's, never
    the warm-up's (:803-813, whose errors are deliberately ignored)."""
    sticky = _sym(ams, "g_temp_sweep_sticky_mask")
    assert sticky == 0, f"sweep failures before the fault: {sticky:#x}"
    hooks = _stall_command_once(ams, cond)
    assert _after_one_stall(ams, hooks, _stalled(ams)) == 1
    ams.run_for(ms=600)                                     # that sweep ends, a clean one runs
    sticky = _sym(ams, "g_temp_sweep_sticky_mask")
    assert bin(sticky).count("1") == 1, f"sticky sweep-failure mask {sticky:#x}"
    ams.run_for(ms=600)
    assert _sym(ams, "g_temp_sweep_last_mask") == 0, "the failure outlived its sweep"
    assert _fsm(ams) == (START, 0)


# -- a dead SPI ------------------------------------------------------------------

def test_a_dead_spi_faults_bms_stale_in_time_and_the_chain_recovers(ams):
    """Every transfer stalls: no poll completes, so BmsService is never
    updated and every module's last_rx_tick ages. BmsStale (reason 3; the
    online mask, re-derived only from a completed read, bms_service.cpp:
    314-338, still says all online, so BmsModuleOffline cannot fire first,
    safety_predicates.hpp:176-183) opens the SDC within the FS 500 ms.
    recover_chain re-wakes the chain before each later poll (:574-576). When
    the bus comes back the polls complete again and recovery stops (the FSM
    stays latched)."""
    errors = _sym(ams, "g_ltc_spi_err_count")
    recoveries = _sym(ams, "g_ltc_chain_recover_count")
    ams.monitor("sysbus.spi1 Stall true", board="ams")
    for elapsed in range(25, 525, 25):
        ams.run_for(ms=25)
        if _sym(ams, "g_state_telemetry", 1) == ERROR:
            break
    else:
        pytest.fail("no Error within 500 ms of SPI1 dying")
    assert _sym(ams, "g_fault_reason_telemetry", 1) == STALE, f"reason after {elapsed} ms"
    ams.run_for(ms=600)
    assert _sym(ams, "g_ltc_spi_err_count") > errors
    assert _sym(ams, "g_ltc_chain_recover_count") > recoveries, "recover_chain never ran"
    ams.monitor("sysbus.spi1 Stall false", board="ams")
    ams.run_for(ms=1000)
    errors = _sym(ams, "g_ltc_spi_err_count")
    recoveries = _sym(ams, "g_ltc_chain_recover_count")
    ams.run_for(ms=1000)
    assert _sym(ams, "g_ltc_spi_err_count") == errors, "the bus still fails"
    assert _sym(ams, "g_ltc_chain_recover_count") == recoveries, "polls still failing"
    assert ams.can("can_acu").last(STATUS).data[2] == 0x1F
    assert _fsm(ams) == (ERROR, STALE)


# -- balancing: quiesce, restore, mask write ---------------------------------------

def _fail_next_quiesce(ams):
    """Stall both WRCFGA attempts of the next quiesce (all-zero DCC, from the
    voltage poll); returns the virtual time the firmware gave up (us)."""
    fails = _sym(ams, "g_balance_quiesce_fail_count")
    hook = _wrcfga_hook(ams, "dcc == 0 and not upd", _stall_once_if("True", _stalled(ams) + 2))
    try:
        ams.run_until(lambda: _sym(ams, "g_balance_quiesce_fail_count") > fails,
                      timeout_ms=400, step_ms=1)
    finally:
        _unhook(ams, hook)
    return ams.now_us()


def test_a_failed_quiesce_is_counted_and_holds_the_selector(balancing):
    """BALANCE-1, what the firmware does today and means to: both WRCFGA
    attempts fail (:394-398, two errors), the poll is flagged
    (g_balance_quiesce_fail_count) and measures anyway, under bleed, since
    the zero mask never reached the chain (:400-408, deliberate: starving the
    predicates is worse). The next balance update holds the mask instead of
    ranking those voltages (Holding, QuiesceFailHold; :650-655), then
    balancing goes on with the same selection. No fault."""
    ams = balancing
    errors = _sym(ams, "g_ltc_spi_err_count")
    under_bleed = int(_chip(ams, BALANCED_CHIP, "AdcvWhileDischarging"), 0)
    assert under_bleed == 0
    _fail_next_quiesce(ams)
    assert _sym(ams, "g_ltc_spi_err_count") - errors == 2
    ams.run_for(ms=20)
    assert int(_chip(ams, BALANCED_CHIP, "AdcvWhileDischarging"), 0) > under_bleed, \
        "the poll did not measure after the failed quiesce"
    held = []
    for _ in range(100):                                    # the next update, <= 800 ms
        ams.run_for(ms=10)
        if _sym(ams, "g_balance_state", 1) == HOLDING:
            held.append(_sym(ams, "g_balance_inhibit", 2))
            break
    assert held, "the balance update never held after the failed quiesce"
    assert held[0] & QUIESCE_FAIL_HOLD
    ams.run_for(ms=1000)
    assert _sym(ams, "g_balance_state", 1) == ACTIVE
    assert _dcc_seen(ams, BALANCED_CHIP) == BALANCED_BIT
    assert _fsm(ams) == (START, 0)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "IFS08-CE-AMS#631: run_voltage_poll runs the open-wire scan "
    "(attempt_open_wire_poll, bms_poll_task.cpp:604) whether or not the "
    "quiesce succeeded, although :599-600 says it runs 'while balancing is "
    "STILL quiesced' (FMEA BALANCE-1, point 2)"))
def test_a_failed_quiesce_keeps_the_open_wire_scan_off_the_bleeding_chain(balancing):
    """After a quiesce that could not be proven, no ADOW reaches a chip with a
    discharge switch on: bleed current shifts the pull-up/down delta exactly
    as it shifts the cell reading (:599-600, open_wire.hpp)."""
    ams = balancing
    before = int(_chip(ams, BALANCED_CHIP, "AdowWhileDischarging"), 0)
    _fail_next_quiesce(ams)
    ams.run_for(ms=50)                                      # that poll's ADOW passes
    assert int(_chip(ams, BALANCED_CHIP, "AdowWhileDischarging"), 0) == before


def test_a_failed_restore_leaves_balancing_off_until_the_next_update(balancing):
    """restore_balancing (:416-425): the mask write after a measurement
    fails, so the chain stays quiesced (DCC 0), counted once; the next
    800 ms update rewrites the mask and the cell bleeds again."""
    ams = balancing
    before = _stalled(ams)
    hook = _wrcfga_hook(ams, "dcc != 0 and not upd", _stall_once_if("True", before + 1))
    assert _after_one_stall(ams, [hook], before) == 1
    assert int(_chip(ams, BALANCED_CHIP, "DischargeBits"), 0) == 0, "the failed restore landed"
    ams.run_until(lambda: int(_chip(ams, BALANCED_CHIP, "DischargeBits"), 0) == BALANCED_BIT,
                  timeout_ms=1000, step_ms=5)
    assert _fsm(ams) == (START, 0)


def test_a_failed_mask_write_is_redone_by_the_next_update(balancing):
    """maybe_run_balance_update (:721-725): the cell is back in line, so the
    update's mask is empty, but its WRCFGA fails. The mask is not cached, so
    the polls keep restoring the previous one, which is what is still on the
    chain (:643-649, :727-733); the next update clears it."""
    ams = balancing
    before = _stalled(ams)
    cycles = _sym(ams, "g_balance_cycles_total_pub")
    _chip(ams, BALANCED_CHIP, "SetCell 4 3700")
    hook = _wrcfga_hook(ams, "upd", _stall_once_if("True", before + 1))
    assert _after_one_stall(ams, [hook], before, timeout_ms=1000) == 1
    assert _sym(ams, "g_balance_cycles_total_pub") == cycles, "a failed write counted as a cycle"
    assert _dcc_seen(ams, BALANCED_CHIP) == BALANCED_BIT, "the previous mask was not kept"
    ams.run_until(lambda: _dcc_seen(ams, BALANCED_CHIP) == 0, timeout_ms=1200, step_ms=20)
    assert _fsm(ams) == (START, 0)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "IFS08-CE-AMS#632: quiesce_balancing "
    "(bms_poll_task.cpp:394-412) takes HAL_OK as proof that DCC is off and "
    "never reads CFGR back, so a WRCFGA an LTC6811 discards (bad data PEC) "
    "is a successful quiesce and the conversion runs under bleed"))
def test_a_quiesce_a_chip_rejects_is_not_trusted(balancing):
    """The LTC6811 discards a write whose data PEC does not match, and says
    nothing: the zero mask reaches the bus but not chip 2, whose switch stays
    on. The quiesce is the only full stop (:386-392); the firmware must not
    convert on that chip as if it were quiet, or must at least flag the poll
    as it does for a bus error (g_balance_quiesce_fail_count)."""
    ams = balancing
    rejected = int(_chip(ams, BALANCED_CHIP, "RejectedWrites"), 0)
    under_bleed = int(_chip(ams, BALANCED_CHIP, "AdcvWhileDischarging"), 0)
    fails = _sym(ams, "g_balance_quiesce_fail_count")
    cells = f"machine['sysbus.spi1.isospi.cells{BALANCED_CHIP}']"
    hook = _wrcfga_hook(ams, f"dcc == 0 and not upd and {cells}.RejectedWrites == {rejected}",
                        f"{cells}.CorruptNextConfigWrites(1)")
    try:
        ams.run_until(lambda: int(_chip(ams, BALANCED_CHIP, "RejectedWrites"), 0) > rejected,
                      timeout_ms=400, step_ms=1)
    finally:
        _unhook(ams, hook)
    ams.run_for(ms=20)
    flagged = _sym(ams, "g_balance_quiesce_fail_count") > fails
    quiet = int(_chip(ams, BALANCED_CHIP, "AdcvWhileDischarging"), 0) == under_bleed
    assert flagged or quiet, "converted under bleed on a quiesce the chip rejected, unflagged"
