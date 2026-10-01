"""Drive a system in virtual time: the API native tests are written against.

    with Sim("systems/ecu.yaml", firmware={"ecu": "ECU08.elf"}) as sim:
        sim.run_for(ms=2000)
        hb = sim.can("can_acu").frames(0x100)
        assert_period(hb, period_us=10_000, tolerance_us=100)

Time only moves when a test says so (`run_for`, `run_until`). Between calls
the emulation is paused, every observation carries its virtual timestamp,
and stimulus can be scheduled at an exact virtual time. A test therefore
gets the same result on a slow laptop, a CI runner, or with Renode told to
go as fast as it can (`advance_immediately`, the default).
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from vhil.bench import DEFAULT_RENODE, REPO, _free_port
from vhil.renode import RenodeMonitor
from vhil.system import System

PROBE_SOURCE = REPO / "models" / "renode" / "VhilProbe.cs"


@dataclass(frozen=True)
class Frame:
    t_us: int
    id: int
    extended: bool
    data: bytes

    @property
    def t_ms(self) -> float:
        return self.t_us / 1000.0


@dataclass(frozen=True)
class Edge:
    t_us: int
    pin: str
    level: bool


def parse_frames(text: str) -> list[Frame]:
    out = []
    for line in text.splitlines():
        parts = line.split()   # a zero-length frame has no hex field
        if len(parts) in (3, 4) and parts[0].isdigit() and parts[1].startswith("0x"):
            out.append(Frame(int(parts[0]), int(parts[1], 16), parts[2] == "1",
                             bytes.fromhex(parts[3] if len(parts) == 4 else "")))
    return out


def parse_edges(text: str) -> list[Edge]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].isdigit():
            out.append(Edge(int(parts[0]), parts[1], parts[2] == "1"))
    return out


def _ids(ids) -> str:
    if ids is None:
        return ""
    if isinstance(ids, int):
        ids = [ids]
    return ",".join(f"0x{i:X}" for i in ids)


def _arg(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


class CanBus:
    """One bus of the system, seen through its VhilCanProbe."""

    def __init__(self, sim: "Sim", name: str):
        self.sim, self.name, self.probe = sim, name, f"vhil_probe_{name}"

    def frames(self, ids=None, since_us: int = 0) -> list[Frame]:
        return parse_frames(self.sim.monitor(f'{self.probe} Frames "{_ids(ids)}" {since_us}'))

    def last(self, can_id: int, since_us: int = 0) -> Optional[Frame]:
        frames = self.frames(can_id, since_us)
        return frames[-1] if frames else None

    def count(self, ids=None, since_us: int = 0) -> int:
        return int(self.sim.monitor(f'{self.probe} Count "{_ids(ids)}" {since_us}').strip(), 0)

    def send(self, can_id: int, data: bytes = b"", extended: bool = False) -> None:
        self.sim.monitor(f'{self.probe} Send {can_id} "{data.hex()}" {_arg(extended)}')

    def send_at(self, at_us: int, can_id: int, data: bytes = b"", extended: bool = False) -> None:
        self.sim.monitor(f'{self.probe} SendAt {at_us} {can_id} "{data.hex()}" {_arg(extended)}')

    def send_periodic(self, key: str, can_id: int, data: bytes, period_ms: float,
                      start_us: int = 0, extended: bool = False) -> None:
        self.sim.monitor(f'{self.probe} SendPeriodic "{key}" {can_id} "{data.hex()}" '
                         f'{int(period_ms * 1000)} {start_us} {_arg(extended)}')

    def update_periodic(self, key: str, data: bytes) -> None:
        self.sim.monitor(f'{self.probe} UpdatePeriodic "{key}" "{data.hex()}"')

    def stop_periodic(self, key: str) -> None:
        self.sim.monitor(f'{self.probe} StopPeriodic "{key}"')


class BoardIO:
    """GPIO of one board: drive inputs, watch outputs."""

    def __init__(self, sim: "Sim", board: str):
        self.sim, self.board, self.probe = sim, board, f"vhil_gpio_{board}"

    def set_input(self, port: str, pin: int, level: bool) -> None:
        self.sim.monitor(f"{port} OnGPIO {pin} {_arg(level)}", board=self.board)

    def watch(self, port: str, pin: int) -> str:
        self.sim.monitor(f'{self.probe} Watch "{port}" {pin}', board=self.board)
        return f"{port}:{pin}"

    def edges(self, pin: str = "", since_us: int = 0) -> list[Edge]:
        return parse_edges(self.sim.monitor(f'{self.probe} Edges "{pin}" {since_us}'))

    def level(self, pin: str) -> bool:
        return self.sim.monitor(f'{self.probe} Level "{pin}"').strip() == "True"


class Sim:
    def __init__(self, system: Path | str, firmware: dict[str, Path | str], *,
                 renode: str = DEFAULT_RENODE, advance_immediately: bool = True,
                 seed: int = 1, log_path: Path | None = None):
        self.system = System(Path(system))
        self.firmware = {b: Path(p).resolve() for b, p in firmware.items()}
        missing = set(self.system.boards) - set(self.firmware)
        if missing:
            raise ValueError(f"no firmware for boards {sorted(missing)}")
        self.renode, self.advance_immediately, self.seed = renode, advance_immediately, seed
        self.log_path = log_path
        self._proc = self._monitor = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "Sim":
        script = Path(tempfile.mkdtemp(prefix="vhil-sim-")) / f"{self.system.id}.resc"
        script.write_text(self.system.render_renode(self.firmware))
        port = _free_port()
        log = open(self.log_path, "w") if self.log_path else subprocess.DEVNULL
        self._proc = subprocess.Popen([self.renode, "--disable-gui", "--plain", "-P", str(port)],
                                      stdout=log, stderr=subprocess.STDOUT)
        self._monitor = RenodeMonitor(port, timeout_s=600)
        m = self._monitor
        m.execute(f"emulation SetSeed {self.seed}")
        m.execute(f"include @{PROBE_SOURCE.as_posix()}")
        m.execute(f"include @{script.as_posix()}")
        for bus in self.system.buses:
            m.execute(f'emulation CreateVhilCanProbe "vhil_probe_{bus}"')
            m.execute(f"connector Connect vhil_probe_{bus} {bus}")
        for board in self.system.boards:
            m.execute(f'emulation CreateVhilGpioProbe "vhil_gpio_{board}" "{board}"')
        m.execute(f"emulation SetGlobalAdvanceImmediately {_arg(self.advance_immediately)}")
        return self

    def stop(self) -> None:
        if self._monitor is not None:
            try:
                self._monitor.execute("quit")
            except Exception:
                pass
            self._monitor.close()
            self._monitor = None
        if self._proc is not None:
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def __enter__(self) -> "Sim":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- time --------------------------------------------------------------

    def now_us(self) -> int:
        info = self.monitor("emulation GetTimeSourceInfo")
        h, mnt, s = re.search(r"Elapsed Virtual Time: (\d+):(\d+):([\d.]+)", info).groups()
        return round((int(h) * 3600 + int(mnt) * 60 + float(s)) * 1_000_000)

    def run_for(self, ms: float = 0, us: int = 0) -> int:
        """Advance virtual time and pause again; returns the new time (us)."""
        total_us = int(ms * 1000) + us
        if total_us > 0:
            self.monitor(f'emulation RunFor "{total_us / 1e6:.6f}"')
        return self.now_us()

    def run_until(self, condition: Callable[[], bool], timeout_ms: float,
                  step_ms: float = 10) -> int:
        """Advance in steps until `condition()` holds; returns the time (us).
        Raises TimeoutError at the timeout, so a test never waits for ever."""
        deadline = self.now_us() + int(timeout_ms * 1000)
        while True:
            if condition():
                return self.now_us()
            if self.now_us() >= deadline:
                raise TimeoutError(f"condition not met within {timeout_ms} ms of virtual time")
            self.run_for(ms=step_ms)

    # -- access ------------------------------------------------------------

    def can(self, bus: str) -> CanBus:
        if bus not in self.system.buses:
            raise KeyError(f"no bus '{bus}' in {self.system.id} ({', '.join(self.system.buses)})")
        return CanBus(self, bus)

    def io(self, board: str) -> BoardIO:
        if board not in self.system.boards:
            raise KeyError(f"no board '{board}' in {self.system.id}")
        return BoardIO(self, board)

    def call(self, path: str, method: str, *args, board: Optional[str] = None) -> str:
        """Call a peripheral or model method, e.g.
        sim.call("sysbus.spi1.isospi.cells7", "SetCell", 2, 3480)."""
        return self.monitor(" ".join([path, method] + [_arg(a) for a in args]), board=board)

    def read_symbol(self, board: str, symbol: str, size: int = 1) -> int:
        """Read a firmware global by its linker symbol (1, 2 or 4 bytes)."""
        address = self.monitor(f'sysbus GetSymbolAddress "{symbol}"', board=board).strip()
        op = {1: "ReadByte", 2: "ReadWord", 4: "ReadDoubleWord"}[size]
        return int(self.monitor(f"sysbus {op} {address}", board=board).strip(), 16)

    def monitor(self, command: str, board: Optional[str] = None) -> str:
        if self._monitor is None:
            raise RuntimeError("Sim not started")
        if board is None and len(self.system.boards) == 1:
            board = next(iter(self.system.boards))
        if board is not None:
            self._monitor.execute(f'mach set "{board}"')
        return self._monitor.execute(command)


# -- assertions over observations ----------------------------------------------

def intervals_us(items: Iterable) -> list[int]:
    times = [i.t_us for i in items]
    return [b - a for a, b in zip(times, times[1:])]


def assert_period(items, period_us: int, tolerance_us: int, min_count: int = 3) -> None:
    """Every interval between consecutive items is period_us ± tolerance_us."""
    items = list(items)
    assert len(items) >= min_count, f"only {len(items)} items, need {min_count}"
    bad = [(i, d) for i, d in enumerate(intervals_us(items)) if abs(d - period_us) > tolerance_us]
    assert not bad, (f"{len(bad)} of {len(items) - 1} intervals outside {period_us} ± {tolerance_us} us; "
                     f"first: #{bad[0][0]} = {bad[0][1]} us")
