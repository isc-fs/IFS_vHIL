"""ECU boot and liveness in virtual time: the first slice of ecu-boot (M4, #12).

Firmware facts (IFS08-CE-ECU):
  ControlPeriodMs = 10, DiagPeriodMs = 1000          ecu_config.hpp:22-23
  0x100 every ControlTask tick, inverter or not       control_task.cpp:305-323
  0x704 health every DiagPeriodMs, ungated            diag_task.cpp:53-65
  OK_STATUS (PD14) high, ERR_STATUS (PD15) low
  unless the state is AmsError, every tick            control_task.cpp:372-375
"""
import pytest

from vhil.sim import assert_period

HEARTBEAT = 0x100
HEALTH = 0x704
CONTROL_PERIOD_US = 10_000
DIAG_PERIOD_US = 1_000_000
OK_STATUS = ("sysbus.gpioPortD", 14)
ERR_STATUS = ("sysbus.gpioPortD", 15)

BOOT_MS = 3500


@pytest.fixture(scope="module")
def booted(make_sim):
    """One ECU, outputs watched from reset, run for BOOT_MS of virtual time."""
    sim = make_sim("ecu")
    io = sim.io("ecu")
    pins = {"ok": io.watch(*OK_STATUS), "err": io.watch(*ERR_STATUS)}
    sim.run_for(ms=BOOT_MS)
    return sim, pins


def test_heartbeat_every_control_tick(booted):
    sim, _ = booted
    hb = sim.can("can_acu").frames(HEARTBEAT)
    # Exact from the second frame on: the task's first wake is aligned to the
    # scheduler start, every later one to the previous (osDelayUntil).
    assert_period(hb[1:], period_us=CONTROL_PERIOD_US, tolerance_us=0, min_count=100)


def test_heartbeat_starts_within_two_ticks_of_reset(booted):
    sim, _ = booted
    first = sim.can("can_acu").frames(HEARTBEAT)[0]
    assert first.t_us <= 2 * CONTROL_PERIOD_US, f"first 0x100 at {first.t_ms} ms"


def test_heartbeat_only_on_acu_bus(booted):
    sim, _ = booted
    for bus in ("can_inv", "can_dash"):
        assert sim.can(bus).count(HEARTBEAT) == 0, f"0x100 on {bus}"


def test_health_every_diag_period(booted):
    sim, _ = booted
    health = sim.can("can_acu").frames(HEALTH)
    assert_period(health[1:], period_us=DIAG_PERIOD_US, tolerance_us=0, min_count=2)


def test_status_leds_ok_from_first_tick(booted):
    sim, pins = booted
    io = sim.io("ecu")
    ok = io.edges(pins["ok"])
    first_hb = sim.can("can_acu").frames(HEARTBEAT)[0]
    assert [e.level for e in ok] == [True], f"OK_STATUS edges: {ok}"
    # The LED write follows the 0x100 post in the same tick, so it is set
    # no later than one tick after the first heartbeat.
    assert ok[0].t_us <= first_hb.t_us + CONTROL_PERIOD_US
    assert io.edges(pins["err"]) == [], "ERR_STATUS lit without AmsError"


def test_same_trace_at_any_speed(make_sim):
    """The M4 exit criterion: a scenario gives the same trace whether Renode
    runs flat out or is paced to real time."""
    traces = []
    for fast in (True, False):
        sim = make_sim("ecu", advance_immediately=fast)
        sim.run_for(ms=500)
        traces.append([(f.t_us, f.id, f.data) for bus in ("can_inv", "can_dash", "can_acu")
                       for f in sim.can(bus).frames()])
        sim.stop()
    assert traces[0], "no frames at all"
    assert traces[0] == traces[1]
