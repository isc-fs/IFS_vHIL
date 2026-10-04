"""The test probe's CAN stimulus in virtual time (models/renode/VhilProbe.cs),
observed through the ECU, which reacts to a single AMS frame: 0x020 [01]
makes the AMS fresh for AmsStaleMs = 200, and the 100 ms 0x504 then reports
the tractive system active (IFS08-CE-ECU control_task.cpp:117-120,
vcu_ts_active.def)."""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

OK_PRECHARGE, TS_ACTIVE = 0x020, 0x504


@pytest.fixture
def ecu(firmware):
    with Sim(REPO / "systems" / "ecu.yaml", {"ecu": firmware("ecu")}) as sim:
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


def test_sent_reports_the_probes_own_frames_apart_from_frames(ecu):
    """What the probe sent, stamped when it went out (the web app's trace
    shows scenario stimuli from it); frames()/count() stay what the bus
    delivered, which never includes the probe's own sends."""
    bus = ecu.can("can_acu")
    t0 = ecu.now_us()
    bus.send_at(t0 + 100_000, OK_PRECHARGE, b"\x01")
    bus.send_periodic("p", OK_PRECHARGE, b"\x02", period_ms=10, start_us=t0 + 200_000)
    ecu.run_for(ms=245)
    bus.stop_periodic("p")
    ecu.run_for(ms=50)
    sent = bus.sent(since_us=t0)
    # A periodic start set from the monitor goes out up to one sync quantum
    # (500 us) late: Tick re-hops until time reaches it. sent() shows when
    # the frames really went out, which is the point of it.
    start = sent[1].t_us - t0
    assert 200_000 <= start <= 200_500, start
    assert [(f.t_us - t0, f.id, f.data) for f in sent] == \
        [(100_000, OK_PRECHARGE, b"\x01")] + [(start + 10_000 * i, OK_PRECHARGE, b"\x02")
                                             for i in range(5)]
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
