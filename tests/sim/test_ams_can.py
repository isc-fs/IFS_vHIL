"""ams-can (#26): the AMS's CAN contract on FDCAN1, in virtual time.

AMS facts (IFS08-CE-AMS Core/Inc/can/messages/*.def, app_init_task.cpp):
  Every AMS TX frame, its DLC and period are declared once in a .def
  (CAN_MSG(name, id, dlc, sender, period_ms)). The contract is read from the
  .def files of the source the image was built from, so it follows the
  firmware version (0x6CC appears in 3.1.0, for one).
  The global filter accepts unmatched standard frames and REJECTS extended
  ones at the hardware (app_init_task.cpp:103-107): the AMS listens to
  nothing 29-bit.
  Pit-diag: 0x7F0 DE AD BE EF enables, 00 00 00 00 disables; each transition
  answers 0x7F1 [enabled] once (pit_ack.def, ams_config.hpp:706-707). The
  stream is 0x680..0x697 cells, 0x6A0..0x6B8 temps and 0x6C0..0x6CC status,
  once per second each.
  0x4A2[7] heartbeat, +1 per frame mod 256; 0x6C9 bytes 0-3 LE FDCAN1
  bus-off recovery count (pit_comms_health.def).
  Run (E-051): 0x100 every 10 ms (LE u16 link volts, bit 17 valid), TSMS
  PF9 held, one DASH_CHG PF10 press on a drained link locks Car and
  precharges; the link at the pack voltage completes it (state 3).
"""
import pytest

from vhil import candef
from vhil.sim import Sim
from vhil.system import REPO

PIT_CELLS = range(0x680, 0x698)
PIT_TEMPS = range(0x6A0, 0x6B9)
PIT_CMD, PIT_ACK = 0x7F0, 0x7F1
ENABLE, DISABLE = bytes.fromhex("DEADBEEF"), bytes(4)
TEMPS, COMMS_HEALTH, VCU = 0x4A2, 0x6C9, 0x100
BOOT_MS = 4000


@pytest.fixture(scope="module")
def contract(firmware):
    """{id: (dlc, period_ms)} of every frame the AMS sends, from the built
    source's .def files; period 0 = event-only. The pit-diag grid is not
    declared there (ams_config.hpp:655-662): 8-byte frames, 1 Hz."""
    src = firmware("ams").resolve().parent.parent
    found = {i: (m.dlc, m.period_ms) for i, m in candef.load(src).items() if m.sender == "AMS"}
    assert found, f"no AMS messages declared under {src / candef.MESSAGES}"
    found.update({i: (8, 1000) for i in (*PIT_CELLS, *PIT_TEMPS)})
    return found


@pytest.fixture(scope="module")
def armed(make_sim):
    """Booted, pit-diag armed at BOOT_MS, then 5 s of traffic."""
    sim = make_sim("ams")
    sim.run_for(ms=BOOT_MS)
    sim.can("can_acu").send(PIT_CMD, ENABLE)
    sim.run_for(ms=5000)
    return sim


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=BOOT_MS)
        yield sim


def _since_boot(sim):
    return [f for f in sim.can("can_acu").frames(since_us=sim.app_started["ams"] + BOOT_MS * 1000)
            if f.id != PIT_CMD]


def test_every_frame_has_its_contract_dlc(armed, contract):
    """A-011: each AMS frame, cyclic and pit-diag, carries its declared DLC,
    and the AMS sends nothing outside the contract (IFS_HIL's list omits
    0x130 and 0x021: drift)."""
    expected = contract
    seen = {}
    for f in _since_boot(armed):
        seen.setdefault(f.id, set()).add(len(f.data))
    unknown = sorted(hex(i) for i in seen if i not in expected)
    assert not unknown, f"frames outside the contract: {unknown}"
    wrong = {hex(i): (sorted(d), expected[i][0]) for i, d in seen.items() if d != {expected[i][0]}}
    assert not wrong, f"DLC (seen, contract): {wrong}"
    missing = sorted(hex(i) for i, (_, period) in expected.items() if period and i not in seen)
    assert not missing, f"contract frames never sent: {missing}"


def test_every_cyclic_frame_keeps_its_period(armed, contract):
    """A-008, A-014: every cyclic frame at its declared period, to 5 % of it."""
    off = {}
    for can_id, (_, period_ms) in sorted(contract.items()):
        if not period_ms:
            continue
        period_us = period_ms * 1000
        since = armed.app_started["ams"] + BOOT_MS * 1000 + 1_100_000
        t = [f.t_us for f in armed.can("can_acu").frames(can_id, since_us=since)]
        deltas = [b - a for a, b in zip(t, t[1:])]
        bad = [d for d in deltas if abs(d - period_us) > period_us // 20]
        if len(deltas) < 2 or bad:
            off[hex(can_id)] = (period_us, len(t), bad[:5])
    assert not off, f"(period us, frames, off deltas): {off}"


def test_the_pit_grid_is_complete_once_a_second(armed):
    """M-03: every cell and temperature frame of the grid, once per second."""
    sim = armed
    since = sim.now_us() - 3_000_000
    counts = {i: sim.can("can_acu").count(i, since_us=since) for i in (*PIT_CELLS, *PIT_TEMPS)}
    off = {hex(i): n for i, n in counts.items() if n != 3}
    assert not off, f"grid frames in 3 s (expected 3 each): {off}"


def test_pit_diag_arms_acks_and_disarms(ams):
    """0x7F0 enable -> 0x7F1 [1] once and the stream starts; disable ->
    0x7F1 [0] once and it stops."""
    can = ams.can("can_acu")
    t0 = ams.now_us()
    assert can.count(0x6C0, since_us=0) == 0, "pit stream ran before arming"
    can.send(PIT_CMD, ENABLE)
    ams.run_for(ms=1500)
    acks = can.frames(PIT_ACK, since_us=t0)
    assert [a.data for a in acks] == [b"\x01"], f"enable acks {acks}"
    assert can.count(0x6C0, since_us=t0) >= 1
    t1 = ams.now_us()
    can.send(PIT_CMD, DISABLE)
    ams.run_for(ms=200)
    t2 = ams.now_us()
    ams.run_for(ms=2000)
    assert [a.data for a in can.frames(PIT_ACK, since_us=t1)] == [b"\x00"]
    assert can.count(0x6C0, since_us=t2) == 0, "pit stream kept running after disarm"


def test_an_extended_id_is_rejected_at_the_filter(ams):
    """A-012: the enable magic on extended 0x7F0 never reaches the firmware."""
    can = ams.can("can_acu")
    t0 = ams.now_us()
    can.send(PIT_CMD, ENABLE, extended=True)
    ams.run_for(ms=1500)
    assert can.count([PIT_ACK, 0x6C0], since_us=t0) == 0, "an extended frame armed pit-diag"
    can.send(PIT_CMD, ENABLE)
    ams.run_for(ms=1500)
    assert can.count(PIT_ACK, since_us=t0) == 1, "the standard frame did not arm it"


def test_heartbeat_over_a_minute(ams):
    """B-010, K-103: 120 frames in 60 s, each +1 mod 256: no stall."""
    t0 = ams.now_us()
    ams.run_for(ms=60_000)
    beats = [f.data[7] for f in ams.can("can_acu").frames(TEMPS, since_us=t0)]
    assert len(beats) == 120, f"{len(beats)} heartbeats in 60 s"
    assert all((b - a) % 256 == 1 for a, b in zip(beats, beats[1:]))


def test_no_spurious_bus_off_recovery(ams):
    """J-132: a healthy bus never triggers a bus-off recovery."""
    can = ams.can("can_acu")
    can.send(PIT_CMD, ENABLE)
    ams.run_for(ms=30_000)
    assert int.from_bytes(can.last(COMMS_HEALTH).data[0:4], "little") == 0


def test_bus_noise_neither_resets_nor_deafens(ams):
    """F-081: 200 frames/s of filler on 0x500..0x5FF for 10 s: no reboot (the
    heartbeat never restarts), no fault, and the pit trigger still works."""
    can = ams.can("can_acu")
    t0 = ams.now_us()
    keys = [f"noise{k}" for k in range(20)]
    for k, key in enumerate(keys):
        can.send_periodic(key, 0x500 + 13 * k, bytes([k] * 8), period_ms=100, start_us=t0 + 5000 * k)
    ams.run_for(ms=10_000)
    for key in keys:
        can.stop_periodic(key)
    beats = [f.data[7] for f in can.frames(TEMPS, since_us=t0)]
    assert all((b - a) % 256 == 1 for a, b in zip(beats, beats[1:])), "the heartbeat restarted"
    assert ams.read_symbol("ams", "g_state_telemetry") == 0, "the AMS left Start"
    t1 = ams.now_us()
    can.send(PIT_CMD, ENABLE)
    ams.run_for(ms=1500)
    assert can.count(PIT_ACK, since_us=t1) == 1


def _to_run(ams):
    can, io = ams.can("can_acu"), ams.io("ams")
    can.send_periodic("vcu", VCU, (0).to_bytes(2, "little") + b"\x02", period_ms=10)
    io.set_input("sysbus.gpioPortF", 9, True)            # TSMS
    ams.run_for(ms=100)
    io.set_input("sysbus.gpioPortF", 10, True)           # DASH_CHG press
    ams.run_for(ms=50)
    io.set_input("sysbus.gpioPortF", 10, False)
    ams.run_for(ms=100)
    can.update_periodic("vcu", (352).to_bytes(2, "little") + b"\x02")
    ams.run_for(ms=200)
    assert ams.read_symbol("ams", "g_state_telemetry") == 3, "did not reach Run"


@pytest.mark.soak
@pytest.mark.parametrize("run", [False, True], ids=["idle", "run"])
def test_soak_30_minutes(ams, run):
    """E-050, E-051: 30 min of virtual time idle in Start, or in Run with a
    live VCU heartbeat: the state held throughout, zero cadence outliers,
    heartbeat continuous."""
    can = ams.can("can_acu")
    if run:
        _to_run(ams)
    expected = 3 if run else 0
    t0 = ams.now_us()
    for _ in range(30):
        ams.run_for(ms=60_000)
        assert ams.read_symbol("ams", "g_state_telemetry") == expected, "the AMS changed state"
    frames = can.frames(TEMPS, since_us=t0)
    deltas = [b.t_us - a.t_us for a, b in zip(frames, frames[1:])]
    assert len(frames) >= 3599 and all(abs(d - 500_000) <= 25_000 for d in deltas)
    assert all((b.data[7] - a.data[7]) % 256 == 1 for a, b in zip(frames, frames[1:]))
    if run:
        assert (frames[-1].data[5] >> 2) & 3 == 1, "not Car-locked"
