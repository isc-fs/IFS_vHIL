"""The tractive-system plant, TS-1 (M8, #147; docs/m8-plan.md): AIR+, AIR-
and the precharge relay, the inverter's DC link and its bleed, and the
inverter's report of the link on 0x466.

A Python stand-in for IFSSIM's electrical FMU (isc-fs/IFSSIM#669), on the
same contract: the inputs and outputs are named as that FMU's
(`air_pos_cmd`, `v_link`, ...), and its parameters are the FMU's parameters,
from catalog/plants/tractive-system.yaml, each cited or "uncommissioned".

Physics (closed form, exact between events):

  - a contact follows its coil after its close/open delay; a coil is
    energised when its drive pin is high and the coil supply (AIR+_VCC, which
    the AMS senses on PF9) is present. Faults: a welded contact stays
    closed; `air_external_open` drops both AIRs' contacts with their coils
    still driven; `pre_open_circuit` is a precharge path that never conducts;
  - the DC link is a capacitor, c_link * c_scale. With AIR- and AIR+ closed
    it is the pack's voltage (the pack's own resistance, ~0.3 ohm against
    400 uF, is a 0.1 ms time constant: not modelled). With AIR- and the
    precharge relay closed it charges towards the pack through r_precharge;
    the bleed (r_discharge, while the shutdown circuit is open or the ECU
    secures it, ECU discharge.hpp:18-27), a leak (r_leak) and an inverter
    bleeder (r_inverter_bleed, null: none) discharge it. Between events
    v(t) = v_eq + (v0 - v_eq) * exp(-dt / tau);
  - the inverter reports the link every vdc_period, rounded to vdc_resolution
    (1 V, 10 bits), and below sense_floor either the floor (`hold`) or
    nothing (`silent`).

The pack voltage is an input (`v_pack`): the sum of the LTC6811 chain
model's cells, the cell source until the FMU provides v_cell.

Times are integer microseconds of virtual time. Inputs are applied in time
order (`set`), at their exact times (a coil edge's from the GPIO probe);
`advance` integrates to a time; `forecast` returns the coming reports
assuming no further input change, without changing the plant.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "catalog" / "plants"

INPUTS = ("air_pos_cmd", "air_neg_cmd", "pre_cmd", "discharge_cmd", "sdc_closed",
          "v_pack", "i_inverter_dc")
FAULTS = ("welded_air_pos", "welded_air_neg", "pre_open_circuit", "air_external_open",
          "c_scale", "r_leak")
# Inputs a scenario row scripts (IFSSIM#669 §3): the inverter's and the
# charger's DC current (A, + = discharge / + = into the pack) and the ambient
# temperature (K).
SCRIPTED = ("i_inverter_dc", "i_charger", "t_ambient")
# Pack parameters a run may override (a row): the pack is rebuilt from them.
PACK_STATE = ("cells_series", "cells_parallel", "cell_capacity", "cell_ocv_soc", "cell_ocv",
              "cell_r0", "cell_r0_alpha", "cell_r1", "cell_tau1", "cell_mass", "cell_cp",
              "cell_r_th", "t_init", "soc_init", "soc_spread", "capacity_spread", "spread_seed")
CONTACTS = ("air_pos", "air_neg", "pre")
# Each fault input's value with no fault, which is also its type for the
# editor's controls (the system contract's `plants`, vhil/server/decode.py).
FAULT_DEFAULTS = {"welded_air_pos": False, "welded_air_neg": False, "pre_open_circuit": False,
                  "air_external_open": False, "c_scale": 1.0, "r_leak": None}


@dataclass(frozen=True)
class Parameter:
    name: str
    value: object
    unit: Optional[str]
    status: str
    source: str
    validated_against: Optional[str]


def load_parameters(plant: str = "tractive-system", overrides: Optional[dict] = None
                    ) -> dict[str, Parameter]:
    """A plant's parameters from the catalogue; `overrides` (name -> value)
    replace values for one run and mark them `override` (a test's choice:
    neither cited nor commissioned)."""
    doc = yaml.safe_load((CATALOG / f"{plant}.yaml").read_text())
    out = {n: Parameter(n, p["value"], p["unit"], p["status"], p["source"], p["validated_against"])
           for n, p in doc["parameters"].items()}
    for name, value in (overrides or {}).items():
        if name not in out:
            raise KeyError(f"no parameter '{name}' in {plant}")
        out[name] = Parameter(name, value, out[name].unit, "override",
                              f"set by the run (catalogue: {out[name].value!r})", None)
    return out


def uncommissioned(params: dict[str, Parameter]) -> list[str]:
    """The parameters a report must call uncommissioned: not cited, or not
    validated against a car log, or overridden by the run."""
    return sorted(n for n, p in params.items()
                  if p.status != "cited" or p.validated_against is None)


def _us(seconds: float) -> int:
    return int(round(float(seconds) * 1e6))


def inverter_report(volts: float, params: dict) -> Optional[int]:
    """What the inverter puts in DCBus_Voltage_V for a link at `volts`:
    rounded to vdc_resolution, the floor below sense_floor (`hold`), or None
    for no frame (`silent`). The inverter's, not the battery's: the vHIL
    applies it to the stand-in's link and to an FMU's alike."""
    res = float(params["vdc_resolution"].value)
    floor = float(params["sense_floor"].value)
    if floor > 0 and volts < floor:
        if params["below_floor"].value == "silent":
            return None
        return int(math.floor(floor / res + 0.5) * res)
    return int(math.floor(volts / res + 0.5) * res)


def vdc_frame(volts: Optional[int]) -> Optional[bytes]:
    """0x466 EMC_TX_STATE_7 as the inverter sends it: DCBus_Voltage_V 10 bits
    LE from bit 16 (ECU NX0001_STS04_A16.dbc:82-87), the E2E counter and CRC
    left 0 (the ECU checks neither, vhil/plants.py _e2e). None: no frame."""
    if volts is None:
        return None
    v = max(0, min(int(volts), 0x3FF))
    return bytes([0, 0, v & 0xFF, v >> 8, 0, 0])


@dataclass
class TractiveSystem:
    params: dict = field(default_factory=load_parameters)
    t_us: int = 0
    v_link: float = 0.0
    inputs: dict = field(default_factory=lambda: {
        "air_pos_cmd": False, "air_neg_cmd": False, "pre_cmd": False, "discharge_cmd": False,
        "sdc_closed": False, "v_pack": 0.0, "i_inverter_dc": 0.0})
    faults: dict = field(default_factory=lambda: dict(FAULT_DEFAULTS))
    closed: dict = field(default_factory=lambda: {c: False for c in CONTACTS})
    # (t_us, contact, closed): contact moves pending, in time order.
    pending: list = field(default_factory=list)
    # (t_us, contact, closed, v_link) every contact move, with the link's
    # voltage at that instant (before the move), for traces and assertions.
    history: list = field(default_factory=list)
    # The circuit's current segment: from seg_t (us) at seg_v (V), of kind
    # ('tied', v_pack, 0) | ('hold', None, inf) | ('rc', v_eq, tau). The link is
    # always computed from the segment's start, and a segment starts only when
    # the circuit changes, so how often the plant is advanced never changes
    # the result (not even in the last bit of a float).
    seg_t: int = 0
    seg_v: float = 0.0
    seg: tuple = ("hold", None, math.inf)

    def p(self, name: str):
        return self.params[name].value

    # -- inputs ------------------------------------------------------------------

    def set(self, name: str, value, t_us: Optional[int] = None) -> None:
        """An input changes at t_us (default: the plant's time), no earlier
        than the plant's time."""
        if name not in INPUTS:
            raise KeyError(f"no input '{name}' (inputs: {', '.join(INPUTS)})")
        t_us = self.t_us if t_us is None else int(t_us)
        if t_us < self.t_us:
            raise ValueError(f"input {name} at {t_us} us, before the plant's {self.t_us} us")
        self.advance(t_us)
        if name in ("v_pack", "i_inverter_dc"):
            self.inputs[name] = float(value)
        else:
            self.inputs[name] = bool(value)
            self._coils(t_us)
        self._resegment(t_us)

    def fault(self, name: str, value, t_us: Optional[int] = None) -> None:
        """Set a fault input (test API): welded_air_pos, welded_air_neg,
        pre_open_circuit, air_external_open (bool); c_scale (x c_link);
        r_leak (ohm, None for none)."""
        if name not in FAULTS:
            raise KeyError(f"no fault '{name}' (faults: {', '.join(FAULTS)})")
        t_us = self.t_us if t_us is None else int(t_us)
        self.advance(t_us)
        self.faults[name] = value
        self._resegment(t_us)

    def _coils(self, t_us: int) -> None:
        """Schedule each contact's move after its delay when its coil's
        energisation changes; a newer change replaces a pending one."""
        supply = self.inputs["sdc_closed"]
        for contact, cmd, (close, open_) in (
                ("air_pos", "air_pos_cmd", ("air_close", "air_open")),
                ("air_neg", "air_neg_cmd", ("air_close", "air_open")),
                ("pre", "pre_cmd", ("pre_close", "pre_open"))):
            energised = supply and self.inputs[cmd]
            target = self._target(contact)
            if energised == target:
                continue
            self.pending = [e for e in self.pending if e[1] != contact]
            if energised != self.closed[contact]:
                self.pending.append((t_us + _us(self.p(close if energised else open_)),
                                     contact, energised))
            self.pending.sort()

    def _target(self, contact: str) -> bool:
        """Where a contact is going: its last pending move, else where it is."""
        for _, c, closed in reversed(self.pending):
            if c == contact:
                return closed
        return self.closed[contact]

    # -- outputs -------------------------------------------------------------------

    def contact(self, contact: str) -> bool:
        """A contact's state as the circuit sees it, faults included."""
        if contact == "air_pos" and self.faults["welded_air_pos"]:
            return True
        if contact == "air_neg" and self.faults["welded_air_neg"]:
            return True
        if contact in ("air_pos", "air_neg") and self.faults["air_external_open"]:
            return False
        return self.closed[contact]

    @property
    def outputs(self) -> dict:
        return {"v_link": self.v_link, "v_pack": self.inputs["v_pack"],
                "air_pos_closed": self.contact("air_pos"), "air_neg_closed": self.contact("air_neg"),
                "pre_closed": self.contact("pre")}

    def bleeding(self) -> bool:
        """The discharge bleed is across the link: SDC open (its coil
        unpowered, NC contact closed) or the ECU securing it."""
        return (not self.inputs["sdc_closed"]) or self.inputs["discharge_cmd"]

    def _segment(self) -> tuple[str, float, float]:
        """('tied', v_pack, 0) | ('hold', v, inf) | ('rc', v_eq, tau) for the
        circuit as it is now."""
        v_pack = self.inputs["v_pack"]
        if self.contact("air_neg") and self.contact("air_pos"):
            return "tied", v_pack, 0.0
        g_charge = 0.0
        if self.contact("air_neg") and self.contact("pre") and not self.faults["pre_open_circuit"]:
            g_charge = 1.0 / float(self.p("r_precharge"))
        g = g_charge
        if self.bleeding():
            g += 1.0 / float(self.p("r_discharge"))
        for r in (self.faults["r_leak"], self.p("r_inverter_bleed")):
            if r is not None:
                g += 1.0 / float(r)
        if g == 0.0:
            return "hold", None, math.inf
        c = float(self.p("c_link")) * float(self.faults["c_scale"])
        return "rc", g_charge * v_pack / g, c / g

    def _value(self, t_us: int) -> float:
        kind, v_eq, tau = self.seg
        if kind == "tied":
            return v_eq
        if kind == "hold":
            return self.seg_v
        return v_eq + (self.seg_v - v_eq) * math.exp(-(t_us - self.seg_t) / 1e6 / tau)

    def _resegment(self, t_us: int) -> None:
        """At t_us (the plant's time), start a new segment if the circuit
        changed."""
        self.v_link = self._value(t_us)
        new = self._segment()
        if new != self.seg:
            self.seg_t, self.seg_v, self.seg = t_us, self.v_link, new
            self.v_link = self._value(t_us)     # tied the instant a path closes

    def advance(self, t_us: int) -> None:
        """Integrate to t_us, moving contacts at their times on the way."""
        t_us = int(t_us)
        while self.pending and self.pending[0][0] <= t_us:
            at, contact, closed = self.pending.pop(0)
            self.t_us = max(self.t_us, at)
            if self.closed[contact] != closed:
                self.history.append((at, contact, closed, self._value(self.t_us)))
                self.closed[contact] = closed
            self._resegment(self.t_us)
        self.t_us = max(self.t_us, t_us)
        self.v_link = self._value(self.t_us)

    # -- the inverter's report -------------------------------------------------------

    def report(self, volts: Optional[float] = None) -> Optional[int]:
        """What the inverter puts in DCBus_Voltage_V for a link at `volts`
        (default: now): inverter_report."""
        return inverter_report(self.v_link if volts is None else volts, self.params)

    def forecast(self, ticks_from_us: int, period_us: int, horizon_us: int
                 ) -> tuple[list[tuple[int, Optional[int]]], bool]:
        """The reports at ticks ticks_from_us + k * period_us up to
        ticks_from_us + horizon_us, assuming no input changes: [(t_us,
        report)] where the report changes (the first tick always), and
        whether the link has settled (nothing pending, and the report will
        not change again). The plant itself is not changed."""
        sim = copy.deepcopy(self)
        out: list = []
        last = object()
        t = ticks_from_us
        end = ticks_from_us + horizon_us
        while t <= end:
            sim.advance(t)
            r = sim.report()
            if r != last:
                out.append((t, r))
                last = r
            if not sim.pending and sim._settled(r):
                return out, True
            t += period_us
        return out, False

    def _settled(self, report) -> bool:
        kind, v_eq, _ = self.seg
        if kind in ("tied", "hold"):
            return True
        # The link only approaches v_eq, never reaches it: what it reads from
        # now on is what a value just short of v_eq reads, the same as now
        # once both read alike (the report is monotonic in the voltage).
        limit = v_eq if self.v_link == v_eq else math.nextafter(v_eq, self.v_link)
        return self.report(limit) == report and self.report(self.v_link) == report


# -- on the co-simulation port ------------------------------------------------------

COIL_SIGNALS = ("air_pos_cmd", "air_neg_cmd", "pre_cmd", "discharge_cmd")


class PortInputs:
    """What a tractive-system plant reads from the co-simulation port, by the
    contract's names (docs/m8-plan.md), shared by the stand-in (TsPlant) and
    the FMU master (vhil/fmi.py FmuPlant): gpio_out `air_pos_cmd`,
    `air_neg_cmd`, `pre_cmd` and optionally `discharge_cmd`, each change with
    its own time; gpio_in `sdc_closed` (the coil supply as the AMS senses it
    on PF9: the level a scenario or test drives there), and `v_pack` from the
    LTC6811 chain's cells (`cells`, the system's LTC6820 device, e.g.
    "isospi"), both at the step's time; without a chain, a fixed `v_pack`
    (V) if given."""

    event_driven = True

    def __init__(self, cells: Optional[str], cells_per_chip, v_pack: Optional[float] = None):
        self.cells, self.v_pack = cells, v_pack
        self.counts = ",".join(str(int(c)) for c in cells_per_chip)
        self.started = False
        self.last: dict = {}
        self.next_us: Optional[int] = None
        self.steps = 0

    def triggers(self, port) -> list[str]:
        return [n for n in COIL_SIGNALS if n in port.signals]

    def changes(self, port, t_us: int, not_before: int) -> list[tuple[int, str, object]]:
        """Every input change since the last call, [(t_us, name, value)] in
        time order (coil edges at their own times, no earlier than
        not_before; the rest at t_us), only those that change something."""
        out = []
        for name in self.triggers(port):
            if not self.started:
                out.append((t_us, COIL_SIGNALS.index(name), name, port.level(name)))
                port.edges(name)                # history up to now: consumed
            else:
                out += [(max(t, not_before), COIL_SIGNALS.index(name), name, level)
                        for t, level in port.edges(name)]
        if "sdc_closed" in port.signals:
            out.append((t_us, 10, "sdc_closed", port.driven("sdc_closed")))
        if self.cells is not None:
            system = port.sim.system
            board, _, spi = system.resolve(system.devices[self.cells]["spi"])
            mv = int(port.sim.call(f"{spi}.{self.cells}", "CellSum", self.counts,
                                   board=board.name).strip(), 0)
            out.append((t_us, 11, "v_pack", mv / 1000))
        elif self.v_pack is not None:
            out.append((t_us, 11, "v_pack", float(self.v_pack)))
        result = []
        for t, _, name, value in sorted(out):
            if name in COIL_SIGNALS or self.last.get(name) != value:
                self.last[name] = value
                result.append((t, name, value))
        self.started = True
        return result

    @staticmethod
    def first_tick(port, report: str, t_us: int, period_us: int) -> tuple[int, int]:
        """The first tick of the report's sender after t_us, and its period."""
        grid = port.ticks(report)
        if grid is None:
            return t_us + period_us, period_us
        start, period_us = grid
        return (start if t_us < start else
                start + ((t_us - start) // period_us + 1) * period_us), period_us


class TsPlant(PortInputs):
    """The tractive system on a system's co-simulation port (vhil/cosim.py),
    event-driven: stepped when a coil (or the ECU's discharge drive) changes,
    when its forecast of the inverter's 0x466 runs out, and whenever the
    port's other plants are. Inputs as PortInputs reads them; output can_tx
    `v_link` (0x466 on the inverter bus), unless `report` is None because an
    Inverter plant reports the link itself (Inverter(dc_link=...)).

    At each step it applies every input change at its own time, then hands
    the emulation the inverter's reports until the link settles (or
    `horizon_ms`, then it asks to be stepped again): how often it is stepped
    doesn't change what it sends.

    With a `pack` (vhil/plant_pack.py, M9 prep #318) the plant is also the
    cell source: the pack's elements give v_pack, carry the pack current
    (`i_inverter_dc` - `i_charger`, scripted, while both AIRs are closed)
    and lose charge through their balance switches; the plant puts them on
    the LTC6811 chain (`chain`, a vhil.plant_pack.ChainDriver) and the pack
    current on the current sensor's legs (`current`: the sensor's catalogue
    model and the port's analog signals for its outputs), each only when it
    moves by the measurement's resolution. Otherwise v_pack is the chain
    model's cell sum, as in M8."""

    def __init__(self, ts: Optional[TractiveSystem] = None, *, cells: Optional[str] = None,
                 v_pack: Optional[float] = None, report: Optional[str] = "v_link",
                 horizon_ms: float = 3000, pack=None, chain=None, current=None):
        self.ts = ts or TractiveSystem()
        super().__init__(None if pack is not None else cells, self.ts.p("cells_per_chip"), v_pack)
        self.report, self.horizon_us = report, int(horizon_ms * 1000)
        self.dirty = True                   # an input changed: forecast again
        self._recorded = 0
        self.pack, self.chain, self.current = pack, chain, current
        self.scripted = {"i_inverter_dc": 0.0, "i_charger": 0.0}
        self.pack_faults: dict = {}
        self.pack_next: Optional[int] = None
        self.ts_next: Optional[int] = None
        self.sensor_i: Optional[float] = None
        self.bleeding = False
        self.bled = [0] * (pack.n if pack is not None else 0)
        self.pack_records: list = []
        self._last_rec: dict = {}
        self.exchange_due = False

    @property
    def params(self) -> dict:
        return self.ts.params

    # -- the pack ----------------------------------------------------------------

    def _pack_current(self) -> float:
        ts = self.ts
        tied = ts.contact("air_pos") and ts.contact("air_neg")
        return (self.scripted["i_inverter_dc"] - self.scripted["i_charger"]) if tied else 0.0

    def _to(self, t_us: int) -> None:
        """Advance the circuit and the pack together to t_us: the pack
        current changes where a contact moves."""
        ts, pack = self.ts, self.pack
        if pack is None:
            ts.advance(t_us)
            return
        while ts.pending and ts.pending[0][0] <= t_us:
            at = ts.pending[0][0]
            pack.advance(at)
            ts.advance(at)
            self._sync_pack(at)
        pack.advance(t_us)
        ts.advance(t_us)
        self._sync_pack(t_us)

    def _sync_pack(self, t_us: int) -> None:
        """The pack current for the circuit as it is now, and the circuit's
        v_pack for the pack as it is now."""
        pack, ts = self.pack, self.ts
        i = self._pack_current()
        if i != pack.i_pack:
            pack.i_pack = i
            self.exchange_due = True        # the sensor's legs, the cells' sag
        v = pack.v_pack
        if ts.inputs["v_pack"] != v:
            ts.set("v_pack", v, t_us)
            self.dirty = True

    def _exchange(self, port, t_us: int) -> None:
        """Balance switches in, measurements out; when to come back."""
        pack, chain = self.pack, self.chain
        p = self.ts.p
        if chain is not None:
            _, on, bits = chain.take_bleed()
            r = float(p("bleed_r"))
            # The bleed removes charge only: the AMS opens every switch while
            # it converts (IFS08-CE-AMS bms_poll_task.cpp:372-420), so the
            # balance current's IR drop is never measured.
            for k in range(min(pack.n, len(on))):
                if on[k]:
                    pack.soc[k] = max(0.0, pack.soc[k] - pack.v_cell(k) / r * on[k] / 1e6
                                      / (3600 * pack.capacity[k]))
                if bool(on[k] or bits[k]) != bool(self.bled[k]):
                    self.bled[k] = int(bool(on[k] or bits[k]))
                    self.pack_records.append((t_us, f"bleed_c{k}", self.bled[k]))
            self.bleeding = any(bits) or any(on)
            chain.arm(not self.bleeding)
            self._sync_pack(t_us)
            chain.push(pack)
        if self.current is not None:
            model_doc, signals = self.current
            i = pack.i_pack
            if i != self.sensor_i:
                from vhil.plant_pack import sensor_volts
                for out, v in sensor_volts(model_doc, i).items():
                    port.set_voltage(signals[out], v)
                self.sensor_i = i
        self._record(t_us)
        # Next exchange: when an element's voltage moves one LTC6811 LSB or a
        # temperature 0.1 C, no sooner than cell_exchange_min, at most
        # bleed_poll apart while a switch is on (its on-time is read then).
        from vhil.plant_pack import LTC_LSB_V
        rate_v = pack.rate_v()
        if self.bleeding:
            rate_v = max(rate_v, max(abs(pack.ocv_slope(pack.soc[k]) * pack.v_cell(k)
                                         / float(p("bleed_r")) / (3600 * pack.capacity[k]))
                                     for k in range(pack.n)))
        rate_t = pack.rate_t()
        dt = min(LTC_LSB_V / rate_v if rate_v else math.inf, 0.1 / rate_t if rate_t else math.inf)
        if self.bleeding:
            dt = min(dt, float(p("bleed_poll")))
        if dt > 600:
            self.pack_next = None           # settled: under one LSB in 10 min
        else:
            self.pack_next = t_us + max(_us(p("cell_exchange_min")), _us(dt))
        if (self.scripted["i_inverter_dc"] or self.scripted["i_charger"]) and self.ts.pending:
            # A contact move that starts or stops the current: be there.
            at = self.ts.pending[0][0]
            self.pack_next = at if self.pack_next is None else min(self.pack_next, at)
        self.exchange_due = False

    def _record(self, t_us: int) -> None:
        pack = self.pack
        v = [pack.v_cell(k) for k in range(pack.n)]
        now = {"v_cell_min": round(min(v), 4), "v_cell_max": round(max(v), 4),
               "t_cell_max": round(max(pack.t_cell(k) for k in range(pack.n)) - 273.15, 1),
               "i_pack": round(pack.i_pack, 3), "soc_min": round(min(pack.soc), 5),
               "v_pack": round(sum(v), 2)}
        for k, val in now.items():
            if self._last_rec.get(k) != val:
                self._last_rec[k] = val
                self.pack_records.append((t_us, k, val))

    # -- the port -----------------------------------------------------------------

    def apply(self, values: dict, t_us: int) -> None:
        """Set fault inputs, scripted inputs or parameters (a scenario's
        `plant` row) at t_us; a parameter becomes an override for the rest
        of the run."""
        ts = self.ts
        self._to(t_us)
        for name, value in values.items():
            if name in FAULTS:
                ts.fault(name, value, t_us)
            elif name in SCRIPTED:
                if name == "t_ambient":
                    if self.pack is not None:
                        self.pack.t_amb = float(value)
                else:
                    self.scripted[name] = float(value)
                    if name in INPUTS:
                        ts.set(name, value, t_us)
            elif self.pack is not None and name in self.pack.FAULTS:
                self.pack.fault(name, value, self.pack_faults)
            elif name in ts.params:
                old = ts.params[name]
                ts.params = {**ts.params, name: Parameter(
                    name, value, old.unit, "override", f"set by the run (catalogue: {old.value!r})",
                    None)}
                ts._resegment(t_us)
                if self.pack is not None and name in PACK_STATE:
                    # A pack parameter (a 15 % pack, a smaller cell): the pack
                    # starts again from it, its faults re-applied.
                    from vhil.plant_pack import Pack
                    self.pack = Pack(ts.params, t_us)
                    faults, self.pack_faults = dict(self.pack_faults), {}
                    for f, v in faults.items():
                        if v is not None:
                            self.pack.fault(f, v, self.pack_faults)
            else:
                raise KeyError(f"no fault, input or parameter '{name}' (settable: "
                               f"{', '.join(sorted(settable()))})")
        if self.pack is not None:
            self._sync_pack(t_us)
            self.pack_next = t_us           # exchange now
        self.dirty = True

    def records(self, plant: str, upto_us: int) -> list[dict]:
        """Trace records (docs/scenarios.md, `plant:` signals) of what the
        plant did up to upto_us, each once: every contact move
        (`<contact>_closed`, with `v_link` then), and at each contact's
        closing the link as a fraction of the pack (`<contact>_close_ratio`:
        AIR+'s against the FS rule's 90 %, the firmware's 95 %); with a pack,
        at each exchange whatever changed of `v_cell_min`, `v_cell_max` (V),
        `t_cell_max` (C), `i_pack` (A), `soc_min`, `v_pack` (V), and each
        element's balance switch (`bleed_c<k>`, 1 while it bled)."""
        out = []
        # Every input change up to now has been applied (an armed edge stops
        # the run and steps the plant first), so the plant may move to now.
        self._to(max(self.ts.t_us, upto_us))
        moves = self.ts.history
        while self._recorded < len(moves) and moves[self._recorded][0] <= upto_us:
            t, contact, closed, v = moves[self._recorded]
            self._recorded += 1
            out.append({"kind": "plant", "t_us": t, "plant": plant, "name": f"{contact}_closed",
                        "value": int(closed)})
            out.append({"kind": "plant", "t_us": t, "plant": plant, "name": "v_link",
                        "value": round(v, 3)})
            if closed:
                v_pack = self.ts.inputs["v_pack"]
                out.append({"kind": "plant", "t_us": t, "plant": plant,
                            "name": f"{contact}_close_ratio",
                            "value": round(v / v_pack, 4) if v_pack else 0.0})
        keep = []
        for t, name, value in self.pack_records:
            if t <= upto_us:
                out.append({"kind": "plant", "t_us": t, "plant": plant, "name": name,
                            "value": value})
            else:
                keep.append((t, name, value))
        self.pack_records = keep
        out.sort(key=lambda r: r["t_us"])
        return out

    @property
    def next_us(self) -> Optional[int]:
        ns = [n for n in (self.ts_next, self.pack_next) if n is not None]
        return min(ns) if ns else None

    @next_us.setter
    def next_us(self, value) -> None:
        self.ts_next = value

    def state(self, t_us: int) -> dict:
        """What the plant is at t_us (no earlier than its own time), for the
        trace's per-slice `plant` records (the editor's state panel;
        docs/scenarios.md, "Signals"): the link and the pack (V, to 0.1), the
        contacts as the circuit sees them (faults included), the coil supply
        as the plant reads it, the bleed, and the pack current (0 without a
        pack); with a pack, also the cells' extremes. Read only: past the plant's time it reads a copy, so neither
        the plant nor what it sends changes. (At a slice end records() has
        advanced the plant there already, so it reads the plant itself.)"""
        ts = self.ts
        if t_us > ts.t_us:
            ts = copy.deepcopy(ts)
            ts.advance(t_us)
        return {"v_link": round(ts.v_link, 1), "v_pack": round(float(ts.inputs["v_pack"]), 1),
                **{f"{c}_closed": int(ts.contact(c)) for c in CONTACTS},
                "sdc_closed": int(bool(ts.inputs["sdc_closed"])),
                "discharge": int(ts.bleeding()), "i_pack": 0,
                **(self._pack_state() if self.pack is not None else {})}

    def _pack_state(self) -> dict:
        pack = self.pack
        v = [pack.v_cell(k) for k in range(pack.n)]
        return {"i_pack": round(pack.i_pack, 3), "v_cell_min": round(min(v), 4),
                "v_cell_max": round(max(v), 4),
                "t_cell_max": round(max(pack.t_cell(k) for k in range(pack.n)) - 273.15, 1),
                "soc_min": round(min(pack.soc), 5), "bleeding": sum(self.bled)}

    def step(self, port, t_us: int) -> None:
        self.steps += 1
        ts = self.ts
        first = not self.started
        changes = self.changes(port, t_us, ts.t_us)
        for t, name, value in changes:
            self._to(t)
            ts.set(name, value, t)
            if self.pack is not None:
                self._sync_pack(t)
        self._to(t_us)
        if self.chain is not None and self.chain.fired():
            self.exchange_due = True        # balancing started: the trigger stopped the run
        if self.pack is not None and (first or changes or self.exchange_due or (
                self.pack_next is not None and t_us >= self.pack_next)):
            self._exchange(port, t_us)
        due = self.ts_next is not None and t_us >= self.ts_next
        if not (changes or self.dirty or due):
            return                          # the forecast stands
        self.dirty = False
        if self.report is None:
            self.ts_next = None
            return
        first_tick, period_us = self.first_tick(port, self.report, t_us, _us(ts.p("vdc_period")))
        entries, settled = ts.forecast(first_tick, period_us, self.horizon_us)
        port.schedule(self.report, [(t, vdc_frame(r)) for t, r in entries],
                      period_ms=period_us / 1000)
        self.ts_next = None if settled else first_tick + self.horizon_us


def settable(plant: str = "tractive-system") -> set[str]:
    """What a scenario's `plant` row may set on a plant of this kind: the
    tractive system's fault inputs and parameters; the shutdown circuit's
    switches, its reset button and parameters (vhil/plant_sdc.py)."""
    if plant == "shutdown-circuit":
        from vhil.plant_sdc import settable as sdc_settable
        return sdc_settable(plant)
    from vhil.plant_pack import Pack
    return set(FAULTS) | set(SCRIPTED) | set(Pack.FAULTS) | set(load_parameters(plant))


def _pack_parts(system, spec: dict, params: dict, sim) -> dict:
    """The pack, and on a running sim its chain and current-sensor drivers."""
    from vhil.plant_pack import ChainDriver, Pack
    out = {"pack": Pack(params)}
    if sim is not None and spec.get("cells"):
        out["chain"] = ChainDriver(sim, system, spec["cells"], params["cells_per_chip"].value)
    if spec.get("current"):
        dev = system.devices[spec["current"]]
        by_pin = {o: e for o, e in dev.get("outputs", {}).items()}
        signals = {}
        for sname, sig in system.doc["port"]["signals"].items():
            (kind, endpoint), = sig.items()
            for out_name, pin in by_pin.items():
                if kind == "analog" and endpoint == pin:
                    signals[out_name] = sname
        out["current"] = (dev["model_doc"], signals)
    return out


# Kinds in the order a step runs them: the shutdown circuit drives the coil
# supply (`sdc_closed`) the tractive system reads in the same step.
KINDS = ("shutdown-circuit", "tractive-system")


def make_plants(system, sim=None) -> dict:
    """The plants a system's `plants:` declares, ready for its port, in the
    order they step (KINDS, then the file's). A tractive system with `pack:
    true` gets the per-cell pack (vhil/plant_pack.py); on a running `sim` it
    also drives the LTC6811 chain (`cells`) and the current sensor
    (`current`, its outputs on the port's analog signals)."""
    from vhil.plant_sdc import ShutdownCircuit, SdcPlant
    out = {}
    specs = sorted(system.plants.items(),
                   key=lambda kv: KINDS.index(kv[1]["plant"]) if kv[1]["plant"] in KINDS else len(KINDS))
    for name, spec in specs:
        params = load_parameters(spec["plant"], spec.get("params"))
        if spec["plant"] == "tractive-system":
            extra = {}
            if spec.get("pack"):
                extra = _pack_parts(system, spec, params, sim)
            out[name] = TsPlant(TractiveSystem(params=params), cells=spec.get("cells"), **extra)
        elif spec["plant"] == "shutdown-circuit":
            out[name] = SdcPlant(ShutdownCircuit(params))
        else:
            raise ValueError(f"plant '{name}': no implementation of {spec['plant']}")
    return out
