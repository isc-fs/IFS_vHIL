"""The shutdown circuit, TS-2 (vhil/plant_sdc.py), without Renode: every
series element opens AIR+_VCC, the IMD and AMS latch relays hold open until
RST_EXT with their OK line high, and the plant drives PF9 only on a change."""
import pytest

from vhil.plant_sdc import SWITCHES, SdcPlant, ShutdownCircuit
from vhil.plant_ts import load_parameters, make_plants, settable


def _armed(**params):
    sdc = ShutdownCircuit(load_parameters("shutdown-circuit", params or None))
    sdc.set("ams_ok", True, 0)
    sdc.set("tsms", True, 0)
    sdc.set("reset", True, 10)
    sdc.set("reset", False, 20)
    return sdc


def test_the_circuit_is_open_after_power_on_until_a_reset_press():
    sdc = ShutdownCircuit()
    sdc.set("tsms", True, 0)
    sdc.set("ams_ok", True, 0)
    assert not sdc.closed and sdc.first_open() == "imd_latch"
    sdc.set("reset", True, 10)
    assert sdc.closed
    sdc.set("reset", False, 20)
    assert sdc.closed, "the latches hold themselves after the button"


@pytest.mark.parametrize("switch", SWITCHES)
def test_every_series_element_opens_the_circuit(switch):
    sdc = _armed()
    assert sdc.closed
    sdc.set(switch, False, 100)
    assert not sdc.closed
    sdc.set(switch, True, 200)
    if switch == "imd_ok":
        # K4 dropped with IMD_OK and stays open until RST_EXT.
        assert not sdc.closed and sdc.first_open() == "imd_latch"
        sdc.set("reset", True, 300)
    assert sdc.closed


def test_ams_ok_falling_drops_the_ams_latch_and_a_press_needs_ams_ok_high():
    sdc = _armed()
    sdc.set("ams_ok", False, 100)
    assert not sdc.closed and sdc.first_open() == "ams_latch"
    sdc.set("reset", True, 200)                 # AMS_OK still low: Q10 off
    assert not sdc.closed
    sdc.set("ams_ok", True, 300)                # still pressed: the coil pulls in
    assert sdc.closed
    assert [(t, k, v) for t, k, v in sdc.history if k == "sdc_closed"][-2:] == [
        (100, "sdc_closed", False), (300, "sdc_closed", True)]


def test_one_reset_resets_both_latches():
    sdc = _armed()
    sdc.set("imd_ok", False, 100)
    sdc.set("ams_ok", False, 100)
    sdc.set("imd_ok", True, 200)
    sdc.set("ams_ok", True, 200)
    assert not sdc.latch["imd"] and not sdc.latch["ams"]
    sdc.set("reset", True, 300)
    assert sdc.latch == {"imd": True, "ams": True} and sdc.closed


def test_the_udv_k1_is_in_series_only_when_the_parameter_says_so():
    assert _armed().closed                      # default: not in series (untraced)
    sdc = _armed(udv_k1_in_series=True)
    assert not sdc.closed and sdc.first_open() == "udv_k1"
    sdc.set("udv_sdc", True, 100)
    assert sdc.closed


class FakePort:
    def __init__(self, signals):
        self.signals = dict.fromkeys(signals)
        self.levels = {s: False for s in signals}
        self.pending = {s: [] for s in signals}
        self.set_calls = []

    def level(self, name):
        return self.levels[name]

    def edges(self, name):
        out, self.pending[name] = self.pending[name], []
        return out

    def edge(self, name, t, level):
        self.levels[name] = level
        self.pending[name].append((t, level))

    def set_level(self, name, level):
        self.set_calls.append((name, level))


def test_the_plant_drives_pf9_on_changes_only_and_records_them():
    port = FakePort(["ams_ok", "sdc_closed"])
    plant = SdcPlant()
    plant.step(port, 0)
    assert port.set_calls == [("sdc_closed", False)]
    plant.step(port, 1000)
    assert len(port.set_calls) == 1, "an unchanged circuit drives nothing"
    port.edge("ams_ok", 2_000_000, True)
    plant.apply({"tsms": True, "reset": True}, 2_500_000)
    plant.step(port, 2_500_000)
    assert port.set_calls[-1] == ("sdc_closed", True)
    port.edge("ams_ok", 3_000_000, False)
    plant.step(port, 3_000_500)                # the sync point after the edge
    assert port.set_calls[-1] == ("sdc_closed", False)
    recs = [(r["t_us"], r["name"], r["value"]) for r in plant.records("sdc", 4_000_000)]
    assert (0, "sdc_closed", 0) in recs and (2_500_000, "sdc_closed", 1) in recs
    assert (3_000_000, "sdc_closed", 0) in recs, "a change is stamped at the edge's time"
    assert plant.records("sdc", 4_000_000) == []
    assert plant.state(4_000_000)["sdc_ams_latch"] == 0


def test_a_parameter_set_by_a_row_is_an_override():
    plant = SdcPlant()
    plant.apply({"udv_k1_in_series": True}, 0)
    assert plant.params["udv_k1_in_series"].status == "override"
    with pytest.raises(KeyError):
        plant.apply({"nope": 1}, 0)


def test_settable_and_make_plants_know_both_kinds():
    assert {"tsms", "reset", "imd_ok", "udv_k1_in_series"} <= settable("shutdown-circuit")
    assert "pre_open_circuit" in settable("tractive-system")
    from vhil.system import REPO, System
    plants = make_plants(System(REPO / "systems" / "ecu-ams.yaml"))
    assert list(plants) == ["sdc", "ts"], "the circuit steps before the tractive system"
    assert System(REPO / "systems" / "ecu-ams.yaml").plant_driven_pins() == {"ams.PF9": "sdc"}
