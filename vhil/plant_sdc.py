"""The shutdown circuit, TS-2 (M8, #147; docs/m8-plan.md "TS-2"): boolean
series logic with the AMS backplane's two latch relays, feeding AIR+_VCC,
the contactor coils' supply, which the AMS senses on PF9 (TSMS_FIL).

Traced (isc-fs/IFS08-ES main bcb4511, `boards/AMS_IFS08/kicad/AMS IFS08.kicad_sch`,
its netlist; docs/backplanes/ams.md):

    SC_IN (J1.14) -> J4.5..J4.6 (off-board, untraced) -> J7.7..J7.8 (HV_INTERLOCK+/-,
    "interlock amphenol") -> K4 IMD latch (21->24) -> K5 AMS latch (21->24)
    -> J2.4..J2.3 (off-board, untraced) -> SC_OUT (J1.15) -> [harness: no schematic]
    -> TSMS = AIR+_VCC (J1.16): the three coils' high side, and PF9 through R18 62k.

K4 and K5 (RTE2xAxx DPDT) are monostable relays wired as latches: the coil's
low side is Q8/Q10, whose gate is IMD_OK (J1.3) / AMS_OK (PB4, J1.4); its
high side is RST_IMD / RST_BMS, fed by the RST_EXT button (J1.7, through
D9/D10) and held by the relay's own pole 1 (11 = +12 V -> 14). So a latch
closes only while its OK line is high and RST_EXT is pressed, holds while OK
stays high, and drops for good when OK falls: open after every power-on
until a press. One RST_EXT resets both. (The library symbol says "RT2
bistable" and the BOM has no part number: the relay type is unconfirmed,
the wiring is the self-hold.)

Everything between SC_OUT and AIR+_VCC is in the car's harness, of which no
schematic exists: BSPD, inertia switch, BOTS, shutdown buttons, TSMS, and
the uDV's K1 (MicroDV2 J7.1/J7.2, isc-fs/IFS09-DV-uDV feat/5-hardware
d2e4250; its series placement untraced). Each is a plain series input here
(uncommissioned: no latch is modelled where none is traced; the uDV's K1 is
in series only when the parameter `udv_k1_in_series` says so). Order does
not matter for a series AND; it would for diagnostics the car lacks.

Relays switch instantly (their delays are unknown: the AIRs' own delays are
the tractive-system plant's); the coil supply follows at the plant's step,
at most one sync quantum after an AMS_OK edge.
"""
from __future__ import annotations

from typing import Optional

from vhil.plant_ts import Parameter, load_parameters

PLANT = "shutdown-circuit"
# Series switches a scenario row sets (True = closed / OK), and the RST_EXT
# button (True = pressed).
SWITCHES = ("tsms", "hv_interlock", "interlocks_ok", "imd_ok", "bspd_ok", "inertia_ok",
            "bots_ok", "estop_ok")
INPUTS = SWITCHES + ("reset",)
DEFAULTS = {**{n: True for n in SWITCHES}, "tsms": False, "reset": False}
# Board outputs the circuit reads (port gpio_out signals): AMS_OK, and the
# uDV's K1 drive where the system has a uDV.
GPIO = ("ams_ok", "udv_sdc")


class ShutdownCircuit:
    """The logic, in virtual time (integer us). `set` changes an input or a
    board output's level at t_us; `closed` is AIR+_VCC."""

    def __init__(self, params: Optional[dict] = None):
        self.params = params or load_parameters(PLANT)
        self.inputs = dict(DEFAULTS)
        self.gpio = {"ams_ok": False, "udv_sdc": False}
        self.latch = {"imd": False, "ams": False}       # K4, K5: open at power-on
        self.t_us = 0
        self.history: list = []                          # (t_us, name, value) on change
        self._last = self.outputs()

    def set(self, name: str, value, t_us: int) -> None:
        if name in INPUTS:
            self.inputs[name] = bool(value)
        elif name in GPIO:
            self.gpio[name] = bool(value)
        else:
            raise KeyError(f"no input '{name}' (inputs: {', '.join(INPUTS + GPIO)})")
        self.t_us = max(self.t_us, int(t_us))
        self._evaluate()

    def _evaluate(self) -> None:
        reset = self.inputs["reset"]
        for latch, ok in (("imd", self.inputs["imd_ok"]), ("ams", self.gpio["ams_ok"])):
            if not ok:
                self.latch[latch] = False          # Q8/Q10 off: the coil and its hold drop
            elif reset:
                self.latch[latch] = True           # RST_EXT energises the coil; 11->14 holds it
        now = self.outputs()
        for k, v in now.items():
            if self._last.get(k) != v:
                self.history.append((self.t_us, k, v))
        self._last = now

    def in_series(self) -> dict:
        """Every series element, True = closed, in the traced order."""
        s = {
            "interlocks": self.inputs["interlocks_ok"],     # J4.5-J4.6, J2.4-J2.3 (untraced)
            "hv_interlock": self.inputs["hv_interlock"],    # J7.7-J7.8
            "imd_latch": self.latch["imd"],                 # K4
            "ams_latch": self.latch["ams"],                 # K5
            "bspd": self.inputs["bspd_ok"],                 # harness from here on
            "inertia": self.inputs["inertia_ok"],
            "bots": self.inputs["bots_ok"],
            "estop": self.inputs["estop_ok"],
        }
        if self.params["udv_k1_in_series"].value:
            s["udv_k1"] = self.gpio["udv_sdc"]
        s["tsms"] = self.inputs["tsms"]
        return s

    @property
    def closed(self) -> bool:
        return all(self.in_series().values())

    def outputs(self) -> dict:
        return {"sdc_closed": self.closed, "imd_latch": self.latch["imd"],
                "ams_latch": self.latch["ams"]}

    def first_open(self) -> Optional[str]:
        """The first open element in the traced order (None when closed)."""
        return next((k for k, v in self.in_series().items() if not v), None)


class SdcPlant:
    """The shutdown circuit on a system's co-simulation port (vhil/cosim.py),
    event-driven: stepped when AMS_OK (or the uDV's K1 drive) changes, when a
    scenario row sets an input (`apply`), and whenever the port's other
    plants are. Drives the gpio_in signal `sdc_closed` (AIR+_VCC as PF9
    senses it), which the tractive-system plant reads as its coil supply:
    list this plant before it."""

    event_driven = True

    def __init__(self, sdc: Optional[ShutdownCircuit] = None):
        self.sdc = sdc or ShutdownCircuit()
        self.started = False
        self.next_us: Optional[int] = None
        self.steps = 0
        self.driven: Optional[bool] = None
        self._recorded = 0

    @property
    def params(self) -> dict:
        return self.sdc.params

    def triggers(self, port) -> list[str]:
        return [n for n in GPIO if n in port.signals]

    def apply(self, values: dict, t_us: int) -> None:
        """A scenario's `plant` row: inputs, or parameters (an override)."""
        for name, value in values.items():
            if name in INPUTS:
                self.sdc.set(name, value, t_us)
            elif name in self.sdc.params:
                old = self.sdc.params[name]
                self.sdc.params = {**self.sdc.params, name: Parameter(
                    name, value, old.unit, "override",
                    f"set by the run (catalogue: {old.value!r})", None)}
                self.sdc.t_us = max(self.sdc.t_us, int(t_us))
                self.sdc._evaluate()
            else:
                raise KeyError(f"no input or parameter '{name}' (settable: "
                               f"{', '.join(sorted(settable()))})")

    def step(self, port, t_us: int) -> None:
        self.steps += 1
        changes = []
        for name in self.triggers(port):
            if not self.started:
                changes.append((t_us, name, port.level(name)))
                port.edges(name)            # history up to now: consumed
            else:
                changes += [(max(t, self.sdc.t_us), name, level) for t, level in port.edges(name)]
        if not self.started:
            # The circuit as the run starts, so a `plant:` expect has a value
            # before the first change.
            self.sdc.t_us = max(self.sdc.t_us, t_us)
            self.sdc.history += [(t_us, k, v) for k, v in self.sdc.outputs().items()]
        self.started = True
        for t, name, level in sorted(changes, key=lambda c: c[0]):
            self.sdc.set(name, level, t)
        closed = self.sdc.closed
        if closed != self.driven:
            port.set_level("sdc_closed", closed)
            self.driven = closed

    def records(self, plant: str, upto_us: int) -> list[dict]:
        """Trace records (docs/scenarios.md, `plant:` signals): every change
        of `sdc_closed` and of the two latches (`imd_latch`, `ams_latch`),
        each once."""
        out = []
        h = self.sdc.history
        while self._recorded < len(h) and h[self._recorded][0] <= upto_us:
            t, name, value = h[self._recorded]
            self._recorded += 1
            out.append({"kind": "plant", "t_us": t, "plant": plant, "name": name,
                        "value": int(value)})
        return out

    def state(self, t_us: int) -> dict:
        """The circuit now, for the state panel: AIR+_VCC, the latches and
        each series element (1 = closed)."""
        out = {k: int(v) for k, v in self.sdc.outputs().items()}
        out.update({f"sdc_{k}": int(v) for k, v in self.sdc.in_series().items()})
        return out


def settable(plant: str = PLANT) -> set[str]:
    return set(INPUTS) | set(load_parameters(plant))
