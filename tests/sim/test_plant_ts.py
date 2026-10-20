"""The tractive-system plant (M8, #147; vhil/plant_ts.py) against the real
AMS and ECU on ecu-ams: the car arms on the plant's DC link, through the
AMS's own contactor outputs, and a run is the same every time.

Firmware (IFS08-CE-AMS dev): Start -> Precharge on a DASH_CHG press with
TSMS on, closing AIR- and PRE together (state_machine.hpp:308-314);
Precharge -> Transition when the relayed link reaches 95 % of the cell sum
(:117-128), closing AIR+ and opening PRE; Transition -> Run (:372-405). The
link reaches the AMS only through the ECU's 0x100 (vehicle_service.cpp).
Every plant parameter is uncommissioned (catalog/plants/tractive-system.yaml):
sequencing and the firmware's own thresholds are asserted, not the RC
curve's timing.
"""
import pytest

from vhil.cosim import Port
from vhil.plant_ts import TsPlant
from vhil.sim import Sim
from vhil.system import REPO

# Impact selection (ci/impact.yaml, docs/ci.md): boards, systems, subsystems.
pytestmark = [pytest.mark.ecu, pytest.mark.ams, pytest.mark.system_ecu_ams, pytest.mark.plant]

START, PRECHARGE, TRANSITION, RUN, ERROR = 0, 1, 2, 3, 5
GPIOF, TSMS, DASH_CHG = "sysbus.gpioPortF", 9, 10
FS_PRECHARGE_RATIO = 0.90        # FS rules: the second AIR closes at >= 90 % (#147)


def _arm(images, make_plant=lambda: TsPlant(cells="isospi")):
    """Power on, then TSMS on 1 s after the apps start and a DASH_CHG press
    0.1 s later; 4 s with the plant. Returns what the run observed."""
    with Sim(REPO / "systems" / "ecu-ams.yaml", images("ecu-ams")) as sim:
        t0 = sim.wait_for_app()
        port, plant = Port(sim), make_plant()
        ams = sim.io("ams")
        states = []

        def run(ms):
            for _ in range(int(ms // 100)):
                port.run([plant], 100)
                states.append(sim.read_symbol("ams", "g_state_telemetry"))

        run(1000)
        ams.set_input(GPIOF, TSMS, True)
        run(100)
        ams.set_input(GPIOF, DASH_CHG, True)
        port.run([plant], 50)
        ams.set_input(GPIOF, DASH_CHG, False)
        run(3900)
        hb = [(f.t_us, int.from_bytes(f.data[0:2], "little"), f.data[2])
              for f in sim.can("can_acu").frames(0x100, since_us=t0)]
        ts = getattr(plant, "ts", None)
        return {
            "plant": plant,
            "states": states,
            "history": list(ts.history) if ts else None,
            "v_pack": ts.inputs["v_pack"] if ts else plant.inputs.get("v_pack"),
            "steps": plant.steps,
            "heartbeat": hb,
            "vdc": [(f.t_us, f.data.hex()) for f in sim.can("can_inv").sent(0x466, since_us=t0)],
            "acu": [(f.start_ns, f.node, f.id, f.data) for f in sim.can("can_acu").timeline(t0)],
            "fault": sim.read_symbol("ams", "g_fault_reason_telemetry"),
        }


@pytest.fixture(scope="module")
def armed(images):
    return _arm(images)


def test_the_car_arms_on_the_plant_s_dc_link(armed):
    assert ERROR not in armed["states"], f"AMS states {armed['states']} (reason {armed['fault']})"
    assert armed["states"][-1] == RUN, f"AMS states {armed['states']}"
    moves = [(c, closed) for _, c, closed, _ in armed["history"]]
    assert moves[:2] in ([("pre", True), ("air_neg", True)], [("air_neg", True), ("pre", True)])
    assert ("air_pos", True) in moves and ("pre", False) in moves
    assert moves.index(("air_pos", True)) > moves.index(("air_neg", True))


def test_air_plus_closes_above_the_fs_ratio_and_the_firmware_s_95_percent(armed):
    """At AIR+'s contact closing the link is at least 90 % of the pack (the
    FS rule); the AMS waited for 95 % of its own cell sum."""
    t, _, _, v = next(m for m in armed["history"] if m[1:3] == ("air_pos", True))
    assert armed["v_pack"] == pytest.approx(351.5)          # 95 x the model's 3700 mV
    assert v >= FS_PRECHARGE_RATIO * armed["v_pack"], f"AIR+ closed at {v:.1f} V"
    assert v >= 0.95 * armed["v_pack"] - 1, f"AIR+ closed at {v:.1f} V, below the firmware's 95 %"


def test_the_ecu_relays_a_ramp_not_a_step(armed):
    """The ECU's 0x100 carries the inverter's reading of the plant's link
    (dc_bus_valid set): it climbs through many values during precharge."""
    rising = sorted({v for _, v, flags in armed["heartbeat"] if flags & 0x2 and 0 < v < 330})
    assert len(rising) > 20, f"relayed link values during precharge: {rising}"
    assert max(v for _, v, _ in armed["heartbeat"]) in (351, 352)


def test_the_plant_is_stepped_on_events_not_every_step(armed):
    """51 Port.run calls (the test's 100 ms slices): one step each at its
    start, plus the coil edges and forecast horizons; not one per 10 ms."""
    assert armed["steps"] <= 51 + 15, armed["steps"]
    assert len(armed["vdc"]) >= 390, "the inverter's 0x466 is not every 10 ms"


def test_two_runs_are_the_same(images, armed):
    again = _arm(images)
    for key in ("history", "heartbeat", "vdc", "acu", "states"):
        assert again[key] == armed[key], f"{key} differs between two runs"
