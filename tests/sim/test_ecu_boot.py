"""ECU boot and liveness in virtual time: the first slice of ecu-boot (M4, #12).

Firmware facts (IFS08-CE-ECU):
  ControlPeriodMs = 10, DiagPeriodMs = 1000          ecu_config.hpp:22-23
  0x100 every ControlTask tick, inverter or not       control_task.cpp:305-323
  0x704 health every DiagPeriodMs, ungated            diag_task.cpp:53-65
  OK_STATUS (PD14) high, ERR_STATUS (PD15) low
  unless the state is AmsError, every tick            control_task.cpp:372-375
  0x704: BE u16 free / min-free heap; byte 4 bits 0-4 = control, CAN RX,
  CAN TX, telemetry, diag stepped since the last frame (CAN RX steps on its
  wait timeout too, so a quiet bus keeps it set), bits 5-7 = bench stubs
  announced (StubNoAms, StubNoInverter, StubStart: all false in the car's
  image); byte 6 uptime s      pit_diag_health.def, diag_task.cpp:36-70,
                                                      can_rx_task.cpp:33-43
"""
import pytest

from vhil.sim import Sim, assert_period
from vhil.system import REPO

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
    sim = make_sim("ecu", wait_for_app=False)     # watched from power-on
    io = sim.io("ecu")
    pins = {"ok": io.watch(*OK_STATUS), "err": io.watch(*ERR_STATUS)}
    sim.wait_for_app()
    sim.run_for(ms=BOOT_MS)
    return sim, pins


def test_heartbeat_every_control_tick(booted):
    sim, _ = booted
    hb = sim.can("can_acu").frames(HEARTBEAT)
    # Exact from the second frame on: the task's first wake is aligned to the
    # scheduler start, every later one to the previous (osDelayUntil).
    assert_period(hb[1:], period_us=CONTROL_PERIOD_US, tolerance_us=0, min_count=100)


def test_heartbeat_starts_within_two_ticks_of_the_app(booted):
    """Within two ticks of the app's start, which follows the bootloader's
    2 s auto-jump window; nothing from the ECU before it."""
    sim, _ = booted
    first = sim.can("can_acu").frames(HEARTBEAT)[0]
    t_us = first.t_us - sim.app_started["ecu"]
    assert 0 <= t_us <= 2 * CONTROL_PERIOD_US, f"first 0x100 {t_us / 1000} ms into the app"


def test_heartbeat_only_on_acu_bus(booted):
    sim, _ = booted
    for bus in ("can_inv", "can_dash"):
        assert sim.can(bus).count(HEARTBEAT) == 0, f"0x100 on {bus}"


def test_health_every_diag_period(booted):
    sim, _ = booted
    health = sim.can("can_acu").frames(HEALTH)
    assert_period(health[1:], period_us=DIAG_PERIOD_US, tolerance_us=0, min_count=2)


def test_status_leds_ok_from_first_tick(booted):
    """The bootloader lights OK_STATUS (PD14) for its window (stm32-can-bootloader
    main.c:481 LED_OK_ON); its handoff resets GPIOD (HAL_DeInit), so the pin
    drops; the app lights it again from its first tick."""
    sim, pins = booted
    io = sim.io("ecu")
    ok = io.edges(pins["ok"])
    first_hb = sim.can("can_acu").frames(HEARTBEAT)[0]
    assert [e.level for e in ok] == [True, False, True], f"OK_STATUS edges: {ok}"
    assert ok[1].t_us <= first_hb.t_us, "OK_STATUS dropped after the app started"
    # The LED write follows the 0x100 post in the same tick, so it is set
    # no later than one tick after the first heartbeat.
    assert ok[2].t_us <= first_hb.t_us + CONTROL_PERIOD_US
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


ALL_TASKS = 0x1F


@pytest.fixture(scope="module")
def running(make_sim):
    """One ECU on quiet buses for 30 s of virtual time."""
    sim = make_sim("ecu")
    sim.run_for(ms=30_000)
    return sim


def test_every_task_steps_every_health_period(running):
    """Task liveness: all five tasks advance between consecutive 0x704s on a
    quiet bus, and no bench stub is announced."""
    frames = running.can("can_acu").frames(HEALTH)
    assert len(frames) >= 28
    assert {f.data[4] for f in frames[1:]} == {ALL_TASKS}


def test_the_heap_settles_and_does_not_leak(running):
    """Heap trace: after boot the free and min-free heap stop moving."""
    frames = running.can("can_acu").frames(HEALTH)[2:]
    free = {int.from_bytes(f.data[0:2], "big") for f in frames}
    low = {int.from_bytes(f.data[2:4], "big") for f in frames}
    assert len(low) == 1, f"min-free heap kept falling: {sorted(low)}"
    assert all(f >= min(low) > 0 for f in free)


def test_uptime_counts_seconds(running):
    up = [f.data[6] for f in running.can("can_acu").frames(HEALTH)]
    assert all(b - a == 1 for a, b in zip(up, up[1:])), f"uptime {up}"


def test_traffic_on_the_other_buses_does_not_disturb_the_acu_bus(images):
    """Bus independence: 1000 frames/s on each of the inverter and dash buses
    (IDs nothing listens to) for 5 s: 0x100 keeps its exact 10 ms, every task
    keeps stepping."""
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=1000)
        for bus in ("can_inv", "can_dash"):
            for k in range(10):
                sim.can(bus).send_periodic(f"{bus}{k}", 0x7A0 + k, bytes([k] * 8), period_ms=10,
                                           start_us=sim.now_us() + 1000 * k)
        t0 = sim.now_us()
        sim.run_for(ms=5000)
        hb = sim.can("can_acu").frames(HEARTBEAT, since_us=t0)
        assert_period(hb, period_us=CONTROL_PERIOD_US, tolerance_us=0, min_count=490)
        assert {f.data[4] for f in sim.can("can_acu").frames(HEALTH, since_us=t0)} == {ALL_TASKS}
