"""ams-telemetry (#39): state of charge, firmware health and post-mortem
frames, and the AMS with no IMU fitted, in virtual time.

AMS facts (IFS08-CE-AMS):
  0x130 every 250 ms: SoC %, 0xFF = unknown (acu_soc.def). A Kalman filter
    seeded from the minimum cell on the OCV table once every module has
    reported, corrected from it every 50 ms; invalidated (0xFF) while the
    pack current sensor faults or goes stale (current_task.cpp update_soc,
    soc_estimator.hpp OcvCellMv / OcvSocPermille). 3700 mV sits between
    3655 mV (50 %) and 3843 mV (70 %): ~55 %.
  0x6CA every second, ungated: BE u16 free / min-free heap, [4] liveness
    (bit 0 SafetyTask, 1 AcuCanTask RX pass, 2 CAN TX scheduler, 3
    BmsPollTask; sampled and cleared per frame; bit 1 is poked on every
    AcuCanTask pass, frame or not, acu_can_task.cpp:594-595, although
    fw_health.hpp describes it as "handled an RX frame"), [5] reset cause, [6] uptime s (wraps),
    [7] last fault (0 clean) (ams_fw_health.def, fw_health.hpp).
  0x6C5 (pit-diag): stack-overflow seen / watermark / task, malloc-failed
    count (pit_post_mortem.def).
  A dead IMU costs one short I2C attempt per second, nothing more
    (ams_config.hpp ImuRetryPeriodMs); the BMI088 model's dies are made to
    NACK ("Respond false") for that case.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

SOC, FW_HEALTH, POST_MORTEM = 0x130, 0x6CA, 0x6C5
PIT_ARM, VCU = 0x7F0, 0x100
UNKNOWN = 0xFF
LIVE_SAFETY, LIVE_RX, LIVE_TX, LIVE_BMS = 1, 2, 4, 8


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=3000)
        yield sim


def _soc(ams):
    return ams.can("can_acu").last(SOC).data[0]


def test_soc_seeds_from_the_resting_cells(ams):
    """3700 mV cells: about 55 %, not the unknown sentinel."""
    ams.run_for(ms=1000)
    assert abs(_soc(ams) - 55) <= 2, f"0x130 {_soc(ams)} %"


def test_soc_follows_the_cells(ams):
    """Every cell at 3994 mV (90 % on the OCV table): the filter's voltage
    correction pulls the estimate up past 80 % within 30 s, and never past
    100."""
    ams.monitor("sysbus.spi1.isospi SetAllCells 3994", board="ams")
    ams.run_for(ms=30_000)
    values = {f.data[0] for f in ams.can("can_acu").frames(SOC, since_us=ams.now_us() - 5_000_000)}
    assert all(80 <= v <= 100 for v in values), f"0x130 {sorted(values)}"


def test_soc_is_unknown_while_the_current_sensor_is_out(ams):
    """A disconnected OUT_P leg (reason 8) invalidates the estimate: 0xFF,
    not a stale number."""
    ams.io("ams").set_voltage("PF7", 0.0)
    ams.run_for(ms=1000)
    assert _soc(ams) == UNKNOWN


@pytest.mark.xfail(strict=True, reason=(
    "stm32-can-bootloader#193: v1.7.0 bl_health.c:59-63 clears RCC_RSR before the jump: "
    "capture_reset_cause (fw_health.cpp:37-47) reads no flag and reports 0, not PowerOn"))
def test_fw_health_heap_uptime_and_clean_boot(ams):
    """0x6CA without arming: heap reported and not leaking, uptime counting
    seconds, power-on cause, no last fault."""
    t0 = ams.now_us()
    ams.run_for(ms=10_000)
    frames = ams.can("can_acu").frames(FW_HEALTH, since_us=t0)
    assert len(frames) >= 9
    free = [int.from_bytes(f.data[0:2], "big") for f in frames]
    low = [int.from_bytes(f.data[2:4], "big") for f in frames]
    assert all(0 < m <= f for f, m in zip(free, low)), list(zip(free, low))
    assert len(set(low)) == 1, f"min-free heap kept falling: {low}"
    up = [f.data[6] for f in frames]
    assert all((b - a) % 256 == 1 for a, b in zip(up, up[1:])), f"uptime {up}"
    assert {(f.data[5], f.data[7]) for f in frames} == {(1, 0)}, "reset cause / last fault"


@pytest.mark.parametrize("traffic", [False, True], ids=["quiet-bus", "vcu-traffic"])
def test_every_task_reports_alive(ams, traffic):
    """Liveness: all four bits every second, on a quiet bus or a busy one."""
    can = ams.can("can_acu")
    if traffic:
        can.send_periodic("vcu", VCU, bytes(3), period_ms=10)
    ams.run_for(ms=1100)
    t0 = ams.now_us()
    ams.run_for(ms=3000)
    expected = LIVE_SAFETY | LIVE_RX | LIVE_TX | LIVE_BMS
    assert {f.data[4] for f in can.frames(FW_HEALTH, since_us=t0)} == {expected}


def test_post_mortem_is_clean(ams):
    """0x6C5: no stack overflow and no failed allocation in a normal run."""
    ams.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    ams.run_for(ms=5000)
    d = ams.can("can_acu").last(POST_MORTEM).data
    assert d[0] == 0, f"stack overflow seen, task 0x{int.from_bytes(d[2:6], 'little'):08X}"
    assert int.from_bytes(d[6:8], "little") == 0, "malloc failed"


def test_a_dead_imu_costs_nothing(ams):
    """No IMU answers on I2C2: the AMS stays in Start, healthy, its
    telemetry on time."""
    for die in ("imu_acc", "imu_gyr"):
        ams.monitor(f"sysbus.i2c2_h7.{die} Respond false", board="ams")
    t0 = ams.now_us()
    ams.run_for(ms=10_000)
    assert ams.read_symbol("ams", "g_state_telemetry") == 0
    assert ams.read_symbol("ams", "g_fault_reason_telemetry") == 0
    t = [f.t_us for f in ams.can("can_acu").frames(0x4A0, since_us=t0)]
    assert all(abs((b - a) - 500_000) <= 10_000 for a, b in zip(t, t[1:])), "0x4A0 cadence slipped"
