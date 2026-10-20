"""sys-ecu-ams-udv: the ECU, the AMS and the uDV on the car's ACU bus, each
through its CAN bootloader, in virtual time.

ECU facts (IFS08-CE-ECU): 0x100 every 10 ms control tick on the ACU bus, in
  every state (control_task.cpp:305-323); 0x504 VCU_ts_active every 100 ms
  to the uDV (catalog/firmware/ecu.yaml `can`, tests/sim/test_sys_ecu_ams.py).
AMS facts (IFS08-CE-AMS): 0x4A0 status on its FDCAN1, the ACU bus
  (tests/sim/test_sys_ecu_ams.py).
uDV facts (IFS09-DV-uDV, udv@dev since #12): 0x504 accepted by the FDCAN2
  range filter 0x504..0x511 (can_interface.cpp initEcu) and stamped into
  g_can_ts_active_stamp_ms on every frame (rx_dispatch, CAN_ID_TS_ACTIVE);
  the data-logger frames 0x500-0x502 every 100 ms on its DV bus
  (can_task.cpp:116-120); boot trigger 0x002 B0 07 AD 13 on FDCAN2, honoured
  in AS OFF (bl_entry.h). The ECU's trigger is ..AD 12 and the AMS's ..AD 11
  on the same id and wire: one frame reboots one board (can-flasher
  src/app_control.rs).
"""
import pytest

from vhil import can_bootloader as cb

# Impact selection (ci/impact.yaml, docs/ci.md): boards, systems, subsystems.
pytestmark = [pytest.mark.ecu, pytest.mark.ams, pytest.mark.udv, pytest.mark.system_ecu_ams_udv,
              pytest.mark.can]

HEARTBEAT, AMS_STATUS, TS_ACTIVE = 0x100, 0x4A0, 0x504
DL_DYN1 = 0x500
UDV_TRIGGER = (0x002, bytes.fromhex("B007AD13"))
BKP0R = 0x58004050


@pytest.fixture(scope="module")
def car(make_sim):
    sim = make_sim("ecu-ams-udv")
    sim.run_for(ms=1000)
    return sim


def _in_bootloader(sim, board):
    return int(sim.monitor("cpu PC", board=board).strip(), 16) < cb.APP_BASE


def _stamp(sim):
    return sim.read_symbol("udv", "g_can_ts_active_stamp_ms", 4)


def test_all_three_boot_through_their_bootloaders(car):
    t = car.now_us() - 500_000
    acu = car.can("can_acu")
    assert not any(_in_bootloader(car, b) for b in ("ecu", "ams", "udv"))
    assert acu.count([HEARTBEAT], t) >= 45, "the ECU is not running"
    assert acu.count([AMS_STATUS], t) >= 1, "the AMS is not running"
    assert car.can("can_dv").count([DL_DYN1], t) >= 4, "the uDV is not running"


def test_the_udv_hears_the_ecu_on_the_acu_bus(car):
    """The ECU's 0x504 reaches the uDV through its FDCAN2 filter."""
    assert car.can("can_acu").count([TS_ACTIVE], car.now_us() - 500_000) >= 4
    before = _stamp(car)
    car.run_for(ms=300)
    assert _stamp(car) > before > 0, "the uDV stamps no 0x504"


def test_the_udv_trigger_reboots_the_udv_alone(car):
    """0x002 ..AD 13 on the shared bus: the uDV parks in its bootloader, and
    the ECU's heartbeat and the AMS's status never stop."""
    car.can("can_acu").send(*UDV_TRIGGER)
    t = car.run_for(ms=500)
    assert _in_bootloader(car, "udv")
    assert not _in_bootloader(car, "ecu") and not _in_bootloader(car, "ams")
    beats = car.can("can_acu").frames([HEARTBEAT], t - 500_000)
    assert len(beats) >= 45 and max(b.t_us - a.t_us for a, b in zip(beats, beats[1:])) <= 20_000
    assert car.can("can_acu").count([AMS_STATUS], t - 500_000) >= 1
    assert car.can("can_dv").count([DL_DYN1], t - 200_000) == 0, "the uDV app still runs"
