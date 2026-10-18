"""The FMI 3.0 co-simulation master (vhil/fmi.py) without Renode, on the
reference tractive-system FMU (tests/fmu/ts_reference.c) the tests compile:
it loads and steps an FMU by the contract's names, it computes what the
Python stand-in computes on the same inputs (so either drops in for the
other), and on the port its forecasts and roll-backs give exactly what a
lock-step master gives, with or without FMU state."""
import shutil

import pytest

from vhil.fmi import Fmu, FmiError, FmuPlant, build_reference_fmu
from vhil.plant_ts import TractiveSystem, load_parameters, vdc_frame

pytestmark = pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler for the reference FMU")

V_PACK = 351.5
H_US = 5_000
# (time us, input, value): an arm, Run, then the SDC opening (the bleed).
EVENTS = [(0, "v_pack", V_PACK), (0, "sdc_closed", True),
          (100_000, "air_neg_cmd", True), (100_000, "pre_cmd", True),
          (1_000_000, "air_pos_cmd", True), (1_000_000, "pre_cmd", False),
          (2_000_000, "sdc_closed", False)]


@pytest.fixture(scope="module")
def fmu_path(tmp_path_factory):
    return build_reference_fmu(tmp_path_factory.mktemp("fmu") / "ts.fmu")


@pytest.fixture(scope="module")
def fmu_path_stateless(tmp_path_factory):
    return build_reference_fmu(tmp_path_factory.mktemp("fmu") / "ts-nostate.fmu",
                               can_get_set_state=False)


def test_it_reads_the_description_and_loads_the_binary(fmu_path):
    f = Fmu(fmu_path)
    assert f.identifier == "ts_reference" and f.can_get_set_state and f.default_step == 0.005
    assert f.variables["v_link"].causality == "output"
    assert f.variables["air_pos_cmd"].type == "Boolean"
    assert {"c_link", "r_precharge", "air_close"} <= {n for n, v in f.variables.items()
                                                    if v.causality == "parameter"}


def test_a_wrong_token_or_version_is_refused(fmu_path, tmp_path):
    f = Fmu(fmu_path)
    f.token = "{nope}"
    with pytest.raises(FmiError):
        f.instantiate()
    d = tmp_path / "v2"
    shutil.copytree(f.root, d)
    md = d / "modelDescription.xml"
    md.write_text(md.read_text().replace('fmiVersion="3.0"', 'fmiVersion="2.0"'))
    with pytest.raises(FmiError):
        Fmu(d)


def _lockstep_fmu(path):
    """The FMU stepped every 5 ms with each input at its time: v_link per grid point."""
    f = Fmu(path)
    f.instantiate()
    f.initialize(0.0)
    out, events = [], list(EVENTS)
    for k in range(0, 3_000_000 // H_US):
        t = k * H_US
        while events and events[0][0] <= t:
            _, name, value = events.pop(0)
            f.set(name, value)
        f.do_step(t / 1e6, H_US / 1e6)
        out.append(f.get("v_link"))
    f.free()
    return out


def test_the_fmu_and_the_stand_in_compute_the_same_link(fmu_path):
    """Interchangeable: the same inputs at the same times give the same link,
    to rounding (the stand-in computes from each segment's start, the FMU
    from each step's)."""
    fmu = _lockstep_fmu(fmu_path)
    ts, events = TractiveSystem(), list(EVENTS)
    stand_in = []
    for k in range(0, 3_000_000 // H_US):
        t = k * H_US
        while events and events[0][0] <= t:
            _, name, value = events.pop(0)
            ts.set(name, value, t)
        ts.advance(t + H_US)
        stand_in.append(ts.v_link)
    assert max(stand_in) == pytest.approx(V_PACK)
    for k, (a, b) in enumerate(zip(fmu, stand_in)):
        assert a == pytest.approx(b, rel=1e-9, abs=1e-9), f"at {(k + 1) * 5} ms: FMU {a}, stand-in {b}"


def test_state_save_and_restore_replays_bit_for_bit(fmu_path):
    f = Fmu(fmu_path)
    f.instantiate()
    f.initialize(0.0)
    for _, name, value in EVENTS[:4]:
        f.set(name, value)
    for k in range(40):
        f.do_step(k * 0.005, 0.005)
    state = f.get_state()
    after = []
    for k in range(40, 80):
        f.do_step(k * 0.005, 0.005)
        after.append(f.get("v_link"))
    f.set_state(state)
    again = []
    for k in range(40, 80):
        f.do_step(k * 0.005, 0.005)
        again.append(f.get("v_link"))
    assert again == after


class FakePort:
    """Coil edges, the driven PF9, and the scheduled 0x466, in fake time."""

    def __init__(self):
        self.signals = {n: None for n in ("air_pos_cmd", "air_neg_cmd", "pre_cmd",
                                          "sdc_closed", "v_link")}
        self.levels = {n: False for n in ("air_pos_cmd", "air_neg_cmd", "pre_cmd")}
        self.pending = {n: [] for n in self.levels}
        self.supply = True
        self.grid = None
        self.frames = {}                      # tick us -> payload, as the probe would send

    def level(self, name):
        return self.levels[name]

    def edges(self, name):
        out, self.pending[name] = self.pending[name], []
        return out

    def driven(self, name):
        return self.supply

    def ticks(self, name):
        return self.grid

    def schedule(self, name, entries, period_ms):
        if self.grid is None:
            self.grid = (entries[0][0], int(period_ms * 1000))
        start, period = self.grid
        first = entries[0][0]
        for t in [t for t in self.frames if t >= first]:
            del self.frames[t]
        data = None
        t, k = first, 0
        horizon = entries[-1][0] + 5_000_000
        while t <= horizon:
            while k < len(entries) and entries[k][0] <= t:
                data = entries[k][1]
                k += 1
            self.frames[t] = data
            t += period
        return self.grid


def _drive(plant, stops):
    """Step the plant at `stops` (us); coil edges at their times reach it at
    the first stop after them, as the trigger delivers them."""
    port = FakePort()
    edges = [(100_000, "air_neg_cmd", True), (100_000, "pre_cmd", True),
             (1_004_000, "air_pos_cmd", True), (1_004_000, "pre_cmd", False)]
    stops, t = sorted(stops), 0
    while t <= 2_500_000:
        while edges and edges[0][0] <= t:
            at, name, level = edges.pop(0)
            port.pending[name].append((at, level))
        plant.step(port, t)
        later = [s for s in stops if s > t] + ([plant.next_us] if plant.next_us and plant.next_us > t else [])
        if not later:
            break
        t = min(later)                        # as Port.run: the next trigger or next_us
    return {t: d for t, d in port.frames.items() if t <= 2_500_000}


def _params():
    return load_parameters()


def test_on_the_port_a_late_edge_rolls_back_to_what_lock_step_gives(fmu_path, fmu_path_stateless):
    """Event-driven (a few stops, the FMU run ahead past each edge and
    rolled back) gives the same 0x466 frames as stopping every 5 ms."""
    def plant(path):
        return FmuPlant(Fmu(path), params=_params(), v_pack=V_PACK)

    lock = plant(fmu_path)
    ref = _drive(lock, range(0, 2_500_001, 5_000))
    events = plant(fmu_path)
    got = _drive(events, [0, 100_500, 600_000, 1_004_500, 2_000_000])
    assert events.rollbacks >= 2, "the FMU never ran past an edge: the test tests nothing"
    assert got == ref
    replay = plant(fmu_path_stateless)
    assert _drive(replay, [0, 100_500, 600_000, 1_004_500, 2_000_000]) == ref
    assert replay.rollbacks >= 2
    # It is the arm the stand-in's contract describes: 0 V, a ramp, the pack.
    values = [d for _, d in sorted(ref.items()) if d is not None]
    assert values[0] == vdc_frame(0) and values[-1] in (vdc_frame(351), vdc_frame(352))


class CellChain:
    """The LTC6811 chain as vhil.plant_pack.ChainDriver drives it, minus Renode."""

    def __init__(self):
        from vhil.plant_pack import ChainDriver
        self.real = ChainDriver.__new__(ChainDriver)
        self.real.chips, self.real.cpc = 10, (9, 10)
        self.real.last_cells = self.real.last_temps = None
        self.real.pushes, self.real.armed = 0, False
        self.calls = []
        self.real.call = lambda method, arg=None: self.calls.append((method, arg)) or ""
        self.bits = [0] * 95

    @property
    def pushes(self):
        return self.real.pushes

    def push(self, cells):
        self.real.push(cells)

    def take_bleed(self):
        return 0, [0] * 95, list(self.bits)


def test_the_fmu_s_cells_ntcs_and_current_reach_the_measurement_chain(fmu_path):
    """M9 prep (#318): v_cell[k] / t_sensor[n] onto the chain's inputs and
    mux channels, i_pack onto the sensor's legs, the DCC bits back as
    cell_bleed[k]: the same drivers the stand-in's pack uses."""
    import yaml
    from vhil.plant_pack import NTC_CHANNELS, UNUSED_RAW
    from vhil.system import REPO
    doc = yaml.safe_load((REPO / "catalog/models/ssa-2-250a.yaml").read_text())
    chain = CellChain()
    plant = FmuPlant(Fmu(fmu_path), params=_params(), v_pack=V_PACK, chain=chain,
                     current=(doc, {"out_p": "i_pack_p", "out_n": "i_pack_n"}))
    assert sorted(plant.cell_vars) == [0, 1, 2] and sorted(plant.bleed_vars) == [0, 1, 2]
    port = FakePort()
    port.volts = {}
    port.set_voltage = lambda name, v: port.volts.__setitem__(name, v)
    plant.step(port, 0)
    method, arg = chain.calls[0]
    raw = [int(x) for x in arg.split(",")]
    assert method == "SetPlantCells" and raw[:3] == [36551] * 3 and raw[3] == UNUSED_RAW
    method, arg = chain.calls[1]
    temps = arg.split(",")
    assert method == "SetPlantTemperatures" and temps[NTC_CHANNELS[0]] == temps[NTC_CHANNELS[1]] == "250"
    assert temps[NTC_CHANNELS[2]] == "", "an NTC the FMU doesn't give is left alone"
    assert port.volts == {"i_pack_p": pytest.approx(1.44), "i_pack_n": pytest.approx(1.44)}
    # A balance switch on element 1 becomes the FMU's cell_bleed[2].
    chain.bits[1] = 1
    plant.step(port, 50_000)
    assert plant.fmu.get("cell_bleed[2]") is True or plant.fmu.get("cell_bleed[2]") == 1
    # The inverter's 100 A with the AIRs closed: the sensor and the sag.
    plant.apply({"i_inverter_dc": 100.0}, 50_000)
    for name in ("air_neg_cmd", "air_pos_cmd"):
        port.pending[name].append((100_000, True))
        port.levels[name] = True
    plant.step(port, 100_500)
    plant.step(port, 200_000)
    assert port.volts["i_pack_p"] == pytest.approx(1.69)
    last = [int(x) for x in [c for c in chain.calls if c[0] == "SetPlantCells"][-1][1].split(",")]
    assert last[0] < 36551 - 300, "100 A x 3.34 mOhm sags the cell ~0.33 V"
