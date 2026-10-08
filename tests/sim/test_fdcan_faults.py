"""FDCAN fault hooks (#22): the firmware's reaction to bus-off, a full TX FIFO
and a failed bring-up, forced through models/renode/Stm32H7Fdcan.cs.

Firmware facts:
  AMS (IFS08-CE-AMS acu_can_task.cpp:140-175): AcuCanTask polls PSR.BO each
    loop; on bus-off it does HAL_FDCAN_Stop/Start, at most every
    FdcanBusOffRetryMs = 100 ms (ams_config.hpp:580), and counts it in
    g_fdcan1_busoff_recovery_count, sent on pit-diag 0x6C9 bytes 0-3 once
    0x7F0 DE AD BE EF arms the stream (pit_comms_health.def).
  ECU (IFS08-CE-ECU app_init_task.cpp): FDCAN2 (ACU) is the load-bearing bus;
    a dead FDCAN1 (inverter) must not take down the 0x100 heartbeat on it.
  Both: HAL_FDCAN_Init failure in MX_FDCANx_Init calls Error_Handler()
    (AMS main.c:504-506, ECU fdcan.c:70).
  ECU FDCAN2: prescaler 3, TSEG1 10, TSEG2 5 on the 24 MHz HSE kernel clock
    = 500 kbit/s (fdcan.c:96-99,242); a pit-diag tick (0x7E0 DE AD BE EF
    arms it) puts 15+ frames on the ACU bus at once (control_task.cpp:377-396).
The error physics (TEC/REC, ACK, bit errors) stays on the physical bench.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

PIT_ARM, COMMS_HEALTH, AMS_STATUS, VCU_HEARTBEAT = 0x7F0, 0x6C9, 0x4A0, 0x100
RETRY_MS = 100


def _recoveries(frame):
    return int.from_bytes(frame.data[0:4], "little")


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=1500)                       # booted, listening
        sim.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))
        sim.run_for(ms=1000)
        yield sim


@pytest.fixture
def ams_quiet(images):
    """The AMS without the pit-diag stream."""
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=2500)
        yield sim


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        sim.wait_for_app()                      # past the bootloader's window
        yield sim


@pytest.fixture
def ecu_powered(images):
    """The ECU at power-on, still in its bootloader: for a hook the app's
    own FDCAN bring-up must see."""
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        yield sim


def _fdcan(sim, board, n, command):
    return sim.monitor(f"sysbus.fdcan{n}_h7 {command}", board=board).strip()


# -- AMS ----------------------------------------------------------------------

def test_ams_recovers_from_bus_off(ams):
    """J-130: bus-off -> Stop/Start within the retry period, and the AMS talks again."""
    _fdcan(ams, "ams", 1, "ForceBusOff")
    t = ams.run_for(ms=RETRY_MS + 100)
    assert _fdcan(ams, "ams", 1, "BusOff") == "False", "still bus-off after the retry period"
    t = ams.run_for(ms=1500)
    can = ams.can("can_acu")
    assert can.count([AMS_STATUS], since_us=t - 1_000_000) >= 1, "no 0x4A0 after recovery"
    assert _recoveries(can.last(COMMS_HEALTH)) == 1


def test_ams_counts_each_recovery(ams):
    """J-131: one count per recovery."""
    for _ in range(3):
        _fdcan(ams, "ams", 1, "ForceBusOff")
        ams.run_for(ms=RETRY_MS + 200)
    ams.run_for(ms=1500)
    assert _recoveries(ams.can("can_acu").last(COMMS_HEALTH)) == 3
    assert int(_fdcan(ams, "ams", 1, "BusOffRecoveries"), 16) == 3


def test_ams_no_recovery_on_a_healthy_bus(ams):
    """J-132: the count stays put when nothing is wrong."""
    ams.run_for(ms=10_000)
    assert _recoveries(ams.can("can_acu").last(COMMS_HEALTH)) == 0


def _bus_off_recurring(sim):
    """No ACK, or a wiring fault: bus-off again right after each rejoin.
    Returns the recovery count every 50 ms over 2 s."""
    counts = []
    for _ in range(40):
        if _fdcan(sim, "ams", 1, "BusOff") == "False":
            _fdcan(sim, "ams", 1, "ForceBusOff")
        sim.run_for(ms=50)
        counts.append(int(_fdcan(sim, "ams", 1, "BusOffRecoveries"), 16))
    return counts


def test_ams_keeps_recovering_when_bus_off_recurs(ams_quiet):
    """The TX FIFO fills while bus-off, but the non-blocking sends just count
    a failure, and each Stop sets CCE, which cancels pending requests:
    recoveries keep the retry pace."""
    counts = _bus_off_recurring(ams_quiet)
    assert counts[-1] - counts[19] >= 7, f"recoveries stalled: {counts}"   # 1 s at ~10/s


@pytest.mark.xfail(strict=True, reason="IFS08-CE-AMS#604: the pit-diag burst blocks on the "
                   "full TX FIFO, so the recovery poll never runs again")
def test_ams_keeps_recovering_when_bus_off_recurs_during_pit_diag(ams):
    counts = _bus_off_recurring(ams)
    assert counts[-1] - counts[19] >= 7, f"recoveries stalled: {counts}"


def test_ams_full_tx_fifo_pauses_then_resumes(ams):
    can = ams.can("can_acu")
    _fdcan(ams, "ams", 1, "TxFifoFull true")
    t = ams.run_for(ms=1500)
    assert can.count(since_us=t - 1_000_000) == 0, "frames sent through a full TX FIFO"
    _fdcan(ams, "ams", 1, "TxFifoFull false")
    t = ams.run_for(ms=1500)
    assert can.count([AMS_STATUS], since_us=t - 1_000_000) >= 1, "AMS silent after the FIFO freed"
    assert ams.read_symbol("ams", "g_state_telemetry") == 0, "AMS left Start"


# -- ECU ----------------------------------------------------------------------

@pytest.mark.parametrize("fault", ["bus-off", "tx-fifo-full"])
def test_ecu_degraded_fdcan1_keeps_the_acu_heartbeat(ecu, fault):
    """B-002: a degraded inverter bus (FDCAN1) never silences FDCAN2's 0x100."""
    ecu.run_for(ms=1000)
    if fault == "bus-off":
        _fdcan(ecu, "ecu", 1, "ForceBusOff")
    else:
        _fdcan(ecu, "ecu", 1, "TxFifoFull true")
    t = ecu.run_for(ms=1000)
    n = ecu.can("can_acu").count([VCU_HEARTBEAT], since_us=t - 1_000_000)
    assert n >= 95, f"{n} heartbeats in 1 s with FDCAN1 {fault}"


def test_fail_init_fails_the_bring_up(ecu_powered):
    """The hook itself: CCCR.INIT never acknowledges, HAL_FDCAN_Init times out,
    and MX_FDCAN1_Init takes the firmware's Error_Handler path. Set inside the
    bootloader's window, after its own FDCAN bring-up, so the app's meets it."""
    ecu = ecu_powered
    ecu.run_for(ms=1000)
    _fdcan(ecu, "ecu", 1, "FailInit true")
    ecu.wait_for_app()
    ecu.run_for(ms=1000)
    pc = ecu.monitor("sysbus FindSymbolAt `cpu PC`", board="ecu").strip()
    assert pc.startswith("Error_Handler"), f"ECU at {pc}"
    assert ecu.can("can_acu").count([VCU_HEARTBEAT]) == 0


def test_wire_timing_paces_tx_at_the_bit_rate(ecu_powered):
    """The hook itself: NBTP holds what HAL_FDCAN_Init wrote, and a burst
    leaves one frame time apart (>= 47 bits, 94 us at 500 kbit/s) instead of
    in the instant the firmware queued it."""
    ecu = ecu_powered
    _fdcan(ecu, "ecu", 2, "WireTiming true")
    ecu.wait_for_app()
    ecu.run_for(ms=1000)
    nbtp = ecu.monitor("sysbus ReadDoubleWord 0x4000A41C", board="ecu")   # FDCAN2 + NBTP
    assert int(nbtp.strip(), 16) == 0x00020904
    can = ecu.can("can_acu")
    can.send(0x7E0, bytes.fromhex("DEADBEEF"))
    t = ecu.run_for(ms=500)
    status = can.frames([0x700], since_us=t - 300_000)[0].t_us
    tick = [f.t_us for f in can.frames([VCU_HEARTBEAT], since_us=status - 10_000) if f.t_us <= status][-1]
    burst = [f for f in can.frames(since_us=tick) if f.t_us < tick + 9_000]  # that tick's frames
    span_us = burst[-1].t_us - burst[0].t_us
    assert len(burst) >= 15 and span_us >= (len(burst) - 1) * 94, f"{len(burst)} frames in {span_us} us"
