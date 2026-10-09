"""CAN bus arbitration, load and ACK in virtual time (#174): the bus model
(models/renode/VhilCanBus.cs) on a bus with `arbitration: true`, against the
real firmwares. One test per exit criterion of #174, and the frame timing
itself.

Bus facts (ISO 11898-1; Bosch CAN 2.0B):
  Lowest arbitration field wins (dominant 0); the others wait for the next
  idle bus. A frame is SOF..EOF with its stuff bits, then 3 bits of
  intermission (vhil/canframe.py, the reference these tests check the C#
  model against). No ACK: error flag + delimiter (14 bits) after the ACK
  slot, TEC +8 until error-passive (128), then no more (rule 3, exception 1);
  an error-passive transmitter suspends 8 bits after each frame.
Controller facts (RM0468, FDCAN):
  CCCR.DAR (AutoRetransmission = DISABLE) cancels a frame that loses
  arbitration or errors; otherwise it is retried. ECR 0x040 TEC [7:0]; PSR
  0x044 LEC [2:0] (3 = Ack error), EP 5, EW 6, BO 7; TXFQS 0x0C4 TFFL [5:0],
  TFQF 21 (stm32h733xx.h:332-364, 4448-4475).
Firmware facts:
  ECU (IFS08-CE-ECU): every FDCAN at 500 kbit/s with AutoRetransmission
    ENABLE, TX FIFO 32 / 16 / 32 deep (fdcan.c:45,68,93,115,141,164); 0x100
    every 10 ms on FDCAN2 (control_task.cpp:305-320); hal_send drops a frame
    the full TX FIFO refuses, ignoring HAL_FDCAN_AddMessageToTxFifoQ's result
    (can_tx_task.cpp:47-53); g_can_tx_dropped counts the RTOS queue's drops
    only (can_tx_task.cpp:56-71).
  AMS (IFS08-CE-AMS): FDCAN1 with AutoRetransmission DISABLE, 16-deep TX
    FIFO (main.c:465,487; docs/CAN_MAP.md); VcuStale = 200 ms once Car-locked
    (ams_config.hpp:163, safety_predicates.hpp:251-255); its LOGFS replies
    go out on 0x010 + node = 0x012, which out-prioritises the ECU's 0x100: a
    pull is only allowed in Start or Error because a long one would starve
    the heartbeat until VcuStale latches Error, and a reply burst is ~17-33
    ms (diag_dispatch.hpp:37-56).
"""
import pytest

from vhil import canframe
from vhil.sim import Sim
from vhil.system import REPO

BIT_NS = 2000                         # 500 kbit/s (both firmwares' NBTP)
FDCAN1, FDCAN2 = 0x4000A000, 0x4000A400
ECR, PSR, TXFQS = 0x040, 0x044, 0x0C4
TFQF, EP, EW, BO = 1 << 21, 1 << 5, 1 << 6, 1 << 7
VCU_HEARTBEAT, ECU_PIT_ARM, AMS_PIT_ARM = 0x100, 0x7E0, 0x7F0
LOGFS = 0x012
INV_SETPOINTS = (0x360, 0x362)


def reg(sim, board, base, offset):
    return int(sim.monitor(f"sysbus ReadDoubleWord {base + offset:#x}", board=board).strip(), 16)


def frame_ns(r):
    return canframe.frame_bits(r.id, r.data, r.extended, r.remote) * BIT_NS


def flood(can, start_us, ms, data=bytes(8)):
    """A LOGFS-style stream on `can`: 0x012 frames of 8 bytes, offered at
    4 per ms, more than a 500 kbit/s bus carries (an all-zero payload is the
    most stuffed: ~270 us a frame), from start_us for `ms`. Two periodic
    senders, one frame each per 500 us sync point. Returns the stop time."""
    for k in ("logfs_a", "logfs_b"):
        can.send_periodic(k, LOGFS, data, period_ms=0.5, start_us=start_us)
    return start_us + int(ms * 1000)


def stop_flood(can):
    for k in ("logfs_a", "logfs_b"):
        can.stop_periodic(k)


@pytest.fixture
def ecu_acu(images):
    """The ECU with its ACU bus (FDCAN2) arbitrated: one board, so the bus
    decides each frame as it is offered and every effect is exact."""
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu"), arbitration=["can_acu"]) as sim:
        sim.wait_for_app()
        sim.run_for(ms=500)
        yield sim


# -- frame time ---------------------------------------------------------------

def test_frames_take_their_exact_bit_time_and_arrive_at_eof(ecu_acu):
    """Each frame of the ECU's real traffic lasts its bits with the exact
    stuff count (the Python reference agrees with the C# model), the bus
    stays busy 3 bits more, and the probe gets it at the next-to-last EOF bit,
    with nothing late (a single board on the bus)."""
    sim = ecu_acu
    can = sim.can("can_acu")
    t = sim.run_for(ms=1000)
    frames = [r for r in can.timeline(since_us=t - 1_000_000) if r.node.startswith("ecu:")]
    assert len(frames) >= 200 and {r.outcome for r in frames} == {"ok"}
    for r in frames:
        assert r.end_ns - r.start_ns == frame_ns(r), r
        assert r.free_ns - r.end_ns == 3 * BIT_NS, r
    got = {(f.id, f.t_us) for f in can.frames(since_us=t - 1_000_000)}
    assert all((r.id, (r.end_ns - BIT_NS) // 1000) in got for r in frames)
    assert sum(r.free_ns - r.start_ns for r in frames) / 1e9 == pytest.approx(
        can.load(t - 1_000_000, t), abs=0.002)
    assert can.stats()["late"] == 0


# -- exit criterion 1: arbitration --------------------------------------------

def test_the_lower_id_goes_first_and_the_other_waits_a_frame(ecu_acu):
    """Two nodes offer at the same virtual instant: 0x200 wins, 0x300 starts
    exactly when the bus is idle again (0x200's frame plus intermission), and
    each sender's record of its own frame is its frame's end."""
    sim = ecu_acu
    a, b = sim.can("can_acu"), sim.can("can_acu").node("b")
    hb = a.timeline(since_us=sim.now_us() - 20_000)
    # Midway between two heartbeats (every 10 ms), in the future, on a sync
    # point: both probes offer there.
    quiet = max(r.start_ns for r in hb if r.id == VCU_HEARTBEAT) // 1000 + 5_000
    while quiet < sim.now_us() + 1_000:
        quiet += 10_000
    at = -(-quiet // 500) * 500
    a.send_at(at, 0x300, b"\x33" * 8)
    b.send_at(at, 0x200, b"\x22" * 8)
    sim.run_for(ms=20)
    tl = {r.id: r for r in a.timeline(since_us=at - 1000) if r.id in (0x200, 0x300)}
    first, second = tl[0x200], tl[0x300]
    assert first.offer_ns == second.offer_ns == at * 1000
    assert first.start_ns == at * 1000, "the bus was not idle"
    assert second.start_ns == first.free_ns == first.start_ns + frame_ns(first) + 3 * BIT_NS
    assert not [r for r in a.timeline(since_us=at - 1000)
                if first.start_ns < r.start_ns < second.start_ns]
    assert [f.t_us for f in a.sent(0x300, since_us=at)] == [second.end_ns // 1000]
    assert [f.t_us for f in b.sent(0x200, since_us=at)] == [first.end_ns // 1000]
    assert [f.t_us for f in a.frames(0x200, since_us=at)] == [(first.end_ns - BIT_NS) // 1000]


@pytest.fixture(scope="module")
def two_boards(images):
    """The ECU and the AMS with pit-diag streaming (ECU: 15+ frames every
    100 ms; AMS: ~60 frames every 1 s, blocking on its FIFO), 3 s of the
    arbitrated ACU bus's timeline."""
    with Sim(REPO / "systems" / "ecu-ams.yaml", images("ecu-ams"), arbitration=["can_acu"]) as sim:
        sim.wait_for_app()
        sim.run_for(ms=1000)
        can = sim.can("can_acu")
        can.send(ECU_PIT_ARM, bytes.fromhex("DEADBEEF"))
        can.send(AMS_PIT_ARM, bytes.fromhex("DEADBEEF"))
        t0 = sim.run_for(ms=500)
        sim.run_for(ms=3000)
        yield can.timeline(since_us=t0), can.stats()


def test_two_boards_arbitrate_on_the_acu_bus(two_boards):
    """Every time a node's frame was ready while another's started, the one
    that started had the lower arbitration field; frames never overlap, and
    each starts when offered or the moment the bus frees."""
    tl, stats = two_boards
    on_bus = [r for r in tl if r.outcome != "lost"]
    assert {r.node.split(":")[0] for r in on_bus} >= {"ecu", "ams"}
    # Frames never overlap, and each starts as soon as both it and the bus
    # are ready: when offered, or the moment the previous frame frees the bus.
    for prev, r in zip(on_bus, on_bus[1:]):
        assert r.start_ns == max(r.offer_ns, prev.free_ns), (prev, r)
    # A contest: another node's next frame (its next decision, a FIFO's head)
    # was already offered when a frame started. The one that started must
    # win arbitration against it.
    by_node: dict[str, list] = {}
    for r in tl:
        by_node.setdefault(r.node, []).append(r)
    contests = 0
    for w in on_bus:
        for node, records in by_node.items():
            if node == w.node:
                continue
            nxt = next((r for r in records if r.start_ns >= w.start_ns), None)
            if nxt is not None and nxt.offer_ns <= w.start_ns:
                assert canframe.wins((w.id, w.extended, w.remote),
                                     (nxt.id, nxt.extended, nxt.remote)), (w, nxt)
                contests += 1
    lost = [r for r in tl if r.outcome == "lost"]
    assert all(r.node.startswith("ams:") for r in lost), "only the AMS has DAR set"
    assert contests >= 20, f"only {contests} contests in 3 s"
    print(f"{len(on_bus)} frames, {contests} contests, {len(lost)} AMS frames lost to "
          f"arbitration ({sorted({hex(r.id) for r in lost})}), stats {stats}")


@pytest.mark.xfail(strict=True, reason="IFS08-CE-AMS#623: with AutoRetransmission DISABLE "
                   "(DAR) a frame that loses arbitration is cancelled, not retried")
def test_every_ams_frame_reaches_the_bus_beside_the_ecu(two_boards):
    """The AMS's docs/CAN_MAP.md says a frame is dropped only if it 'loses
    arbitration and then errors'. RM0468 (FDCAN, disabled automatic
    retransmission) cancels it on lost arbitration alone: beside the ECU's
    10 ms traffic the AMS's pit-diag scan loses the same frames every
    second (0x685-0x689 here; on the car, whichever its burst lines up
    with)."""
    tl, _ = two_boards
    lost = [r for r in tl if r.outcome == "lost"]
    assert lost == [], f"{len(lost)} AMS frames lost to arbitration: {sorted({hex(r.id) for r in lost})}"


# -- determinism (#209) -------------------------------------------------------

def acu_run(images):
    """One power-on of ecu-ams with both pit-diag streams armed: the ACU
    bus's whole timeline and its stats."""
    with Sim(REPO / "systems" / "ecu-ams.yaml", images("ecu-ams"), arbitration=["can_acu"]) as sim:
        sim.wait_for_app()
        sim.run_for(ms=500)
        can = sim.can("can_acu")
        can.send(ECU_PIT_ARM, bytes.fromhex("DEADBEEF"))
        can.send(AMS_PIT_ARM, bytes.fromhex("DEADBEEF"))
        sim.run_for(ms=1000)
        return can.timeline(), can.stats()


def test_two_board_runs_give_the_same_bus_timeline(images):
    """The same system and inputs give the same ACU bus, frame for frame:
    order, offer/start/end times, arbitration outcomes. Each board runs on a
    time source of its own (models/renode/VhilMachine.cs) and they meet only
    at sync points, where the bus decides; before #209 both shared Renode's
    master time source, each board's timers followed whichever CPU thread
    the host had run furthest, and no two runs agreed past the first 20 ms
    after boot."""
    runs = [acu_run(images) for _ in range(3)]
    tl0, stats0 = runs[0]
    assert {r.node.split(":")[0] for r in tl0} >= {"ecu", "ams"}
    for k, (tl, stats) in enumerate(runs[1:], 1):
        diverged = next((i for i, (a, b) in enumerate(zip(tl0, tl)) if a != b), min(len(tl0), len(tl)))
        assert tl == tl0, (f"run {k} diverged at frame {diverged} of {len(tl0)}: "
                           f"{tl0[diverged:diverged + 1]} vs {tl[diverged:diverged + 1]}")
        assert stats == stats0
    # A wake that found the bus taken is retried a microsecond later, by
    # host timing: with a time source per board it never happens.
    assert stats0["wake_retries"] == 0


# -- exit criterion 2: saturation ---------------------------------------------

def test_a_saturating_sender_starves_lower_priority_and_fills_the_tx_fifo(ecu_acu):
    """A 0x012 stream above the bus's capacity: the bus is 100 % busy, the
    ECU's 0x100 (lower priority) never gets on it, its 16-deep TX FIFO fills
    (TXFQS.TFQF) and the rest are dropped; once the stream ends the FIFO
    drains and the heartbeat is back every 10 ms."""
    sim = ecu_acu
    can = sim.can("can_acu")
    t0 = -(-sim.now_us() // 500) * 500 + 500
    end = flood(can, t0, 300)
    sim.run_for(us=end - sim.now_us())
    stop_flood(can)
    assert can.load(t0 + 2_000, end) >= 0.99
    heartbeats = [r for r in can.timeline(since_us=t0 + 1_000) if r.id == VCU_HEARTBEAT
                  and r.start_ns < end * 1000]
    assert heartbeats == [], f"0x100 got on a saturated bus: {heartbeats[:3]}"
    txfqs = reg(sim, "ecu", FDCAN2, TXFQS)
    assert txfqs & TFQF and txfqs & 0x3F == 0, f"TXFQS {txfqs:#x}: the TX FIFO is not full"
    # The backlog drains, then the 16 queued frames, then the heartbeat again.
    t = sim.run_for(ms=300)
    hb = [f.t_us for f in can.frames(VCU_HEARTBEAT, since_us=t - 100_000)]
    assert len(hb) >= 9, f"{len(hb)} heartbeats in the last 100 ms"
    assert reg(sim, "ecu", FDCAN2, TXFQS) & TFQF == 0


@pytest.mark.xfail(strict=True, reason="IFS08-CE-ECU#251: a frame the full TX FIFO refuses is "
                   "dropped uncounted (hal_send ignores the HAL's error)")
def test_the_ecu_counts_heartbeats_a_full_tx_fifo_drops(ecu_acu):
    """g_can_tx_dropped is documented as the count that makes 'a silently-lost
    safety cyclic (0x100/0x504/...)' visible (can_tx_task.cpp:64-67, the
    sticky tx_dropped bit on 0x700). On a saturated bus the hardware FIFO
    refuses ~30 heartbeats in 300 ms: none of them is counted."""
    sim = ecu_acu
    can = sim.can("can_acu")
    t0 = -(-sim.now_us() // 500) * 500 + 500
    end = flood(can, t0, 300)
    sim.run_for(us=end - sim.now_us())
    stop_flood(can)
    assert reg(sim, "ecu", FDCAN2, TXFQS) & TFQF
    assert sim.read_symbol("ecu", "g_can_tx_dropped", 4) > 0


# -- exit criterion 3: ACK ----------------------------------------------------

def test_a_lone_ecu_goes_error_passive_and_recovers_when_acked(images):
    """The inverter bus with no inverter and the probe listening only: the
    ECU's 0x360 gets no ACK, TEC climbs 8 per attempt to 128 (error-passive,
    PSR.EP; never bus-off) and stays there, retried every frame-to-ACK +
    error frame + intermission + suspend; its TX FIFO fills. The ACU
    heartbeat goes on. With an ACK again the head goes out and TEC drops."""
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu"), arbitration=["can_inv"]) as sim:
        sim.wait_for_app()
        inv = sim.can("can_inv")
        inv.set_ack(False)
        t0 = sim.now_us()
        sim.run_for(ms=500)
        tl = inv.timeline(since_us=t0)
        assert tl and {r.outcome for r in tl} == {"ack"}
        assert inv.frames(since_us=t0) == [], "a frame nobody acknowledged was delivered"
        ecu = next(n for n, v in inv.nodes().items() if v["kind"] == "controller")
        assert inv.nodes()[ecu]["tec"] == 128
        assert reg(sim, "ecu", FDCAN1, ECR) & 0xFF == 128
        psr = reg(sim, "ecu", FDCAN1, PSR)
        assert psr & EP and psr & EW and not psr & BO, f"PSR {psr:#x}"
        assert psr & 0x7 in (3, 7), f"LEC {psr & 7}"          # Ack error (7: unchanged since)
        assert reg(sim, "ecu", FDCAN1, TXFQS) & TFQF
        head = tl[-1]
        to_ack = canframe.frame_bits(head.id, head.data) - 10 + 2
        period = (to_ack + 6 + 8 + 3 + 8) * BIT_NS
        assert {b.start_ns - a.start_ns for a, b in zip(tl[-20:], tl[-19:])} == {period}
        assert {r.id for r in tl} <= set(INV_SETPOINTS)
        assert sim.can("can_acu").count([VCU_HEARTBEAT], since_us=t0) >= 48

        inv.set_ack(True)
        t1 = sim.run_for(ms=100)
        ok = [r for r in inv.timeline(since_us=t1 - 100_000) if r.outcome == "ok"]
        assert len(ok) >= 20
        assert inv.nodes()[ecu]["tec"] < 128 and not reg(sim, "ecu", FDCAN1, PSR) & EP
        assert reg(sim, "ecu", FDCAN1, TXFQS) & TFQF == 0


def test_a_lone_ams_gives_up_each_frame_and_stays_error_passive(images):
    """The AMS (AutoRetransmission DISABLE) alone on its bus: one attempt per
    frame, each an ACK error and cancelled, TEC to 128 and no further, no
    bus-off, so no Stop/Start recovery (it reacts to PSR.BO only:
    acu_can_task.cpp:140-175) and it stays in Start."""
    with Sim(REPO / "systems" / "ams.yaml", images("ams"), arbitration=["can_acu"]) as sim:
        sim.wait_for_app()
        acu = sim.can("can_acu")
        acu.set_ack(False)
        t0 = sim.run_for(ms=2000)
        tl = acu.timeline(since_us=t0 - 2_000_000)
        assert len(tl) >= 20 and {r.outcome for r in tl} == {"ack"}
        # Each frame tried once: no two attempts of one frame back to back.
        assert all(a.offer_ns != b.offer_ns or a.id != b.id for a, b in zip(tl, tl[1:]))
        ams = next(n for n, v in acu.nodes().items() if v["kind"] == "controller")
        assert acu.nodes()[ams]["tec"] == 128
        psr = reg(sim, "ams", FDCAN1, PSR)
        assert psr & EP and not psr & BO
        assert reg(sim, "ams", FDCAN1, TXFQS) & TFQF == 0, "cancelled frames stayed queued"
        assert int(sim.monitor("sysbus.fdcan1_h7 BusOffRecoveries", board="ams").strip(), 16) == 0
        assert sim.read_symbol("ams", "g_state_telemetry") == 0


# -- exit criterion 4: a bus-load scenario from the car ---------------------------

class Car:
    """ecu-ams armed to Run (as tests/sim/test_sys_ecu_ams.py's Rig: the
    inverter's DC link follows the AMS's contactors), on an arbitrated ACU
    bus."""

    def __init__(self, sim):
        from tests.sim.test_sys_ecu_ams import Rig
        self.rig = Rig(sim)
        self.sim = sim
        self.acu = sim.can("can_acu")

    def arm_to_run(self):
        self.sim.wait_for_app()
        self.rig.run(3000)
        self.rig.arm()
        assert self.rig.wait_ams(3, 1000) is not None, f"AMS state {self.rig.ams()}"
        self.rig.run(500)
        assert (self.rig.ams(), self.rig.ts_active()) == (3, 1)


@pytest.fixture
def car(images):
    with Sim(REPO / "systems" / "ecu-ams.yaml", images("ecu-ams"), arbitration=["can_acu"]) as sim:
        car = Car(sim)
        car.arm_to_run()
        yield car


def test_a_short_logfs_burst_leaves_the_car_in_run(car):
    """A 30 ms LOGFS-sized burst on 0x012 (diag_dispatch.hpp: ~17-33 ms)
    holds the ECU's 0x100 off the bus for its length, well inside VcuStale's
    200 ms: the AMS stays in Run and the ECU sees it ready."""
    t0 = -(-car.sim.now_us() // 500) * 500 + 500
    end = flood(car.acu, t0, 30)
    car.rig.run(30)
    # The flood starts at the sync point t0, 0.5-1 ms after the run began,
    # so it holds the bus from t0 to here: a little under 30 ms.
    held_ms = (car.sim.now_us() - t0) / 1000
    stop_flood(car.acu)
    car.rig.run(500)
    hb = sorted(r.start_ns for r in car.acu.timeline(since_us=t0 - 20_000) if r.id == VCU_HEARTBEAT)
    gap = max(b - a for a, b in zip(hb, hb[1:])) / 1e6
    assert held_ms <= gap < 200, f"longest 0x100 gap {gap} ms, the flood held {held_ms} ms"
    assert (car.rig.ams(), car.rig.ams("g_fault_reason_telemetry")) == (3, 0)
    assert car.rig.ts_active() == 1
    assert end > t0


def test_a_long_logfs_flood_starves_the_heartbeat_into_vcu_stale(car):
    """The hazard diag_dispatch.hpp:37-43 forbids pulls in Run for: a long
    stream on 0x012 out-prioritises the ECU's 0x100 for good; the AMS latches
    VcuStale 200 ms after the last heartbeat it got (one 10 ms safety tick
    of slack) and opens the AIRs, and the ECU, whose view of the AMS starves
    too, drops ts_active."""
    t0 = -(-car.sim.now_us() // 500) * 500 + 500
    flood(car.acu, t0, 600)
    elapsed = car.rig.wait_ams(5, 600)          # polled every 10 ms
    t_error = car.sim.now_us()
    stop_flood(car.acu)
    assert elapsed is not None, f"AMS still in {car.rig.ams()}"
    assert car.rig.ams("g_fault_reason_telemetry") == 11            # VcuStale
    hb = [r for r in car.acu.timeline(since_us=t0 - 20_000)
          if r.id == VCU_HEARTBEAT and r.outcome == "ok" and r.start_ns <= t_error * 1000]
    last_rx = max(r.end_ns for r in hb) // 1000
    assert last_rx <= t0 + 2_000, "a heartbeat got through the flood"
    # VcuStale is age > 200 ms at a 10 ms safety tick; the poll adds <= 10 ms.
    assert 200_000 < t_error - last_rx <= 200_000 + 10_000 + 10_000, t_error - last_rx
    car.rig.run(400)
    assert car.rig.ts_active() == 0
