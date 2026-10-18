"""The tractive-system plant (vhil/plant_ts.py) without Renode: its physics
against the closed form, its faults, its forecast of the inverter's 0x466,
and that how often it is stepped never changes what it computes."""
import math

import pytest

from vhil.plant_ts import (TractiveSystem, TsPlant, load_parameters, uncommissioned,
                           vdc_frame)

V_PACK = 351.5          # 95 cells at the LTC6811 model's 3700 mV


def _ts(**overrides):
    ts = TractiveSystem(params=load_parameters(overrides=overrides))
    ts.set("v_pack", V_PACK, 0)
    ts.set("sdc_closed", True, 0)
    return ts


def _p(name):
    return load_parameters()[name].value


def test_every_parameter_is_cited_or_says_it_is_not():
    params = load_parameters()
    assert {p.status for p in params.values()} <= {"cited", "uncommissioned"}
    assert all(p.source for p in params.values())
    # Nothing is validated against a car log yet: every one is reported.
    assert set(uncommissioned(params)) == set(params)
    assert params["c_link"].status == "cited"
    assert params["r_precharge"].status == "uncommissioned"


def test_an_override_is_marked_as_the_run_s_choice():
    p = load_parameters(overrides={"sense_floor": 48})
    assert p["sense_floor"].value == 48 and p["sense_floor"].status == "override"
    with pytest.raises(KeyError):
        load_parameters(overrides={"nope": 1})


def test_precharge_follows_the_rc_closed_form_after_the_contacts_close():
    ts = _ts()
    ts.set("air_neg_cmd", True, 1000)
    ts.set("pre_cmd", True, 1000)
    closed_at = 1000 + round(max(_p("air_close"), _p("pre_close")) * 1e6)
    ts.advance(closed_at - 1)
    assert ts.v_link == 0.0, "the link moved before both contacts closed"
    tau = _p("r_precharge") * _p("c_link")
    for dt in (0.05, 0.276, 0.5, 1.0):
        ts.advance(closed_at + round(dt * 1e6))
        assert ts.v_link == pytest.approx(V_PACK * (1 - math.exp(-dt / tau)), rel=1e-12)
    assert [(c, closed) for _, c, closed, _ in ts.history] == [("pre", True), ("air_neg", True)]


def test_air_plus_ties_the_link_to_the_pack_and_opening_holds_it():
    ts = _ts()
    for name in ("air_neg_cmd", "pre_cmd"):
        ts.set(name, True, 0)
    ts.advance(1_000_000)
    assert ts.v_link < 0.98 * V_PACK
    ts.set("air_pos_cmd", True, 1_000_000)
    ts.set("pre_cmd", False, 1_000_000)
    ts.advance(1_000_000 + round(_p("air_close") * 1e6))
    assert ts.v_link == V_PACK and ts.outputs["air_pos_closed"]
    # Every coil off with the SDC closed: no bleed, no leak, nothing drains it.
    for name in ("air_pos_cmd", "air_neg_cmd"):
        ts.set(name, False, 2_000_000)
    ts.advance(5_000_000)
    assert ts.v_link == V_PACK and not ts.outputs["air_neg_closed"]


def test_the_bleed_drains_the_link_when_the_sdc_opens_or_the_ecu_secures_it():
    tau = _p("r_discharge") * _p("c_link")
    for how in ("sdc", "ecu"):
        ts = _ts()
        ts.set("air_neg_cmd", True, 0)
        ts.set("air_pos_cmd", True, 0)
        ts.advance(100_000)
        ts.set("air_neg_cmd", False, 100_000)
        ts.set("air_pos_cmd", False, 100_000)
        opened = 100_000 + round(_p("air_open") * 1e6)
        ts.advance(opened)
        if how == "sdc":
            ts.set("sdc_closed", False, opened)
        else:
            ts.set("discharge_cmd", True, opened)
        ts.advance(opened + 2_000_000)
        assert ts.v_link == pytest.approx(V_PACK * math.exp(-2.0 / tau), rel=1e-12)


def test_the_sdc_opening_drops_every_contact():
    ts = _ts()
    for name in ("air_neg_cmd", "air_pos_cmd"):
        ts.set(name, True, 0)
    ts.advance(100_000)
    ts.set("sdc_closed", False, 100_000)       # coil supply gone, pins still high
    ts.advance(100_000 + round(_p("air_open") * 1e6))
    assert not ts.outputs["air_pos_closed"] and not ts.outputs["air_neg_closed"]


def test_faults():
    ts = _ts()
    ts.fault("pre_open_circuit", True)
    ts.set("air_neg_cmd", True, 0)
    ts.set("pre_cmd", True, 0)
    ts.advance(5_000_000)
    assert ts.v_link == 0.0, "a precharge path that never conducts charged the link"

    ts = _ts()
    ts.fault("welded_air_pos", True)
    ts.set("air_neg_cmd", True, 0)              # AIR- alone closes onto a welded AIR+
    ts.advance(round(_p("air_close") * 1e6))
    assert ts.v_link == V_PACK

    ts = _ts()
    ts.fault("welded_air_neg", True)
    assert ts.outputs["air_neg_closed"]
    ts.set("pre_cmd", True, 0)
    ts.advance(1_000_000)
    assert ts.v_link > 0.9 * V_PACK, "a welded AIR- precharges with PRE alone"

    ts = _ts()
    for name in ("air_neg_cmd", "air_pos_cmd"):
        ts.set(name, True, 0)
    ts.advance(100_000)
    ts.fault("air_external_open", True, 100_000)
    ts.fault("r_leak", 10_000.0, 100_000)
    ts.advance(1_100_000)
    tau = 10_000.0 * _p("c_link")
    assert ts.v_link == pytest.approx(V_PACK * math.exp(-1.0 / tau), rel=1e-12)

    ts = _ts()
    ts.fault("c_scale", 2.0)
    ts.set("air_neg_cmd", True, 0)
    ts.set("pre_cmd", True, 0)
    closed = round(_p("air_close") * 1e6)
    ts.advance(closed + 552_000)                # one tau of the doubled C
    assert ts.v_link == pytest.approx(V_PACK * (1 - math.exp(-1.0)), rel=1e-3)


def test_stepping_often_or_rarely_computes_the_same_bits():
    def run(step_us):
        ts = _ts()
        events = [(10_000, "air_neg_cmd", True), (10_000, "pre_cmd", True),
                  (900_000, "air_pos_cmd", True), (900_000, "pre_cmd", False),
                  (3_000_000, "air_pos_cmd", False), (3_000_000, "air_neg_cmd", False),
                  (3_200_000, "discharge_cmd", True)]
        out, t = [], 0
        while t <= 6_000_000:
            while events and events[0][0] <= t:
                at, name, level = events.pop(0)
                ts.set(name, level, at)
            ts.advance(t)
            if t % 10_000 == 0:
                out.append(ts.v_link)
            t += step_us
        return out
    ref = run(10_000)
    assert run(500) == ref and run(5_000) == ref


def test_the_report_rounds_to_volts_and_honours_the_sense_floor():
    ts = _ts()
    assert ts.report(351.49) == 351 and ts.report(351.5) == 352
    assert vdc_frame(352) == bytes([0, 0, 0x60, 0x01, 0, 0])
    assert vdc_frame(None) is None
    held = _ts(sense_floor=48)
    assert held.report(47.9) == 48 and held.report(48.2) == 48 and held.report(60) == 60
    quiet = _ts(sense_floor=48, below_floor="silent")
    assert quiet.report(47.9) is None and quiet.report(48.0) == 48


def test_forecast_lists_changes_until_settled_and_leaves_the_plant_alone():
    ts = _ts()
    ts.set("air_neg_cmd", True, 0)
    ts.set("pre_cmd", True, 0)
    before = (ts.t_us, ts.v_link, list(ts.pending))
    entries, settled = ts.forecast(10_000, 10_000, 3_000_000)
    assert (ts.t_us, ts.v_link, ts.pending) == before
    assert entries[0] == (10_000, 0)
    assert all(b[0] > a[0] and b[1] > a[1] for a, b in zip(entries, entries[1:]))
    assert settled and entries[-1][1] == 351      # approaching 351.5 V from below
    # The forecast's values are the plant's own at those times.
    for t, r in entries[::7]:
        ts.advance(t)
        assert ts.report() == r


class FakePort:
    """Enough of vhil.cosim.Port for TsPlant: coil levels and edges, the
    driven PF9, the scheduled 0x466."""

    def __init__(self):
        self.signals = {n: None for n in ("air_pos_cmd", "air_neg_cmd", "pre_cmd",
                                          "sdc_closed", "v_link")}
        self.levels = {n: False for n in ("air_pos_cmd", "air_neg_cmd", "pre_cmd")}
        self.pending_edges = {n: [] for n in self.levels}
        self.supply = True
        self.grid = None
        self.schedules = []

    def level(self, name):
        return self.levels[name]

    def edges(self, name):
        out, self.pending_edges[name] = self.pending_edges[name], []
        return out

    def driven(self, name):
        return self.supply

    def ticks(self, name):
        return self.grid

    def schedule(self, name, entries, period_ms):
        if self.grid is None:
            self.grid = (entries[0][0], int(period_ms * 1000))
        self.schedules.append(list(entries))
        return self.grid


def test_on_the_port_it_applies_edges_at_their_times_and_schedules_the_reports():
    port = FakePort()
    plant = TsPlant(_ts())
    plant.ts.set("v_pack", V_PACK, 0)
    plant.step(port, 0)
    assert port.schedules[-1] == [(10_000, vdc_frame(0))] and plant.next_us is None
    # PRE and AIR- closed at 5.5 ms; the plant only hears of it at 6 ms.
    for name in ("air_neg_cmd", "pre_cmd"):
        port.pending_edges[name].append((5_500, True))
    plant.step(port, 6_000)
    first = port.schedules[-1]
    assert first[0][0] == 10_000
    ref = _ts()
    ref.set("air_neg_cmd", True, 5_500)
    ref.set("pre_cmd", True, 5_500)
    for t, data in first:
        ref.advance(t)
        assert data == vdc_frame(ref.report())


def test_a_system_declares_its_plants_and_they_are_checked(tmp_path):
    import yaml
    from vhil.plant_ts import make_plants
    from vhil.system import REPO, System, SystemError
    system = System(REPO / "systems" / "ecu-ams.yaml")
    plants = make_plants(system)
    assert set(plants) == {"sdc", "ts"} and plants["ts"].cells == "isospi"
    doc = yaml.safe_load((REPO / "systems" / "ecu-ams.yaml").read_text())
    for bad, why in (({"plant": "warp-core"}, "no catalog/plants/warp-core.yaml"),
                     ({"plant": "tractive-system", "params": {"nope": 1}}, "no parameter 'nope'"),
                     ({"plant": "tractive-system", "cells": "sd"}, "not an LTC6820")):
        doc["plants"] = {"ts": bad}
        path = tmp_path / "ecu-ams.yaml"
        path.write_text(yaml.safe_dump(doc))
        with pytest.raises(SystemError, match=why):
            System(path)
