"""An FMI 3.0 co-simulation master (M8, #147; docs/m8-plan.md): loads an FMU
and runs it as a plant on a system's co-simulation port, in lock-step with
the emulation's virtual time, deterministically.

It exists for IFSSIM's electrical FMU (isc-fs/IFSSIM#669), which replaces
the Python stand-in (vhil/plant_ts.py) on the same contract: the FMU's
variables carry the contract's names (`air_pos_cmd` ... `v_link`), so the
master maps them by name, not by a table of its own.

    fmu = Fmu("ts.fmu")                         # unpacked, its binary loaded
    plant = FmuPlant(fmu, cells="isospi")       # on the port, like TsPlant
    port.run([plant], ms=5000)

Only the C API of the standard is used, through ctypes: no dependency. The
binary for this host (binaries/<arch>-linux/<modelIdentifier>.so, FMI 3.0
platform tuples) must be in the FMU.

Timing. The FMU steps on a fixed communication grid (`h`, its
DefaultExperiment stepSize, else 5 ms), from the time the plant first steps;
inputs are held over a step, and a change takes effect at the first grid
point at or after it. The plant is event-driven (vhil/cosim.py): to hand
the emulation the coming 0x466 reports, the master steps the FMU ahead of
the emulation, so a coil edge can arrive for a time the FMU has passed. It
then goes back: to the FMU state it saved at its last exchange
(fmi3GetFMUState/SetFMUState, when the FMU says canGetAndSetFMUState), else
by fmi3Reset and a replay of every input change from the start (IFSSIM#669
§2: same inputs, same outputs). Either way the result is what an FMU stepped
in lock-step with the inputs would give: an input change is never applied
late or early.
"""
from __future__ import annotations

import ctypes
import os
import platform
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from vhil.plant_ts import PortInputs, inverter_report, load_parameters, vdc_frame

OK, WARNING = 0, 1

_LOG = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)
_UPDATE = ctypes.CFUNCTYPE(None)


class FmiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Variable:
    name: str
    vr: int
    type: str           # Float64 | Boolean | Int32 | String
    causality: str      # parameter | input | output | local | ...
    start: Optional[str]


def platform_tuple() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine, machine)
    return f"{arch}-linux"


class Fmu:
    """An FMI 3.0 co-simulation FMU: unpacked (a .fmu archive into a
    temporary directory, or a directory as is), described, its binary
    loaded. One instance at a time (instantiate)."""

    TYPES = {"Float64": ctypes.c_double, "Boolean": ctypes.c_bool, "Int32": ctypes.c_int32}
    KNOWN = ("Float64", "Boolean", "Int32", "String")

    def __init__(self, path: Path | str, *, can_get_set_state: Optional[bool] = None):
        path = Path(path)
        if path.is_dir():
            self.root = path
        else:
            self.root = Path(tempfile.mkdtemp(prefix="vhil-fmu-"))
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    target = (self.root / member).resolve()
                    if self.root.resolve() not in target.parents and target != self.root.resolve():
                        raise FmiError(f"{path}: archive member outside the FMU: {member}")
                z.extractall(self.root)
        md = ET.parse(self.root / "modelDescription.xml").getroot()
        if md.get("fmiVersion", "").split(".")[0] != "3":
            raise FmiError(f"{path}: FMI {md.get('fmiVersion')}, not 3.0")
        cs = md.find("CoSimulation")
        if cs is None:
            raise FmiError(f"{path}: not a co-simulation FMU")
        self.token = md.get("instantiationToken")
        self.identifier = cs.get("modelIdentifier")
        self.can_get_set_state = (cs.get("canGetAndSetFMUState") == "true"
                                  if can_get_set_state is None else can_get_set_state)
        exp = md.find("DefaultExperiment")
        self.default_step = float(exp.get("stepSize")) if exp is not None and exp.get("stepSize") else None
        self.variables: dict[str, Variable] = {}
        for v in md.find("ModelVariables"):
            if v.tag not in self.KNOWN:
                raise FmiError(f"{path}: variable {v.get('name')} of type {v.tag}, which the "
                               f"master doesn't handle ({', '.join(self.KNOWN)})")
            self.variables[v.get("name")] = Variable(v.get("name"), int(v.get("valueReference")),
                                                     v.tag, v.get("causality", "local"), v.get("start"))
        binary = self.root / "binaries" / platform_tuple() / f"{self.identifier}.so"
        if not binary.is_file():
            raise FmiError(f"{path}: no binary for {platform_tuple()} ({binary.relative_to(self.root)})")
        self.lib = ctypes.CDLL(str(binary))
        self._bind()
        self.instance = None
        self.messages: list[str] = []
        self._log = _LOG(lambda env, status, category, message: self.messages.append(
            (message or b"").decode(errors="replace")))
        self._update = _UPDATE(0)

    def _bind(self):
        L, P, B, D, S, VR = (self.lib, ctypes.c_void_p, ctypes.c_bool, ctypes.c_double,
                             ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32))
        L.fmi3InstantiateCoSimulation.restype = P
        L.fmi3InstantiateCoSimulation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                                  B, B, B, B, VR, S, P, _LOG, _UPDATE]
        L.fmi3EnterInitializationMode.argtypes = [P, B, D, D, B, D]
        L.fmi3ExitInitializationMode.argtypes = [P]
        L.fmi3DoStep.argtypes = [P, D, D, B, ctypes.POINTER(B), ctypes.POINTER(B), ctypes.POINTER(B),
                                 ctypes.POINTER(D)]
        # The getters and setters of the types the FMU's variables use (an
        # FMU must export every fmi3 function, but only these are called).
        for t in sorted({v.type for v in self.variables.values()}):
            for op in ("Set", "Get"):
                fn = getattr(L, f"fmi3{op}{t}")
                fn.restype = ctypes.c_int
                fn.argtypes = [P, VR, S, (ctypes.POINTER(ctypes.c_char_p) if t == "String"
                                          else ctypes.POINTER(self.TYPES[t])), S]
        for name in ("fmi3Reset", "fmi3Terminate"):
            getattr(L, name).argtypes = [P]
        L.fmi3FreeInstance.argtypes = [P]
        L.fmi3FreeInstance.restype = None
        if self.can_get_set_state:
            for fn in ("fmi3GetFMUState", "fmi3SetFMUState", "fmi3FreeFMUState"):
                getattr(L, fn).restype = ctypes.c_int
            L.fmi3GetFMUState.argtypes = [P, ctypes.POINTER(P)]
            L.fmi3SetFMUState.argtypes = [P, P]
            L.fmi3FreeFMUState.argtypes = [P, ctypes.POINTER(P)]
        for fn in ("fmi3EnterInitializationMode", "fmi3ExitInitializationMode", "fmi3DoStep",
                   "fmi3Reset", "fmi3Terminate"):
            getattr(L, fn).restype = ctypes.c_int

    def _check(self, status: int, what: str) -> None:
        if status > WARNING:
            raise FmiError(f"{self.identifier}: {what} returned {status}: {self.messages[-3:]}")

    # -- lifecycle ---------------------------------------------------------------

    def instantiate(self, name: str = "vhil") -> None:
        resources = (self.root / "resources").as_uri() + "/"
        self.instance = self.lib.fmi3InstantiateCoSimulation(
            name.encode(), (self.token or "").encode(), resources.encode(), False, False, False,
            False, None, 0, None, self._log, self._update)
        if not self.instance:
            raise FmiError(f"{self.identifier}: fmi3InstantiateCoSimulation failed: {self.messages}")

    def initialize(self, start_s: float = 0.0) -> None:
        self._check(self.lib.fmi3EnterInitializationMode(self.instance, False, 0.0, start_s, False, 0.0),
                    "fmi3EnterInitializationMode")
        self._check(self.lib.fmi3ExitInitializationMode(self.instance), "fmi3ExitInitializationMode")

    def reset(self) -> None:
        self._check(self.lib.fmi3Reset(self.instance), "fmi3Reset")

    def free(self) -> None:
        if self.instance:
            self.lib.fmi3Terminate(self.instance)
            self.lib.fmi3FreeInstance(self.instance)
            self.instance = None

    def do_step(self, t_s: float, h_s: float) -> None:
        flags = [ctypes.c_bool() for _ in range(3)]
        last = ctypes.c_double()
        self._check(self.lib.fmi3DoStep(self.instance, t_s, h_s, False, *[ctypes.byref(f) for f in flags],
                                        ctypes.byref(last)), "fmi3DoStep")
        if flags[1].value:
            raise FmiError(f"{self.identifier}: the FMU asked to terminate at {t_s + h_s} s")

    # -- variables ---------------------------------------------------------------

    def set(self, name: str, value) -> None:
        v = self.variables[name]
        vr = (ctypes.c_uint32 * 1)(v.vr)
        if v.type == "String":
            arr = (ctypes.c_char_p * 1)(str(value).encode())
            self._check(self.lib.fmi3SetString(self.instance, vr, 1, arr, 1), f"set {name}")
            return
        ctype = self.TYPES[v.type]
        arr = (ctype * 1)(value)
        self._check(getattr(self.lib, f"fmi3Set{v.type}")(self.instance, vr, 1, arr, 1), f"set {name}")

    def get(self, name: str):
        v = self.variables[name]
        ctype = self.TYPES[v.type]
        arr = (ctype * 1)()
        self._check(getattr(self.lib, f"fmi3Get{v.type}")(self.instance, (ctypes.c_uint32 * 1)(v.vr), 1,
                                                          arr, 1), f"get {name}")
        return arr[0]

    # -- state ---------------------------------------------------------------------

    def get_state(self, state=None):
        handle = ctypes.c_void_p(state)
        self._check(self.lib.fmi3GetFMUState(self.instance, ctypes.byref(handle)), "fmi3GetFMUState")
        return handle.value

    def set_state(self, state) -> None:
        self._check(self.lib.fmi3SetFMUState(self.instance, state), "fmi3SetFMUState")


class FmuPlant(PortInputs):
    """An FMU on a system's co-simulation port, as TsPlant runs the stand-in:
    the same inputs (PortInputs: coil edges at their own times, the coil
    supply, v_pack from the LTC6811 chain when the FMU takes it as an input),
    the FMU's `v_link` reported on 0x466 by the inverter (vhil/plant_ts.py
    inverter_report, with the catalogue's vdc_period, resolution and sense
    floor). Parameters: the catalogue's (`params`, vhil.plant_ts
    load_parameters) set by name on the FMU's parameters that carry them, a
    null as 0 (an absent element: no resistor).

    Event-driven: at each step it brings the FMU to the step's time (back
    first, if an input changed for a time the FMU has passed), saves it there,
    then steps it ahead to forecast the reports for `horizon_ms`, and asks to
    be stepped again at the forecast's end. A forecast that never changed
    doubles the next one, up to `max_horizon_ms`."""

    def __init__(self, fmu: Fmu, *, params: Optional[dict] = None, cells: Optional[str] = None,
                 v_pack: Optional[float] = None, report: Optional[str] = "v_link",
                 step_s: Optional[float] = None,
                 horizon_ms: float = 200, max_horizon_ms: float = 5000):
        self.fmu = fmu
        self.params = params or load_parameters()
        super().__init__(cells, self.params["cells_per_chip"].value, v_pack)
        self.report = report
        self.h_us = int(round((step_s or fmu.default_step or 0.005) * 1e6))
        self.base_horizon_us = self.horizon_us = int(horizon_ms * 1000)
        self.max_horizon_us = int(max_horizon_ms * 1000)
        self.origin: Optional[int] = None    # emulation time (us) of the FMU's t = 0
        self.t_f = 0                         # the FMU's time, us from origin
        self.inputs: dict = {}               # held now, by name
        self.log: list = []                  # (grid us, name, value): every input change, in order
        self.applied = 0                     # how many of the log's the FMU has applied
        self.checkpoint = None               # (t_f, state or None, inputs, applied)
        self.rollbacks = 0
        self.outputs: list = []              # (emulation us, v_link) from the FMU's last sample on

    def _grid_at_or_after(self, t_us: int) -> int:
        rel = t_us - self.origin
        return -(-rel // self.h_us) * self.h_us

    def _init_fmu(self) -> None:
        f = self.fmu
        for name, p in self.params.items():
            v = f.variables.get(name)
            if v is not None and v.causality == "parameter":
                f.set(name, 0.0 if p.value is None else p.value)
        f.initialize(0.0)
        self.t_f, self.inputs, self.applied = 0, {}, 0
        self.outputs = [(self.origin, f.get("v_link"))]

    def _apply(self, name: str, value) -> None:
        if name in self.fmu.variables and self.fmu.variables[name].causality == "input":
            self.fmu.set(name, value)
        self.inputs[name] = value

    def _step_to(self, t_f: int, upto: Optional[int] = None) -> None:
        """Step the FMU to t_f (a grid point), applying the log's changes
        (from the first not applied, up to index `upto`) at their grid
        points."""
        upto = len(self.log) if upto is None else upto
        while True:
            while self.applied < upto and self.log[self.applied][0] <= self.t_f:
                _, name, value = self.log[self.applied]
                self._apply(name, value)
                self.applied += 1
            if self.t_f >= t_f:
                return
            self.fmu.do_step(self.t_f / 1e6, self.h_us / 1e6)
            self.t_f += self.h_us
            self.outputs.append((self.origin + self.t_f, self.fmu.get("v_link")))

    def _rollback(self) -> None:
        """Bring the FMU back to its checkpoint: an earlier time, with the
        inputs applied then."""
        self.rollbacks += 1
        t_ck, state, inputs, applied = self.checkpoint
        if state is not None:
            self.fmu.set_state(state)
            self.t_f, self.applied = t_ck, applied
            for name, value in inputs.items():
                self._apply(name, value)
            self.outputs = [(self.origin + t_ck, self.fmu.get("v_link"))]
            return
        # No state: back to t = 0, and the same inputs again.
        self.fmu.reset()
        self._init_fmu()
        self._step_to(t_ck, upto=applied)
        self.outputs = self.outputs[-1:]

    def _save(self) -> None:
        state = None
        if self.fmu.can_get_set_state:
            state = self.fmu.get_state(self.checkpoint[1] if self.checkpoint else None)
        self.checkpoint = (self.t_f, state, dict(self.inputs), self.applied)

    def step(self, port, t_us: int) -> None:
        self.steps += 1
        if self.origin is None:
            self.origin = t_us
            self.fmu.instantiate()
            self._init_fmu()
        changes = [(self._grid_at_or_after(t), name, value)
                   for t, name, value in self.changes(port, t_us, self.origin)]
        now_f = (t_us - self.origin) // self.h_us * self.h_us       # last grid point <= now
        if not changes and self.next_us is not None and t_us < self.next_us:
            return                              # the forecast stands: nothing new
        if changes and changes[0][0] < self.t_f:
            self._rollback()                    # the FMU ran past an input change
        self.log += changes
        self._step_to(max(now_f, self.t_f))
        self._save()
        if self.report is None:
            self.next_us = None
            return
        period_us = int(round(float(self.params["vdc_period"].value) * 1e6))
        first, period_us = self.first_tick(port, self.report, t_us, period_us)
        end = first + self.horizon_us
        rel_end = end - self.origin
        self._step_to(rel_end - rel_end % self.h_us)
        entries, last, held, k = [], object(), self.outputs[0][1], 0
        t = first
        while t <= end:
            while k < len(self.outputs) and self.outputs[k][0] <= t:
                held = self.outputs[k][1]
                k += 1
            r = inverter_report(held, self.params)
            if r != last:
                entries.append((t, vdc_frame(r)))
                last = r
            t += period_us
        port.schedule(self.report, entries, period_ms=period_us / 1000)
        quiet = len(entries) == 1
        self.horizon_us = min(self.horizon_us * 2, self.max_horizon_us) if quiet else self.base_horizon_us
        self.next_us = end
        self.outputs = self.outputs[-1:]

    def report(self) -> Optional[int]:
        """The inverter's reading of the FMU's link now (Inverter(dc_link=...),
        with report=None)."""
        return inverter_report(self.fmu.get("v_link"), self.params)


def build_reference_fmu(out: Path, *, can_get_set_state: bool = True,
                        cc: str = "cc") -> Path:
    """Compile tests/fmu/ts_reference.c with the host's C compiler and pack
    it as an FMU at `out` (a .fmu): the reference the master's tests run
    (docs/m8-plan.md). Raises FileNotFoundError without a compiler."""
    import shutil
    import subprocess
    src = Path(__file__).resolve().parent.parent / "tests" / "fmu"
    if shutil.which(cc) is None:
        raise FileNotFoundError(f"no C compiler ({cc})")
    work = Path(tempfile.mkdtemp(prefix="vhil-fmu-build-"))
    binary = work / "binaries" / platform_tuple() / "ts_reference.so"
    binary.parent.mkdir(parents=True)
    subprocess.run([cc, "-shared", "-fPIC", "-O2", "-fvisibility=hidden", "-o", str(binary),
                    str(src / "ts_reference.c"), "-lm"], check=True)
    (work / "resources").mkdir()
    md = (src / "modelDescription.xml").read_text().replace(
        "@STATE@", "true" if can_get_set_state else "false")
    (work / "modelDescription.xml").write_text(md)
    out = Path(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(work.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(work).as_posix())
    return out
