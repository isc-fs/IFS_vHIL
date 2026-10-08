"""ecu-as-buzzer: the AS Emergency tone the ECU sounds on the RTDS buzzer, in
virtual time.

The rule (FSG DV, as the firmware quotes it, as_buzzer.hpp:5-8,
udv_as_status.def:6-8): AS Emergency is indicated acoustically, an
intermittent sound at 50 % duty in the 1-5 Hz band, for 10 s.

ECU facts (IFS08-CE-ECU, Core/ unless shown):
  vehicle_service.cpp:282-287: 0x50A (UDV_as_status, ACU bus, DLC >= 1)
    byte 0 = the uDV's AS state, stamped with its own tick.
  control_task.cpp:162-164: as_fresh = 0x50A within UdvAsStaleMs = 400
    (ecu_config.hpp:53; is_fresh is age <= window, vehicle_service.cpp:128-133).
  as_buzzer.cpp:20-63, called every control tick in every state
    (control.cpp:355-363): a rising edge into Emergency (0x01) starts a
    AsEmergencySoundMs = 10000 window (ecu_config.hpp:49), the first frame
    counting as an edge; the tone is on while (elapsed / 150) is even
    (AsBuzzerHalfPeriodMs = 150, ecu_config.hpp:45): 150 ms on, 150 ms off,
    the last pulse cut at 10 s (9900..10000 ms). The window runs on the
    ECU's clock: the level staying Emergency does not extend it, and a new
    edge restarts it.
  Stale fail-safe: 0x50A going silent while the last state seen was Driving
    (0x03) or Ready (0x02) starts the tone once per silence (re-armed when
    frames return); silence after Off (0x00) or Finished (0x04), or a uDV
    never heard (a manual car), never sounds.
  control_task.cpp:370-371: RTDS (PB4, main.h:82-83) = R2D's tone OR this one.
  pit_diag.cpp:68-74 / pit_diag_dv.def:13-21: 0x707 byte 0 bit 5
    as_emergency (the window is running), bit 6 as_from_stale, bit 7 as_fresh;
    byte 4 the raw status. 0x707 streams every 100 ms once 0x7E0 DEADBEEF arms it.
The ECU is parked in WaitInvVdcConfig (no inverter): the tone does not depend
on the drive state, and nothing else drives the RTDS there.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

AS_STATUS, PIT_CMD, PIT_DV = 0x50A, 0x7E0, 0x707
OFF, EMERGENCY, READY, DRIVING, FINISHED = range(5)
TICK_MS, HALF_MS, SOUND_MS, STALE_MS = 10, 150, 10_000, 400
JITTER_US = 2_000          # a control tick's compute, well under one tick
RTDS = ("sysbus.gpioPortB", 4)
AS_EMERGENCY, AS_FROM_STALE, AS_FRESH = 0x20, 0x40, 0x80


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        sim.wait_for_app()                      # past the bootloader's window
        sim.can("can_acu").send(PIT_CMD, bytes.fromhex("DEADBEEF"))
        sim.run_for(ms=50)
        yield sim


def _udv(ecu, status, first=True):
    """The uDV's 0x50A at its 10 Hz (udv_as_status.def:26); first=False
    changes the status of the running stream. Returns when the first frame
    with that status went out (an update waits for the stream's next slot)."""
    acu = ecu.can("can_acu")
    t = ecu.now_us()
    if first:
        acu.send_periodic("as", AS_STATUS, bytes([status]), 100)
    else:
        acu.update_periodic("as", bytes([status]))
    ecu.run_for(ms=120)
    return next(f.t_us for f in acu.sent([AS_STATUS], since_us=t) if f.data[0] == status)


def _rises_and_falls(edges):
    rises = [e.t_us for e in edges if e.level]
    falls = [e.t_us for e in edges if not e.level]
    return rises, falls


def _assert_one_tone(edges, start_us):
    """Exactly one 10 s tone from start_us: 34 pulses of 150 ms on / 150 ms
    off, the last cut to 100 ms at the 10 s mark."""
    rises, falls = _rises_and_falls(edges)
    assert rises and abs(rises[0] - start_us) <= JITTER_US, \
        f"tone starts at {rises[:1]}, expected {start_us} us"
    assert len(rises) == len(falls) == SOUND_MS // (2 * HALF_MS) + 1, \
        f"{len(rises)} pulses, {len(falls)} ends"
    for k, (r, f) in enumerate(zip(rises, falls)):
        on = HALF_MS if k < len(rises) - 1 else SOUND_MS - k * 2 * HALF_MS
        assert abs(r - (start_us + k * 2 * HALF_MS * 1000)) <= JITTER_US, f"pulse {k} rises at {r}"
        assert abs((f - r) / 1000 - on) <= JITTER_US / 1000, f"pulse {k} lasts {(f - r) / 1000} ms"
    assert abs(falls[-1] - (start_us + SOUND_MS * 1000)) <= JITTER_US, \
        f"tone lasts {(falls[-1] - rises[0]) / 1000} ms, not {SOUND_MS}"


def _dv_flags(ecu, since_us, until_us=None):
    return [(f.t_us, f.data[0], f.data[4]) for f in ecu.can("can_acu").frames([PIT_DV], since_us)
            if until_us is None or f.t_us <= until_us]


def test_a_manual_car_never_sounds(ecu):
    """No uDV fitted, no 0x50A ever: never a tone (the fail-safe is gated on
    having seen the uDV mid-mission, as_buzzer.cpp:36-45)."""
    io = ecu.io("ecu")
    pin = io.watch(*RTDS)
    t0 = ecu.now_us()
    ecu.run_for(ms=SOUND_MS + 2000)
    assert not [e for e in io.edges(pin) if e.level], "the RTDS sounded with no uDV"
    flags = _dv_flags(ecu, t0)
    assert flags and all(b0 & (AS_EMERGENCY | AS_FROM_STALE | AS_FRESH) == 0 for _, b0, _ in flags)


def test_an_emergency_sounds_ten_seconds_of_150_ms_pulses(ecu):
    """The first frame is already Emergency (an edge, as_buzzer.cpp:30-32):
    3.3 Hz at 50 % duty for exactly 10 s, then silence although the uDV keeps
    reporting Emergency (edge-triggered, as_buzzer.hpp:16-21)."""
    io = ecu.io("ecu")
    pin = io.watch(*RTDS)
    t0 = _udv(ecu, EMERGENCY)
    ecu.run_for(ms=SOUND_MS + 3000)
    edges = io.edges(pin, since_us=t0)
    rises, _ = _rises_and_falls(edges)
    assert rises, "no tone"
    assert rises[0] - t0 <= 3 * TICK_MS * 1000, f"tone {(rises[0] - t0) / 1000} ms after the frame"
    _assert_one_tone(edges, rises[0])
    assert not io.level(pin)
    during = _dv_flags(ecu, rises[0] + 200_000, rises[0] + (SOUND_MS - 200) * 1000)
    after = _dv_flags(ecu, rises[0] + (SOUND_MS + 200) * 1000)
    assert during and all(b0 & AS_EMERGENCY and not b0 & AS_FROM_STALE and b0 & AS_FRESH
                          and st == EMERGENCY for _, b0, st in during)
    assert after and all(not b0 & AS_EMERGENCY and b0 & AS_FRESH for _, b0, _ in after)


@pytest.mark.parametrize("status", [DRIVING, READY])
def test_a_udv_silent_mid_mission_sounds_once(ecu, status):
    """0x50A stops while the uDV reported Driving or Ready: the tone starts
    on the first tick the frame is older than 400 ms, runs 10 s, flagged
    as_from_stale, and does not repeat while the silence lasts."""
    io = ecu.io("ecu")
    pin = io.watch(*RTDS)
    _udv(ecu, status)
    ecu.run_for(ms=1000)
    acu = ecu.can("can_acu")
    acu.stop_periodic("as")
    last = acu.sent([AS_STATUS])[-1].t_us
    ecu.run_for(ms=2 * SOUND_MS)
    edges = io.edges(pin, since_us=last)
    rises, _ = _rises_and_falls(edges)
    assert rises, "a uDV gone quiet mid-mission sounded nothing"
    late_ms = (rises[0] - last) / 1000
    assert STALE_MS < late_ms <= STALE_MS + 2 * TICK_MS, f"tone {late_ms} ms after the last 0x50A"
    _assert_one_tone(edges, rises[0])
    during = _dv_flags(ecu, rises[0] + 200_000, rises[0] + (SOUND_MS - 200) * 1000)
    assert during and all(b0 & AS_EMERGENCY and b0 & AS_FROM_STALE and not b0 & AS_FRESH
                          for _, b0, _ in during)


def test_the_stale_fail_safe_rearms_when_frames_return(ecu):
    """One tone per silence: frames back (Driving), then a second silence,
    a second tone (as_buzzer.cpp:23-25)."""
    io = ecu.io("ecu")
    pin = io.watch(*RTDS)
    acu = ecu.can("can_acu")
    tones = []
    for _ in range(2):
        acu.send_periodic("as", AS_STATUS, bytes([DRIVING]), 100)
        ecu.run_for(ms=1000)
        acu.stop_periodic("as")
        last = acu.sent([AS_STATUS])[-1].t_us
        ecu.run_for(ms=SOUND_MS + 1000)
        rises, _ = _rises_and_falls(io.edges(pin, since_us=last))
        tones.append(rises[:1])
    assert all(tones), f"tones per silence: {tones}"


@pytest.mark.parametrize("status", [OFF, FINISHED])
def test_silence_after_off_or_finished_is_not_an_emergency(ecu, status):
    """A uDV that reported Off or Finished and then stops talking is not
    mid-mission: no tone."""
    io = ecu.io("ecu")
    pin = io.watch(*RTDS)
    _udv(ecu, status)
    ecu.run_for(ms=1000)
    ecu.can("can_acu").stop_periodic("as")
    ecu.run_for(ms=3000)
    assert not [e for e in io.edges(pin) if e.level], "the RTDS sounded"


def test_a_second_emergency_restarts_the_window(ecu):
    """Emergency, Ready 3 s in (the tone runs on, on its own clock),
    Emergency again 1 s later: the window restarts at the second edge, so
    the tone ends 10 s after it (as_buzzer.cpp:12-18)."""
    io = ecu.io("ecu")
    pin = io.watch(*RTDS)
    t1 = _udv(ecu, EMERGENCY)
    ecu.run_for(ms=3000)
    _udv(ecu, READY, first=False)
    ecu.run_for(ms=1000)
    t2 = _udv(ecu, EMERGENCY, first=False)
    ecu.run_for(ms=SOUND_MS + 2000)
    rises, falls = _rises_and_falls(io.edges(pin, since_us=t1))
    assert rises and falls
    period_us = 2 * HALF_MS * 1000
    before = [r for r in rises if r < t2]
    assert before[-1] > t2 - 2 * period_us, "the tone stopped on Ready"
    assert all(min((r - rises[0]) % period_us, period_us - (r - rises[0]) % period_us) <= JITTER_US
               for r in before), "the first tone slipped off its grid on Ready"
    end_ms = (falls[-1] - t2) / 1000
    assert SOUND_MS <= end_ms <= SOUND_MS + 3 * TICK_MS, \
        f"the tone ended {end_ms} ms after the second edge (first edge: " \
        f"{(falls[-1] - t1) / 1000} ms), expected 10 s after the second"
