"""ams-balancing in the FSM's energised states: the operator's 0x103 master
switch in Charge and in Run, a cell imbalance set on the LTC6811 models, the
discharge (DCC) bits read from the models and from pit-diag. Replaces IFS_HIL
Block BAL (B-01..B-06), which needs the bench's Pico LTC emulator for the
imbalance.

AMS facts (IFS08-CE-AMS main: balance_controller.hpp, vehicle_service.cpp,
acu_can_task.cpp, bms_poll_task.cpp):
  0x103 "BALO" Off, "BALN" On (any state), "BALX" Auto (Charge only); never
    seen or older than BalanceOverrideFreshMs (5 s): Off
    (VehicleService::effective_balance_cmd). IFS_HIL's Block BAL predates
    this: it expects Auto when no 0x103 is fresh (drift).
  0x6C0 byte 2 bit 2 (balance_override): the effective command is Off,
    fresh BALO or the dead-man fallback (acu_can_task.cpp:315-320).
  0x6C2: DCC bits of flat cells 0..63 as a LE u64, bit 19 * m + c
    (pit_balance_mask_a.def, g_balance_dcc_bits, bms_poll_task.cpp:634).
  A cell balances when above the pack's second-lowest by BalanceDeltaMv (50);
    the mask is recomputed every 800 ms. The override never touches the FSM,
    the contactors or AMS_OK: only compute_mask reads it.
"""
import pytest

from ams_car import CHARGE, Car, ERROR, RUN
from vhil.sim import Sim
from vhil.system import REPO

BAL = 0x103
PIT_ARM, PIT_FSM, BAL_MASK_A = 0x7F0, 0x6C0, 0x6C2
OVERRIDE = 0x04                     # 0x6C0 byte 2 bit 2
UPDATE_MS = 2000                    # two mask updates, whatever the phase
HIGH_CHIP, HIGH_CELL = 0, 0         # module 0 cell 0: DCC bit 0 of chip 0, 0x6C2 bit 0
NOMINAL_MV, HIGH_MV = 3700, 3800


@pytest.fixture
def car(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=3000)
        sim.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))
        sim.monitor(f"sysbus.spi1.isospi SetAllCells {NOMINAL_MV}", board="ams")
        sim.monitor(f"sysbus.spi1.isospi.cells{HIGH_CHIP} SetCell {HIGH_CELL} {HIGH_MV}",
                    board="ams")
        yield Car(sim)


def _charge(car):
    """Charger mode: a fresh 0x101, no VCU, TSMS and a press -> Charge."""
    car.charger()
    car.tsms(True)
    car.sim.run_for(ms=1100)
    car.press()
    assert car.wait_for(CHARGE, 500) is not None, f"no Charge (state {car.state()})"


def _command(car, magic: bytes):
    car.can.send_periodic("bal", BAL, magic, period_ms=1000)


def _dcc(car):
    """DCC bits of every chip, the widest of a few looks a few ms apart (the
    firmware clears them for ~2 ms around each ADCV)."""
    seen = [0] * 10
    for _ in range(3):
        for k in range(10):
            seen[k] |= int(car.sim.monitor(f"sysbus.spi1.isospi.cells{k} DischargeBits",
                                           board="ams").strip(), 0)
        car.sim.run_for(ms=7)
    return seen


def _pit(car):
    """(0x6C2 mask, balance_override) from a fresh pit-diag scan."""
    car.sim.run_for(ms=1100)
    mask = int.from_bytes(car.can.last(BAL_MASK_A).data, "little")
    return mask, bool(car.can.last(PIT_FSM).data[2] & OVERRIDE)


def _only_the_high_cell():
    dcc = [0] * 10
    dcc[HIGH_CHIP] = 1 << HIGH_CELL
    return dcc


def test_auto_balances_in_charge(car):
    """Replaces IFS_HIL BAL B-01: in Charge with no 0x103 the command is Off
    (nothing balances, override reported); BALX (Auto) then discharges the
    high cell alone, on the chip and on 0x6C2, override clear."""
    _charge(car)
    car.sim.run_for(ms=UPDATE_MS)
    assert _dcc(car) == [0] * 10, "balanced with no 0x103 (Off by default)"
    assert _pit(car) == (0, True)
    _command(car, b"BALX")
    car.sim.run_for(ms=UPDATE_MS)
    assert _dcc(car) == _only_the_high_cell()
    assert _pit(car) == (1 << (19 * (HIGH_CHIP // 2) + HIGH_CELL), False)
    assert car.state() == CHARGE


def test_balo_suppresses_and_balx_resumes_in_charge(car):
    """Replaces IFS_HIL BAL B-02 and B-03: BALO clears every switch and sets
    the override bit; BALX brings the high cell back and clears it."""
    _charge(car)
    _command(car, b"BALX")
    car.sim.run_for(ms=UPDATE_MS)
    assert _dcc(car) == _only_the_high_cell()
    car.can.update_periodic("bal", b"BALO")
    car.sim.run_for(ms=UPDATE_MS)
    assert _dcc(car) == [0] * 10, "BALO did not stop balancing"
    assert _pit(car) == (0, True)
    car.can.update_periodic("bal", b"BALX")
    car.sim.run_for(ms=UPDATE_MS)
    assert _dcc(car) == _only_the_high_cell(), "BALX did not resume"
    assert _pit(car)[1] is False


def test_a_silent_override_falls_back_to_off_in_charge(car):
    """Replaces IFS_HIL BAL B-04: the operator link goes quiet, and 5 s on
    the effective command is Off, not Auto (IFS_HIL expects Auto: drift),
    so a BALX that stops being re-sent stops the balancing."""
    _charge(car)
    _command(car, b"BALX")
    car.sim.run_for(ms=UPDATE_MS)
    assert _dcc(car) == _only_the_high_cell()
    car.can.stop_periodic("bal")
    car.sim.run_for(ms=5000 + UPDATE_MS)
    assert _dcc(car) == [0] * 10, "a stale BALX kept balancing"
    assert _pit(car) == (0, True)
    assert car.state() == CHARGE


def test_balo_in_run_changes_nothing_else(car):
    """Replaces IFS_HIL BAL B-06: Auto is Charge-only, so Run balances nothing
    with the imbalance set; a BALO there leaves the FSM in Run, AMS_OK high
    on PB4 (the system's ams_ok port pin), both AIRs closed and no fault."""
    signals = car.sim.system.doc["port"]["signals"]
    pins = {n: car.io.watch(*car.io.gpio(signals[n]["gpio_out"].split(".", 1)[1]))
            for n in ("ams_ok", "air_n", "air_p")}
    car.to_run()
    _command(car, b"BALX")
    car.sim.run_for(ms=UPDATE_MS)
    assert _dcc(car) == [0] * 10, "Auto balanced outside Charge"
    car.can.update_periodic("bal", b"BALO")
    car.sim.run_for(ms=UPDATE_MS)
    assert (car.state(), car.reason()) == (RUN, 0)
    assert all(car.io.level(p) for p in pins.values()), \
        {n: car.io.level(p) for n, p in pins.items()}
    assert _dcc(car) == [0] * 10
    assert _pit(car) == (0, True)
    assert car.state() != ERROR
