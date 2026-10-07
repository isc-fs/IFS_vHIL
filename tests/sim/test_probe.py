"""The test probe's CAN stimulus in virtual time (models/renode/VhilProbe.cs),
observed through the ECU, which reacts to a single AMS frame: 0x020 [01]
makes the AMS fresh for AmsStaleMs = 200, and the 100 ms 0x504 then reports
the tractive system active (IFS08-CE-ECU control_task.cpp:117-120,
vcu_ts_active.def)."""
import pytest

from vhil import canframe
from vhil.sim import Sim
from vhil.system import REPO

OK_PRECHARGE, TS_ACTIVE = 0x020, 0x504
QUANTUM_US = 500                                # systems/ecu.yaml time.quantum_s
BIT_NS = 2000                                   # 500 kbit/s (the ECU's FDCAN2 NBTP)


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=1000)
        yield sim


def _first_active_after(sim, t0):
    return next((f.t_us for f in sim.can("can_acu").frames(TS_ACTIVE, since_us=t0)
                 if f.data[0] == 1), None)


@pytest.mark.parametrize("lead_ms", [50, 300, 1200])
def test_send_at_a_future_time_arrives_then(ecu, lead_ms):
    """#73: a frame scheduled ahead goes out at its time, not now and not
    never: 0x504 turns active within one 100 ms period after it, never
    before."""
    t0 = ecu.now_us()
    at = t0 + lead_ms * 1000
    ecu.can("can_acu").send_at(at, OK_PRECHARGE, b"\x01")
    ecu.run_for(ms=lead_ms + 300)
    first = _first_active_after(ecu, t0)
    assert first is not None, "the scheduled frame never arrived"
    assert at <= first <= at + 110_000, f"active at +{(first - t0) / 1000} ms, frame due +{lead_ms} ms"


def _on_the_bus(bus, can_id, since_us):
    """The bus model's record (models/renode/VhilCanBus.cs, Timeline) of each
    frame with can_id: offer, start, end and idle again, in virtual time."""
    return [r for r in bus.timeline(since_us=since_us) if r.id == can_id]


def _check_bus_times(bus, records, since_us):
    """Each frame starts at its offer if the bus is idle, else exactly when
    the bus is idle again after the frame on it (ISO 11898-1 bus idle); it
    lasts its exact bits at 500 kbit/s (vhil/canframe.py)."""
    tl = bus.timeline(since_us=since_us - 1000)
    for r in records:
        assert r.outcome == "ok", r
        assert r.end_ns - r.start_ns == canframe.frame_bits(r.id, r.data) * BIT_NS, r
        busy = [o.free_ns for o in tl if o.start_ns < r.start_ns and o.free_ns > r.offer_ns]
        assert r.start_ns == max([r.offer_ns] + busy), (r, busy)


def test_sent_reports_the_probes_own_frames_apart_from_frames(ecu):
    """What the probe sent, stamped when it went out (the web app's trace
    shows scenario stimuli from it): on the arbitrated bus, the end of its
    frame's last EOF bit as the bus model reports it, the frame having
    started at its offer or when the bus was next idle. frames()/count()
    stay what the bus delivered, which never includes the probe's own
    sends."""
    bus = ecu.can("can_acu")
    t0 = ecu.now_us()
    bus.send_at(t0 + 100_000, OK_PRECHARGE, b"\x01")
    bus.send_periodic("p", OK_PRECHARGE, b"\x02", period_ms=10, start_us=t0 + 200_000)
    ecu.run_for(ms=245)
    bus.stop_periodic("p")
    ecu.run_for(ms=50)
    sent = bus.sent(since_us=t0)
    tl = _on_the_bus(bus, OK_PRECHARGE, t0)
    _check_bus_times(bus, tl, t0)
    # Offered when asked: send_at at its time; a periodic start set from
    # the monitor up to one sync quantum (500 us) late (Tick re-hops until
    # time reaches it), then exactly a period apart.
    offers = [r.offer_ns // 1000 - t0 for r in tl]
    start = offers[1]
    assert 200_000 <= start <= 200_000 + QUANTUM_US, start
    assert offers == [100_000] + [start + 10_000 * i for i in range(5)], offers
    assert [(f.t_us, f.id, f.data) for f in sent] == [(r.end_ns // 1000, r.id, r.data) for r in tl]
    assert [f.data for f in sent] == [b"\x01"] + [b"\x02"] * 5
    assert bus.sent(TS_ACTIVE, since_us=t0) == []
    assert bus.frames(OK_PRECHARGE, since_us=t0) == [] and bus.count(OK_PRECHARGE, since_us=t0) == 0
    assert bus.frames(TS_ACTIVE, since_us=t0), "the ECU's own frames still arrive"


def test_send_periodic_with_a_future_start(ecu):
    """A periodic sender starting ahead holds off until its start."""
    t0 = ecu.now_us()
    start = t0 + 500_000
    ecu.can("can_acu").send_periodic("ams", OK_PRECHARGE, b"\x01", period_ms=10, start_us=start)
    ecu.run_for(ms=1000)
    first = _first_active_after(ecu, t0)
    assert first is not None and start <= first <= start + 110_000, first


@pytest.mark.parametrize("lead_ms", [3, 250])
def test_a_periodic_sender_starts_at_the_first_sync_point_after_start(ecu, lead_ms):
    """#130: injections run at Renode's sync points (every time.quantum_s),
    so the first frame is offered to the bus at the first sync point at or
    after start_us, and the rest exactly a period apart; each goes out as
    the bus lets it, and sent() reports its end."""
    bus = ecu.can("can_acu")
    t0 = ecu.now_us()
    start = t0 + lead_ms * 1000 + 137          # off any quantum boundary
    bus.send_periodic("p", OK_PRECHARGE, b"\x01", period_ms=10, start_us=start)
    ecu.run_for(ms=lead_ms + 60)
    bus.stop_periodic("p")
    tl = _on_the_bus(bus, OK_PRECHARGE, t0)
    _check_bus_times(bus, tl, t0)
    first = -(-start // QUANTUM_US) * QUANTUM_US
    assert [r.offer_ns // 1000 for r in tl[:3]] == [first, first + 10_000, first + 20_000], tl[:3]
    assert [f.t_us for f in bus.sent(OK_PRECHARGE, since_us=t0)] == [r.end_ns // 1000 for r in tl]
