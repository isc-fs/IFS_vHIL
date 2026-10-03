"""ecu-cal (#48): the operator pedal-calibration session, end to end in
virtual time, on an ECU provisioned as the car's are (systems/ecu-bl.yaml:
the CAN bootloader formats sector 7, the application appends to it).

ECU facts (IFS08-CE-ECU, Core/):
  0x7E2 PitCal_cmd (ACU bus, DLC 8): byte0 cmd, byte1 arg, bytes 4-7 guard,
    big-endian (pit_cal_cmd.def:21-25, can_rx_task.cpp:95-105); a one-deep
    mailbox handled by ControlTask every 10 ms tick (control_task.cpp:222-227).
  Commands POLL 0 .. RESET_DEFAULTS 7; capture points APPS_REST 1, APPS_FULL 2,
    BRAKE_REST 3, BRAKE_PRESSED 4, APPS_MID 5 (cal_session.hpp:29-36).
  0x7E3 PitCal_status: state, last_cmd, result, captured_mask, validation
    flags, cal_load (pit_cal_status.def:12-19); the answer to every 0x7E2 and
    every PitDiagStreamMs = 100 ms while a session is open
    (control_task.cpp:255-264). 0x7E4/0x7E5 follow READ_STORED/READ_STAGED
    with the eight values, BE u16 (pit_cal_apps.def, pit_cal_brake.def).
  ENTER and RESET_DEFAULTS need guard 0xCA11B0DE; COMMIT needs the CRC-32
    (ISO-HDLC, = zlib.crc32) of the staged record (cal_session.cpp:83,146,168;
    pedal_cal_nvm.cpp:33-59). CAPTURE samples the live raw ADC; a brake span
    re-derives arm = rest + 10 % and dv_hard = rest + 60 % (cal_session.cpp:121-126).
  vehicle_safe = !ok_precharge (fresh 0x020) && state != Active &&
    torque == 0 && inv_rpm == 0 (control_task.cpp:209-212); losing it refuses
    ENTER and drops an open session on CAPTURE/COMMIT (cal_session.cpp:84,95,138).
  COMMIT needs all five points and validate_cal() clean, else
    ValidationFailed + Error (cal_session.cpp:139-156, pedal_cal.cpp:21-77).
    A valid one applies at once, then appends ONE 32-byte flash word to
    sector 7 (0x080E0000) after the last live entry with seq = max + 1, and
    reads it back (control_task.cpp:237-249, pedal_cal_flash.cpp:25-70,
    pedal_cal_nvm.cpp:124-190). It never erases: a non-erased append slot is
    CalWrite::Full -> NvmWriteFailed, the calibration stays live.
  A session idle for CalSessionTimeoutMs = 30000 closes: Idle, NotInSession
    (cal_session.hpp:66, cal_session.cpp:45-53).
  Boot loads the newest key-0x1000 record with the magic (pedal_cal_nvm.cpp:61-122):
    none -> Defaults 0; valid -> Loaded 1; validate_cal() rejects -> Invalid 2;
    unknown version -> BadVersion 3; all but Loaded run the compile-time
    defaults (ecu_config.hpp:95-130). The outcome rides 0x704 byte5 b4-b5,
    ungated, every 1 s (pit_diag_health.def:59, pit_diag.cpp:288).
Corrupt records are written into flash from the test through the monitor:
that models what a torn program or bit rot leaves behind, read back clean.
The vHIL has no flash ECC (on the H733 a torn flash word usually raises a
double-bit ECC error instead), so this exercises the firmware's parser, not
the ECC path.
"""
import re
import zlib
from dataclasses import dataclass
from typing import Optional

import pytest

from vhil.sim import Sim
from vhil.system import REPO

CAL_CMD, CAL_STATUS, CAL_APPS, CAL_BRAKE = 0x7E2, 0x7E3, 0x7E4, 0x7E5
HEALTH, PIT_ARM, PIT_BRAKE = 0x704, 0x7E0, 0x705
AMS_OK_PRECHARGE, INV_RPM = 0x020, 0x463
POLL, ENTER, CAPTURE, READ_STORED, READ_STAGED, COMMIT, ABORT, RESET_DEFAULTS = range(8)
APPS_REST, APPS_FULL, BRAKE_REST, BRAKE_PRESSED, APPS_MID = 1, 2, 3, 4, 5
IDLE, ACTIVE, VALIDATED, COMMITTING, COMMITTED, ERROR = range(6)
(OK, BAD_GUARD, NOT_IN_SESSION, VEHICLE_NOT_SAFE, SAMPLE_UNSTABLE, MISSING_POINTS,
 VALIDATION_FAILED, NVM_WRITE_FAILED, UNKNOWN_CMD) = range(9)
LOAD_DEFAULTS, LOAD_LOADED, LOAD_INVALID, LOAD_BAD_VERSION = range(4)
GUARD = 0xCA11B0DE
ALL_POINTS = 0x1F
APPS_SPAN_MISMATCH = 1 << 2
SESSION_TIMEOUT_MS, STREAM_MS, TICK_MS = 30000, 100, 10
BOOT_MS = 3000                       # 2 s bootloader auto-jump window + app start
VREF = 3.3
NVM_BASE, ENTRY, NVM_MAGIC, CAL_KEY = 0x080E0000, 32, 0xABCD, 0x1000

# Compile-time defaults: apps1 min/max, apps2 min/max, brake rest/arm/dv_hard/pressed.
DEFAULTS = (2490, 3350, 2345, 3025, 0, 750, 2500, 3000)
# Pedal positions (raw codes) for a good sweep: apps1, apps2, brake.
REST, FULL, MID = (2000, 1800, 600), (3000, 2600, 3200), (2500, 2200, 600)
# What that sweep stages: brake arm/dv_hard re-derived from 600..3200.
SWEPT = (2000, 3000, 1800, 2600, 600, 860, 2160, 3200)


@dataclass(frozen=True)
class Reply:
    state: int
    last_cmd: int
    result: int
    mask: int
    flags: int
    cal_load: int
    values: Optional[tuple] = None


def _record(values):
    """The 18-byte record body: version 1, reserved, eight BE u16."""
    return bytes([1, 0]) + b"".join(v.to_bytes(2, "big") for v in values)


def _crc(values):
    return zlib.crc32(_record(values))


def _cmd(sim, cmd, arg=0, guard=0) -> Reply:
    """Send one 0x7E2; return the ECU's answer (and 0x7E4/0x7E5 if sent)."""
    acu = sim.can("can_acu")
    t = sim.now_us()
    acu.send(CAL_CMD, bytes([cmd, arg, 0, 0]) + guard.to_bytes(4, "big"))
    sim.run_for(ms=3 * TICK_MS)
    replies = [f for f in acu.frames([CAL_STATUS], since_us=t) if f.data[1] == cmd]
    assert replies, f"no 0x7E3 answer to cmd {cmd}"
    d = replies[-1].data
    values = None
    apps, brake = acu.last(CAL_APPS, since_us=t), acu.last(CAL_BRAKE, since_us=t)
    if apps and brake:
        values = tuple(int.from_bytes(f.data[i:i + 2], "big")
                       for f in (apps, brake) for i in (0, 2, 4, 6))
    return Reply(*d[:6], values=values)


def _volts(code):
    return code * VREF / 4095


def _pedals(sim, apps1, apps2, brake):
    io = sim.io("ecu")
    io.set_voltage("PF8", _volts(apps1))
    io.set_voltage("PF9", _volts(apps2))
    io.set_voltage("PF7", _volts(brake))
    sim.run_for(ms=3 * TICK_MS)


def _capture(sim, point, pedals):
    _pedals(sim, *pedals)
    r = _cmd(sim, CAPTURE, point)
    assert r.result == OK, r
    return r


def _sweep(sim, rest=REST, full=FULL, mid=MID):
    """ENTER and capture all five points, the way the pit tool walks it."""
    assert _cmd(sim, ENTER, guard=GUARD).state == ACTIVE
    _capture(sim, APPS_REST, rest)
    _capture(sim, BRAKE_REST, rest)
    _capture(sim, APPS_FULL, full)
    _capture(sim, BRAKE_PRESSED, full)
    return _capture(sim, APPS_MID, mid)


def _commit(sim, values):
    return _cmd(sim, COMMIT, guard=_crc(values))


def _flash(sim, address, n):
    out = sim.monitor(f"sysbus ReadBytes {address:#x} {n}", board="ecu")
    data = bytes(int(b, 16) for b in re.findall(r"0x([0-9A-Fa-f]{2})", out))
    assert len(data) == n, out
    return data


def _entries(sim, slots=16):
    """Live NVM entries (magic intact) among the first `slots`: (slot, bytes)."""
    raw = _flash(sim, NVM_BASE, slots * ENTRY)
    return [(i, raw[i * ENTRY:(i + 1) * ENTRY]) for i in range(slots)
            if int.from_bytes(raw[i * ENTRY:i * ENTRY + 2], "little") == NVM_MAGIC]


def _cal_entries(sim):
    return [(i, e) for i, e in _entries(sim) if int.from_bytes(e[2:4], "little") == CAL_KEY]


def _cal_status(sim, since_us):
    """0x704 cal_status (byte5 b4-b5) from the newest frame since since_us."""
    frame = sim.can("can_acu").last(HEALTH, since_us=since_us)
    assert frame is not None, "no 0x704"
    return (frame.data[5] >> 4) & 0x3


def _boot(firmware):
    sim = Sim(REPO / "systems" / "ecu-bl.yaml",
              {"ecu": firmware("ecu"), "ecu.bootloader": firmware("can-bootloader")}).start()
    sim.run_for(ms=BOOT_MS)
    return sim


def _power_cycle(sim):
    t = sim.now_us()
    sim.power_cycle("ecu")
    sim.run_for(ms=BOOT_MS)
    return t


# -- the session, on one board ---------------------------------------------------

@pytest.fixture(scope="module")
def board(firmware):
    sim = _boot(firmware)
    yield sim
    sim.stop()


@pytest.fixture
def ecu(board):
    """The shared board, left with no session open and no stimulus running."""
    yield board
    acu = board.can("can_acu")
    acu.stop_periodic("ams")
    acu.send(AMS_OK_PRECHARGE, b"\x00")
    board.can("can_inv").send(INV_RPM, bytes(8))
    _cmd(board, ABORT)


def test_an_erased_nvm_runs_the_defaults_and_says_so(ecu):
    """First boot of a provisioned board: no record, compile-time defaults,
    cal_status Defaults on 0x704 and 0x7E3; READ_STORED needs no session."""
    assert _cal_status(ecu, 0) == LOAD_DEFAULTS
    r = _cmd(ecu, READ_STORED)
    assert (r.state, r.result, r.cal_load) == (IDLE, OK, LOAD_DEFAULTS)
    assert r.values == DEFAULTS


@pytest.mark.parametrize("cmd", [ENTER, RESET_DEFAULTS])
def test_opening_and_reset_need_the_guard(ecu, cmd):
    """A stray frame can't open a session or stage the defaults."""
    r = _cmd(ecu, cmd, guard=GUARD ^ 1)
    assert (r.state, r.result) == (IDLE, BAD_GUARD)
    r = _cmd(ecu, cmd, guard=GUARD)
    assert (r.state, r.result) == (ACTIVE, OK)
    assert r.mask == (ALL_POINTS if cmd == RESET_DEFAULTS else 0)


@pytest.mark.parametrize("cmd, arg", [(CAPTURE, APPS_REST), (READ_STAGED, 0), (COMMIT, 0)])
def test_session_commands_need_a_session(ecu, cmd, arg):
    r = _cmd(ecu, cmd, arg, guard=_crc(DEFAULTS))
    assert (r.state, r.result, r.values) == (IDLE, NOT_IN_SESSION, None)


def test_unknown_commands_and_points_are_refused(ecu):
    assert _cmd(ecu, 9).result == UNKNOWN_CMD
    _cmd(ecu, ENTER, guard=GUARD)
    r = _cmd(ecu, CAPTURE, 9)
    assert (r.state, r.result, r.mask) == (ACTIVE, UNKNOWN_CMD, 0)


def test_capture_takes_the_live_adc_and_derives_the_brake_thresholds(ecu):
    """Each point is the raw code on its pin; the brake span re-derives arm
    (10 %) and dv_hard (60 %); READ_STAGED shows the set before any commit."""
    assert _sweep(ecu).mask == ALL_POINTS
    r = _cmd(ecu, READ_STAGED)
    assert (r.state, r.mask) == (ACTIVE, ALL_POINTS)
    assert r.values == SWEPT
    assert _cmd(ecu, READ_STORED).values == DEFAULTS, "staging changed the live set"


def test_the_session_streams_its_status_every_100_ms(ecu):
    from vhil.sim import assert_period
    _cmd(ecu, ENTER, guard=GUARD)
    t = ecu.run_for(ms=1000)
    beats = ecu.can("can_acu").frames([CAL_STATUS], since_us=t - 1_000_000)
    assert_period(beats, period_us=STREAM_MS * 1000, tolerance_us=0, min_count=9)
    assert all(f.data[0] == ACTIVE for f in beats)


def test_abort_discards_the_staged_set(ecu):
    """ABORT drops what was captured; the next session starts from the live set."""
    _sweep(ecu)
    r = _cmd(ecu, ABORT)
    assert (r.state, r.mask) == (IDLE, 0)
    assert _cmd(ecu, READ_STAGED).result == NOT_IN_SESSION
    _cmd(ecu, ENTER, guard=GUARD)
    assert _cmd(ecu, READ_STAGED).values == DEFAULTS


def test_commit_needs_every_point_and_the_staged_crc(ecu):
    """MissingPoints until all five are in; a CRC of anything but the staged
    set is ValidationFailed and leaves the session open (a desynced client)."""
    _cmd(ecu, ENTER, guard=GUARD)
    _capture(ecu, APPS_REST, REST)
    r = _commit(ecu, DEFAULTS)
    assert (r.state, r.result) == (ACTIVE, MISSING_POINTS)
    _sweep(ecu)
    r = _commit(ecu, DEFAULTS)
    assert (r.state, r.result, r.flags) == (ACTIVE, VALIDATION_FAILED, 0)
    assert not _cal_entries(ecu), "a refused commit wrote flash"


def test_a_calibration_that_fails_validation_is_never_written(ecu):
    """APPS2 at 25 % where APPS1 reads 50 %: the mid-travel check (5 %)
    flags AppsSpanMismatch, the session goes to Error, flash is untouched."""
    _sweep(ecu, mid=(2500, 2000, 600))
    r = _commit(ecu, SWEPT)
    assert (r.state, r.result, r.flags) == (ERROR, VALIDATION_FAILED, APPS_SPAN_MISMATCH)
    assert not _cal_entries(ecu)
    assert _cmd(ecu, READ_STORED).values == DEFAULTS


def test_precharge_keeps_the_session_shut(ecu):
    """vehicle_safe: TS up (fresh 0x020 ok_precharge) refuses ENTER; TS down opens it."""
    acu = ecu.can("can_acu")
    acu.send_periodic("ams", AMS_OK_PRECHARGE, b"\x01", 10)
    ecu.run_for(ms=50)
    r = _cmd(ecu, ENTER, guard=GUARD)
    assert (r.state, r.result) == (IDLE, VEHICLE_NOT_SAFE)
    acu.update_periodic("ams", b"\x00")
    ecu.run_for(ms=50)
    assert _cmd(ecu, ENTER, guard=GUARD).state == ACTIVE


@pytest.mark.parametrize("cmd", [CAPTURE, COMMIT])
def test_precharge_mid_session_drops_it(ecu, cmd):
    """TS coming up during a session: the next CAPTURE or COMMIT is refused
    and everything staged is discarded; nothing reaches flash."""
    _sweep(ecu)
    ecu.can("can_acu").send_periodic("ams", AMS_OK_PRECHARGE, b"\x01", 10)
    ecu.run_for(ms=50)
    r = _cmd(ecu, cmd, APPS_REST, guard=_crc(SWEPT))
    assert (r.state, r.result, r.mask) == (IDLE, VEHICLE_NOT_SAFE, 0)
    assert not _cal_entries(ecu)


def test_a_spinning_motor_keeps_the_session_shut_until_it_reports_zero(ecu):
    """inv_rpm != 0 closes the gate. It is the last 0x463 value, not
    freshness-gated, so a stale non-zero speed stays closed (fail-closed)."""
    inv = ecu.can("can_inv")
    erpm = 1000
    inv.send(INV_RPM, bytes([0, 0, 0, 0, 0, (erpm & 0xF) << 4, (erpm >> 4) & 0xFF,
                             (erpm >> 12) & 0xFF]))
    ecu.run_for(ms=1000)                                  # long past InvFeedbackStaleMs
    assert _cmd(ecu, ENTER, guard=GUARD).result == VEHICLE_NOT_SAFE
    inv.send(INV_RPM, bytes(8))
    ecu.run_for(ms=2 * TICK_MS)
    assert _cmd(ecu, ENTER, guard=GUARD).state == ACTIVE


def test_an_idle_session_closes_after_30_s(ecu):
    """No traffic for CalSessionTimeoutMs: one 0x7E3 Idle/NotInSession on the
    tick it lapses, then the stream stops."""
    acu = ecu.can("can_acu")
    t = ecu.now_us()
    _cmd(ecu, ENTER, guard=GUARD)
    reply = [f for f in acu.frames([CAL_STATUS], since_us=t) if f.data[1] == ENTER][-1]
    ecu.run_for(ms=SESSION_TIMEOUT_MS + 500)
    after = acu.frames([CAL_STATUS], since_us=reply.t_us + 1)
    closed = [f for f in after if f.data[0] == IDLE]
    assert closed, "the session never timed out"
    assert closed[0].data[2] == NOT_IN_SESSION
    lapse_ms = (closed[0].t_us - reply.t_us) / 1000
    assert SESSION_TIMEOUT_MS <= lapse_ms <= SESSION_TIMEOUT_MS + TICK_MS, lapse_ms
    assert all(f.t_us <= closed[0].t_us for f in after), "status kept streaming after close"


@pytest.mark.xfail(strict=True, reason=(
    "firmware: CalSession never leaves Committing after a durable commit. "
    "control_task.cpp:244 overrides only the one reply to Committed; the "
    "session's own state_ stays Committing (cal_session.cpp:157, no "
    "persist-ok hook), so the 100 ms stream and POLL say Committing until "
    "the 30 s timeout reports NotInSession"))
def test_the_session_keeps_reporting_committed(ecu):
    """A client that missed the Committed reply must learn it from the
    stream or a POLL (pit_cal_status.def:3-5)."""
    _cmd(ecu, ENTER, guard=GUARD)
    _cmd(ecu, RESET_DEFAULTS, guard=GUARD)
    assert _commit(ecu, DEFAULTS).state == COMMITTED
    t = ecu.run_for(ms=300)
    beats = ecu.can("can_acu").frames([CAL_STATUS], since_us=t - 300_000)
    assert beats
    assert [f.data[0] for f in beats] == [COMMITTED] * len(beats)
    assert _cmd(ecu, POLL).state == COMMITTED


# -- persistence: fresh boards, power cycles ---------------------------------------

@pytest.fixture
def fresh(firmware):
    sim = _boot(firmware)
    yield sim
    sim.stop()


def test_commit_applies_now_persists_one_flash_word_and_reloads(fresh):
    """Apply-on-commit (brake % on 0x705 moves at once), one 32-byte entry
    appended after the bootloader's with seq = max + 1, cal_status Loaded,
    and after a power cut the ECU boots on the committed set."""
    acu = fresh.can("can_acu")
    acu.send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    before = _entries(fresh)
    _sweep(fresh)
    _pedals(fresh, 2500, 2200, 1900)
    fresh.run_for(ms=2 * STREAM_MS)
    pct_before = acu.last(PIT_BRAKE).data[2]
    assert pct_before == 1900 * 100 // 4095              # uncalibrated: full-range scale

    r = _commit(fresh, SWEPT)
    assert (r.state, r.result, r.cal_load) == (COMMITTED, OK, LOAD_LOADED)
    fresh.run_for(ms=2 * STREAM_MS)
    assert acu.last(PIT_BRAKE).data[2] == (1900 - 600) * 100 // (3200 - 600)
    assert _cmd(fresh, READ_STORED).values == SWEPT

    after = _entries(fresh)
    assert after[:len(before)] == before, "the bootloader's entries moved"
    (slot, entry), = after[len(before):]
    assert slot == before[-1][0] + 1
    seq = max(int.from_bytes(e[8:12], "little") for _, e in before) + 1
    expected = (NVM_MAGIC.to_bytes(2, "little") + CAL_KEY.to_bytes(2, "little")
                + bytes([18, 0, 0, 0]) + seq.to_bytes(4, "little") + _record(SWEPT) + bytes(2))
    assert entry == expected
    t = fresh.run_for(ms=1100)
    assert _cal_status(fresh, t - 1_100_000) == LOAD_LOADED

    t = _power_cycle(fresh)
    assert _cal_status(fresh, t) == LOAD_LOADED
    r = _cmd(fresh, READ_STORED)
    assert (r.cal_load, r.values) == (LOAD_LOADED, SWEPT)


def test_a_torn_newest_record_falls_back_to_the_previous_one(fresh):
    """Two commits (the sweep, then RESET_DEFAULTS); the newest word is torn
    (one magic byte left erased, as an interrupted program leaves it). Boot
    loads the previous record. The torn slot is the append point and is not
    erased, so the next commit is refused (CalWrite::Full -> NvmWriteFailed,
    session Error) but still applies for this power cycle."""
    _sweep(fresh)
    assert _commit(fresh, SWEPT).state == COMMITTED
    _cmd(fresh, ENTER, guard=GUARD)
    _cmd(fresh, RESET_DEFAULTS, guard=GUARD)
    assert _commit(fresh, DEFAULTS).state == COMMITTED
    newest, _ = _cal_entries(fresh)[-1]
    fresh.monitor(f"sysbus WriteByte {NVM_BASE + newest * ENTRY + 1:#x} 0xFF", board="ecu")

    t = _power_cycle(fresh)
    assert _cal_status(fresh, t) == LOAD_LOADED
    assert _cmd(fresh, READ_STORED).values == SWEPT

    _sweep(fresh, rest=(2100, 1900, 700))
    staged = _cmd(fresh, READ_STAGED).values
    r = _commit(fresh, staged)
    assert (r.state, r.result) == (ERROR, NVM_WRITE_FAILED)
    assert _cmd(fresh, READ_STORED).values == staged, "not applied for this power cycle"
    assert _flash(fresh, NVM_BASE + (newest + 1) * ENTRY, ENTRY) == b"\xFF" * ENTRY
    t = fresh.run_for(ms=1100)
    assert _cal_status(fresh, t - 1_100_000) == LOAD_LOADED   # what boot found, unchanged


@pytest.mark.parametrize("offset, value, status", [
    (12 + 4, 0xFF, LOAD_INVALID),       # apps1_max high byte rots: 0xFFB8 > 4095
    (12 + 0, 0x03, LOAD_BAD_VERSION),   # record version 1 -> 3
], ids=["out-of-range-value", "unknown-version"])
def test_a_corrupt_record_boots_on_the_defaults_and_says_why(fresh, offset, value, status):
    """A record that keeps its magic but fails the parser's checks: boot runs
    the compile-time defaults, never the corrupt values, and names the cause
    on 0x704 and 0x7E3."""
    _sweep(fresh)
    assert _commit(fresh, SWEPT).state == COMMITTED
    (slot, _), = _cal_entries(fresh)
    fresh.monitor(f"sysbus WriteByte {NVM_BASE + slot * ENTRY + offset:#x} {value:#x}",
                  board="ecu")
    t = _power_cycle(fresh)
    assert _cal_status(fresh, t) == status
    r = _cmd(fresh, READ_STORED)
    assert (r.cal_load, r.values) == (status, DEFAULTS)
