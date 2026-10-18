"""The per-cell pack (vhil/plant_pack.py, M9 prep #318) without Renode: its
closed form, the cell and NTC mapping onto the LTC6811 chain, its faults,
and the tractive-system plant driving the chain and the current sensor only
when a value moves by the measurement's resolution."""
import math

import pytest

from vhil.plant_pack import (LTC_LSB_V, NTC_CHANNELS, Pack, cell_input, ntc_cell,
                             sensor_volts)
from vhil.plant_ts import TractiveSystem, TsPlant, load_parameters, settable


def _pack(**overrides):
    return Pack(load_parameters(overrides=overrides or None))


def test_every_element_maps_to_a_used_chip_input_once():
    seen = {cell_input(k) for k in range(95)}
    assert len(seen) == 95
    for m in range(5):
        assert {i for c, i in seen if c == 2 * m} == set(range(9))       # C1..C9
        assert {i for c, i in seen if c == 2 * m + 1} == set(range(10))  # C1..C10
    assert cell_input(18) == (1, 9) and cell_input(19) == (2, 0)


def test_ntcs_spread_over_their_chip_s_cells():
    assert len(NTC_CHANNELS) == 20 and NTC_CHANNELS[10] == 16
    assert [ntc_cell(0, k) for k in (0, 19)] == [0, 8]
    assert [ntc_cell(1, k) for k in (0, 19)] == [9, 18]
    assert ntc_cell(9, 19) == 94


def test_the_ocv_is_the_cited_table_and_the_pack_starts_at_rest():
    p = _pack()
    assert p.ocv(0.5) == pytest.approx(3.6551) and p.ocv(0.25) == pytest.approx((3.2815 + 3.4676) / 2)
    assert p.v_pack == pytest.approx(95 * 3.6551)
    assert p.rate_v() == 0.0 and p.rate_t() == 0.0, "a pack at rest moves nothing"


def test_a_constant_discharge_follows_the_closed_form():
    p = _pack()
    p.i_pack = 60.0
    q = 3.0 * 6 * 3600
    r0, r1, tau = 0.02006 / 6, 0.008 / 6, 30.0
    for t in (1.0, 10.0, 30.0):
        p.advance(int(t * 1e6))
        soc = 0.5 - 60 * t / q
        v_rc = 60 * r1 * (1 - math.exp(-t / tau))
        assert p.soc[0] == pytest.approx(soc, rel=1e-12)
        assert p.v_rc[0] == pytest.approx(v_rc, rel=1e-9)
        # R0 falls as the element heats (alpha < 0): ~1.4 K, -3 % by 30 s.
        assert p.v_cell(0) == pytest.approx(p.ocv(soc) - 60 * r0 - v_rc, abs=60 * r0 * 0.05)
    assert p.temp[0] > 298.15


def test_how_often_it_is_advanced_does_not_change_the_result():
    a, b = _pack(), _pack()
    a.i_pack = b.i_pack = 120.0
    a.advance(20_000_000)
    for t in range(1, 2001):
        b.advance(t * 10_000)
    assert b.soc == pytest.approx(a.soc, rel=1e-12)
    assert b.v_rc == pytest.approx(a.v_rc, rel=1e-9)
    assert b.temp == pytest.approx(a.temp, rel=1e-6)


def test_spreads_are_seeded_and_faults_act_by_index():
    a = _pack(soc_spread=0.02, capacity_spread=0.05)
    b = _pack(soc_spread=0.02, capacity_spread=0.05)
    assert a.soc == b.soc and len(set(a.soc)) > 1
    faults = {}
    a.fault("weak_cell", 40, faults)
    a.fault("weak_cell_capacity", 0.5, faults)
    assert a.capacity[40] == pytest.approx(9.0)
    a.fault("hot_cell", 7, faults)
    a.fault("hot_cell_dt", 40.0, faults)
    assert a.t_cell(7) == pytest.approx(a.temp[7] + 40)
    p = _pack()
    p.fault("imbalance_cell", 3, faults)
    p.fault("imbalance_soc", 0.1, faults)
    assert p.soc[3] == pytest.approx(0.6) and p.soc[4] == pytest.approx(0.5)


def test_the_current_sensor_voltages_follow_its_catalogue_model():
    import yaml
    from vhil.system import REPO
    doc = yaml.safe_load((REPO / "catalog/models/ssa-2-250a.yaml").read_text())
    v = sensor_volts(doc, 100.0)
    assert v["out_p"] == pytest.approx(1.69) and v["out_n"] == pytest.approx(1.19)
    assert sensor_volts(doc, 1000.0)["out_p"] == 2.75          # clipped


class FakeChain:
    def __init__(self):
        self.pushed, self.bleed, self.armed, self.bits = [], [0] * 95, None, [0] * 95

    def take_bleed(self):
        on, self.bleed = self.bleed, [0] * 95
        return 0, on, self.bits

    def arm(self, on):
        self.armed = on

    def fired(self):
        return False

    def push(self, pack):
        self.pushed.append([round(pack.v_cell(k) / LTC_LSB_V) for k in range(pack.n)])


class FakePort:
    signals = {"air_pos_cmd": 0, "air_neg_cmd": 0, "pre_cmd": 0, "sdc_closed": 0}

    def __init__(self):
        self.levels = {"air_pos_cmd": False, "air_neg_cmd": False, "pre_cmd": False}
        self.pending = {k: [] for k in self.levels}
        self.sdc = True
        self.volts = {}

    def level(self, n):
        return self.levels[n]

    def edges(self, n):
        out, self.pending[n] = self.pending[n], []
        return out

    def edge(self, n, t, v):
        self.levels[n] = v
        self.pending[n].append((t, v))

    def driven(self, n):
        return self.sdc

    def set_voltage(self, n, v):
        self.volts[n] = v

    def ticks(self, n):
        return None

    def schedule(self, *a, **k):
        return (0, 10_000)


def _plant():
    import yaml
    from vhil.system import REPO
    params = load_parameters()
    doc = yaml.safe_load((REPO / "catalog/models/ssa-2-250a.yaml").read_text())
    chain = FakeChain()
    plant = TsPlant(TractiveSystem(params=params), pack=Pack(params), chain=chain,
                    current=(doc, {"out_p": "i_pack_p", "out_n": "i_pack_n"}))
    return plant, chain


def test_the_plant_pushes_once_at_rest_and_the_current_flows_only_with_both_airs_closed():
    plant, chain = _plant()
    port = FakePort()
    plant.step(port, 0)
    assert len(chain.pushed) == 1 and plant.pack_next is None, "a pack at rest is settled"
    assert plant.ts.inputs["v_pack"] == pytest.approx(95 * 3.6551)
    assert port.volts == {"i_pack_p": pytest.approx(1.44), "i_pack_n": pytest.approx(1.44)}
    plant.apply({"i_inverter_dc": 100.0}, 1_000_000)
    plant.step(port, 1_000_000)
    assert plant.pack.i_pack == 0.0, "no current with the AIRs open"
    port.edge("air_neg_cmd", 1_000_000, True)
    port.edge("air_pos_cmd", 1_000_000, True)
    plant.step(port, 1_000_500)
    assert plant.next_us == 1_020_000, "the plant comes back when the AIRs' contacts close"
    plant.step(port, 1_020_000)
    assert plant.pack.i_pack == 100.0
    assert port.volts["i_pack_p"] == pytest.approx(1.69)
    assert plant.pack_next - 1_020_000 == 100_000, "no sooner than cell_exchange_min"
    v_at_close = chain.pushed[-1][0]
    plant.step(port, plant.pack_next)
    assert chain.pushed[-1][0] < v_at_close, "the cells sag and fall under load"
    assert plant.ts.inputs["v_pack"] < 95 * 3.6551 - 95 * 100 * 0.02006 / 6 + 1


def test_bleed_removes_charge_and_the_trigger_rearms_when_it_stops():
    plant, chain = _plant()
    port = FakePort()
    plant.step(port, 0)
    assert chain.armed is True
    chain.bleed[5] = 2_000_000                  # 2 s of DCC on element 5
    chain.bits[5] = 1
    soc = plant.pack.soc[5]
    plant.apply({}, 3_000_000)                  # an exchange, as the worker steps after a row
    plant.step(port, 3_000_000)
    q = 3.0 * 6 * 3600
    assert plant.pack.soc[5] == pytest.approx(soc - 3.6551 / 23.5 * 2.0 / q, rel=1e-3)
    assert chain.armed is False and plant.pack_next is not None
    chain.bits[5] = 0
    plant.step(port, plant.pack_next)
    assert chain.armed is True
    recs = [(r["name"], r["value"]) for r in plant.records("ts", 10_000_000)]
    assert ("bleed_c5", 1) in recs and ("bleed_c5", 0) in recs


def test_settable_covers_the_pack_s_inputs_and_faults():
    s = settable("tractive-system")
    assert {"i_inverter_dc", "i_charger", "t_ambient", "weak_cell", "hot_cell", "imbalance_soc",
            "soc_init", "bleed_r"} <= s
