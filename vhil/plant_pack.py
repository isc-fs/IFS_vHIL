"""The accumulator, per cell (M9 prep, #318; docs/m8-plan.md "M9 prep"): the
stand-in's model of the 95 series elements the AMS measures, and how the
plant's v_cell / t_sensor / i_pack reach the AMS's measurement chain.

A stand-in for IFSSIM's electrical FMU (isc-fs/IFSSIM#669 §3-4), on its
contract: outputs `v_cell[95]` (V), `t_sensor[N]` (K), `soc_cell[95]`,
`i_pack` (A, + = discharge), input `cell_bleed[95]` (each element's LTC6811
balance switch). Every parameter is the catalogue's
(catalog/plants/tractive-system.yaml), cited or uncommissioned.

Each series element is cells_parallel cells in parallel, one equivalent cell
(capacity x P, resistances / P):

    v = OCV(soc) - i * R0(T) - v_rc
    d soc / dt = -i / (3600 * Q),  d v_rc / dt = (i * R1 - v_rc) / tau1
    m cp dT/dt = i^2 R0 + i v_rc - (T - T_amb) / R_th

with i the pack current plus the element's bleed (V / bleed_r while its
switch is on, as the on-time the LTC6811 model accounts). Over an interval
of constant current every term is closed form (soc linear, v_rc and T
first-order), so how often the plant is advanced does not change the result
beyond the float's last bits, and OCV(soc) is the cited table, linear
between its points. The bleed's heat is in the BMS board's resistors, not
the cell: not modelled.

The NTCs: 20 per LTC6811 on ADG731 channels 0-9 and 16-25 (AMS
ams_config.hpp:1143-1146), firmware slot k of chip c is module c//2's
slot (c % 2) * 20 + k. Which cell each NTC touches is in no document: they
are spread evenly over the chip's cells (`ntc_map: proportional`,
uncommissioned), and each reads its cell's temperature.
"""
from __future__ import annotations

import math
import random
from bisect import bisect_right
from typing import Optional

N_CELLS = 95
CELLS_PER_MODULE = 19
NTC_PER_CHIP = 20
# ADG731 channel of firmware NTC slot k (0-19) on a chip: AMS ams_config.hpp:1143-1146.
NTC_CHANNELS = tuple(range(10)) + tuple(range(16, 26))
MUX_CHANNELS = 32
INPUTS_PER_CHIP = 12
LTC_LSB_V = 100e-6          # LTC6811 cell register LSB (datasheet: 100 uV), the model's
UNUSED_RAW = 37000          # inputs no cell drives keep the model's default 3.7 V
K0 = 273.15


def cell_input(k: int, cells_per_chip=(9, 10)) -> tuple[int, int]:
    """(chip, input 0-11) of series element k: module m = k // 19 has chips
    2m (its first cells_per_chip[0]) and 2m + 1 (the rest), from C1 up
    (AMS bms_service.cpp:293-306, docs/BMS_LTC6811.md:129-139)."""
    m, j = divmod(k, CELLS_PER_MODULE)
    upper = int(cells_per_chip[0])
    return (2 * m, j) if j < upper else (2 * m + 1, j - upper)


def ntc_cell(chip: int, slot: int, cells_per_chip=(9, 10)) -> int:
    """The series element NTC `slot` of `chip` reads (`proportional`)."""
    m = chip // 2
    upper = int(cells_per_chip[0])
    n = upper if chip % 2 == 0 else int(cells_per_chip[1])
    first = m * CELLS_PER_MODULE + (0 if chip % 2 == 0 else upper)
    return first + slot * n // NTC_PER_CHIP


class Pack:
    """95 series elements in closed form between events. `advance(t_us)`
    integrates at the held current; `set_current`, `bleed` change it."""

    FAULTS = ("weak_cell", "weak_cell_capacity", "weak_cell_r0", "hot_cell", "hot_cell_dt",
              "imbalance_cell", "imbalance_soc")

    def __init__(self, params: dict, t_us: int = 0):
        self.params = params
        p = self.p
        self.n = int(p("cells_series"))
        par = int(p("cells_parallel"))
        rng = random.Random(int(p("spread_seed")))
        cap_spread, soc_spread = float(p("capacity_spread")), float(p("soc_spread"))
        # Deterministic per-element offsets: uniform in [-spread, +spread],
        # from a seeded generator (the same on every host and run).
        self.capacity = [float(p("cell_capacity")) * par * (1 + cap_spread * (2 * rng.random() - 1))
                         for _ in range(self.n)]
        soc0 = float(p("soc_init"))
        self.soc = [min(1.0, max(0.0, soc0 + soc_spread * (2 * rng.random() - 1)))
                    for _ in range(self.n)]
        self.r0_scale = [1.0] * self.n
        self.r0 = float(p("cell_r0")) / par
        self.r1 = float(p("cell_r1")) / par
        self.tau1 = float(p("cell_tau1"))
        self.alpha = float(p("cell_r0_alpha"))
        self.r_th = float(p("cell_r_th")) / par
        self.heat_cap = float(p("cell_mass")) * float(p("cell_cp")) * par
        self.ocv_soc = [float(x) for x in p("cell_ocv_soc")]
        self.ocv_v = [float(x) for x in p("cell_ocv")]
        self.v_rc = [0.0] * self.n
        self.temp = [float(p("t_init"))] * self.n
        self.t_amb = float(p("t_init"))
        self.hot: dict[int, float] = {}
        self.i_pack = 0.0
        self.bleed_i = [0.0] * self.n           # mean bleed current over the coming interval
        self.t_us = int(t_us)
        self.weak = -1

    def p(self, name):
        return self.params[name].value

    # -- the cell --------------------------------------------------------------

    def ocv(self, soc: float) -> float:
        xs, ys = self.ocv_soc, self.ocv_v
        soc = min(xs[-1], max(xs[0], soc))
        i = min(len(xs) - 2, max(0, bisect_right(xs, soc) - 1))
        return ys[i] + (ys[i + 1] - ys[i]) * (soc - xs[i]) / (xs[i + 1] - xs[i])

    def ocv_slope(self, soc: float) -> float:
        xs, ys = self.ocv_soc, self.ocv_v
        i = min(len(xs) - 2, max(0, bisect_right(xs, min(xs[-1], max(xs[0], soc))) - 1))
        return (ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i])

    def r0_at(self, k: int) -> float:
        """R0 at the element's temperature: R0 * max(1 + alpha (T - 25 C), 0.4)
        (the TFM fit the AMS's estimator uses, ams_config.hpp:487-498)."""
        return self.r0 * self.r0_scale[k] * max(1 + self.alpha * (self.temp[k] - K0 - 25), 0.4)

    def current(self, k: int) -> float:
        return self.i_pack + self.bleed_i[k]

    def v_cell(self, k: int) -> float:
        return self.ocv(self.soc[k]) - self.current(k) * self.r0_at(k) - self.v_rc[k]

    def t_cell(self, k: int) -> float:
        return self.temp[k] + self.hot.get(k, 0.0)

    def t_ntc(self, chip: int, slot: int, cells_per_chip=(9, 10)) -> Optional[float]:
        """What NTC `slot` of `chip` reads, K: its cell's temperature."""
        k = ntc_cell(chip, slot, cells_per_chip)
        return self.t_cell(k) if k < self.n else None

    @property
    def v_pack(self) -> float:
        return sum(self.v_cell(k) for k in range(self.n))

    # -- time ------------------------------------------------------------------

    def advance(self, t_us: int) -> None:
        t_us = int(t_us)
        dt = (t_us - self.t_us) / 1e6
        if dt <= 0:
            return
        e1 = math.exp(-dt / self.tau1)
        tau = self.heat_cap * self.r_th
        eth = math.exp(-dt / tau)
        for k in range(self.n):
            i = self.current(k)
            if i:
                self.soc[k] = min(1.0, max(0.0, self.soc[k] - i * dt / (3600 * self.capacity[k])))
            target = i * self.r1
            v0 = self.v_rc[k]
            self.v_rc[k] = target + (v0 - target) * e1
            # Heat i^2 R0 + i v_rc, with R0 at 25 C (its temperature
            # dependence enters the voltage, not the heat, so the interval
            # stays closed form): constant a plus b e^(-t/tau1) from the RC.
            a = i * i * self.r0 * self.r0_scale[k] + i * target
            b = i * (v0 - target)
            t_eq = self.t_amb + a * self.r_th
            kk = b / self.heat_cap / (1 / tau - 1 / self.tau1) if b else 0.0
            self.temp[k] = t_eq + kk * e1 + (self.temp[k] - t_eq - kk) * eth
        self.t_us = t_us

    def rate_v(self) -> float:
        """The fastest any element's voltage moves now, V/s (for when to
        exchange next)."""
        out = 0.0
        for k in range(self.n):
            i = self.current(k)
            r = abs(self.ocv_slope(self.soc[k]) * i / (3600 * self.capacity[k]))
            r += abs((i * self.r1 - self.v_rc[k]) / self.tau1)
            out = max(out, r)
        return out

    def rate_t(self) -> float:
        """The fastest any element's temperature moves now, K/s."""
        tau = self.heat_cap * self.r_th
        out = 0.0
        for k in range(self.n):
            i = self.current(k)
            t_eq = self.t_amb + (i * i * self.r0 * self.r0_scale[k] + i * self.v_rc[k]) * self.r_th
            out = max(out, abs(t_eq - self.temp[k]) / tau)
        return out

    # -- faults (IFSSIM#669 §4) -----------------------------------------------------

    def fault(self, name: str, value, faults: dict) -> None:
        """Apply a fault input; `faults` holds them all (indices pair with
        their values, -1 / None for none)."""
        faults[name] = value
        w = faults.get("weak_cell")
        if w is not None and 0 <= int(w) < self.n and name in ("weak_cell", "weak_cell_capacity",
                                                                "weak_cell_r0"):
            k = int(w)
            base = float(self.p("cell_capacity")) * int(self.p("cells_parallel"))
            if self.weak != k and 0 <= self.weak < self.n:
                self.r0_scale[self.weak] = 1.0
            self.weak = k
            self.capacity[k] = base * float(faults.get("weak_cell_capacity") or 1.0)
            self.r0_scale[k] = float(faults.get("weak_cell_r0") or 1.0)
        if name in ("hot_cell", "hot_cell_dt"):
            self.hot = {}
            h = faults.get("hot_cell")
            if h is not None and 0 <= int(h) < self.n:
                self.hot[int(h)] = float(faults.get("hot_cell_dt") or 0.0)
        if name in ("imbalance_cell", "imbalance_soc"):
            c, d = faults.get("imbalance_cell"), faults.get("imbalance_soc")
            if c is not None and d is not None and 0 <= int(c) < self.n:
                # One-shot: the element's charge moves by d at this instant.
                self.soc[int(c)] = min(1.0, max(0.0, self.soc[int(c)] + float(d)))
                faults["imbalance_soc"] = None


class ChainDriver:
    """What the pack puts on the LTC6811 chain model (models/renode/IsoSpi.cs,
    on the system's LTC6820 device): the cells in 100 uV and the NTCs in
    0.1 C, each pushed only when some value moved by at least that, in one
    monitor call; and the balance switches' on-time taken back as bleed."""

    def __init__(self, sim, system, device: str, cells_per_chip=(9, 10)):
        board, _, spi = system.resolve(system.devices[device]["spi"])
        self.sim, self.board, self.path = sim, board.name, f"{spi}.{device}"
        self.chips = int(next(d for d in system.devices.values()
                              if d.get("attach") == device).get("count", 10))
        self.cpc = tuple(int(c) for c in cells_per_chip)
        self.last_cells: Optional[list] = None
        self.last_temps: Optional[list] = None
        self.pushes = 0
        self.armed = False

    def __deepcopy__(self, memo):
        # The emulation is not the plant's to copy (a live op is tried on a
        # copy of the plant, vhil/worker.py): the copy shares this driver,
        # which applying an op never calls.
        return self

    def call(self, method: str, arg: Optional[str] = None) -> str:
        return self.sim.call(self.path, method, *([arg] if arg is not None else []),
                             board=self.board)

    def push(self, pack: Pack) -> None:
        raw = [UNUSED_RAW] * (self.chips * INPUTS_PER_CHIP)
        for k in range(pack.n):
            chip, inp = cell_input(k, self.cpc)
            v = pack.v_cell(k)
            if v is not None:               # an FMU that gives fewer cells leaves the rest
                raw[chip * INPUTS_PER_CHIP + inp] = max(0, min(0xFFFF, int(round(v / LTC_LSB_V))))
        if raw != self.last_cells:
            self.call("SetPlantCells", ",".join(map(str, raw)))
            self.last_cells = raw
            self.pushes += 1
        temps = [""] * (self.chips * MUX_CHANNELS)
        for chip in range(self.chips):
            for slot, ch in enumerate(NTC_CHANNELS):
                t = pack.t_ntc(chip, slot, self.cpc)
                if t is not None:
                    temps[chip * MUX_CHANNELS + ch] = str(int(round((t - K0) * 10)))
        if temps != self.last_temps:
            self.call("SetPlantTemperatures", ",".join(temps))
            self.last_temps = temps
            self.pushes += 1

    def take_bleed(self) -> tuple[int, list[int], list[int]]:
        """(now us, on-time us per element since the last take, DCC bit per
        element now)."""
        lines = self.call("TakeBleed").strip().splitlines()
        now = int(lines[0])
        on = [0] * (len(lines) - 1) * INPUTS_PER_CHIP
        bits = [0] * len(on)
        for chip, line in enumerate(lines[1:]):
            mask, _, times = line.partition(":")
            for c, us in enumerate(times.split(",")):
                on[chip * INPUTS_PER_CHIP + c] = int(us)
                bits[chip * INPUTS_PER_CHIP + c] = (int(mask) >> c) & 1
        n = (self.chips // 2) * CELLS_PER_MODULE
        out_on, out_bits = [0] * n, [0] * n
        for k in range(n):
            chip, inp = cell_input(k, self.cpc)
            out_on[k] = on[chip * INPUTS_PER_CHIP + inp]
            out_bits[k] = bits[chip * INPUTS_PER_CHIP + inp]
        return now, out_on, out_bits

    def fired(self) -> bool:
        """Whether the armed trigger went off (a chip's DCC went from none to
        some since it was armed): it disarms itself then."""
        if not self.armed:
            return False
        if self.call("BleedTrigger").strip().lower() == "false":
            self.armed = False
            return True
        return False

    def arm(self, on: bool) -> None:
        if on != self.armed:
            self.sim.call(self.path, "BleedTrigger", bool(on), board=self.board)
            self.armed = on


def sensor_volts(model_doc: dict, current_a: float) -> dict[str, float]:
    """An analog current sensor's outputs at current_a, from its catalogue
    model's transfer (catalog/models/<model>.yaml `analog`): offset + gain *
    I, clipped."""
    out = {}
    for name, o in model_doc["analog"]["outputs"].items():
        v = float(o["offset_V"]) + float(o["gain_V"]) * current_a
        out[name] = min(float(o.get("max_V", v)), max(float(o.get("min_V", v)), v))
    return out
