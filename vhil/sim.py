"""Drive a system in virtual time: the API native tests are written against.

    with Sim("systems/ecu.yaml", firmware={"ecu": "ECU08.elf",
                                           "ecu.bootloader": "CAN_BL.elf"}) as sim:
        t = sim.wait_for_app()          # the bootloader's 2 s window, then the app
        sim.run_for(ms=2000)
        hb = sim.can("can_acu").frames(0x100, since_us=t)
        assert_cadence(hb, period_us=10_000, jitter_us=1000)

Time only moves when a test says so (`run_for`, `run_until`). Between calls
the emulation is paused, every observation carries its virtual timestamp,
and stimulus can be scheduled at an exact virtual time. A test therefore
gets the same result on a slow laptop, a CI runner, or with Renode told to
go as fast as it can (`advance_immediately`, the default).

Instrumentation (tests/sim/conftest.py turns it on for every Sim of a run
through INSTRUMENT): `trace` keeps the last N translation blocks per CPU for
failure snapshots (vhil/snapshot.py; a C# hook on every block, 5-8x
slower), `coverage_dir` logs every translated block per CPU for
vhil/coverage.py (5-25% slower; a reset retranslates, and logs, everything).
"""
from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from vhil import elf
from vhil.bench import DEFAULT_RENODE, REPO, _free_port
from vhil.flash_image import APP_BASE
from vhil import renode as rn
from vhil.renode import RenodeMonitor
from vhil.system import System

PROBE_SOURCE = REPO / "models" / "renode" / "VhilProbe.cs"
TRACE_SOURCE = REPO / "models" / "renode" / "VhilTrace.cs"


@dataclass
class Instrumentation:
    """Defaults for every Sim that doesn't set its own (conftest options)."""
    trace: int = 0                       # blocks kept per CPU; 0 = off
    coverage_dir: Optional[Path] = None  # where translated-block logs go
    # Called with the Sim when a `with Sim(...)` block exits on an exception,
    # before Renode stops: the last chance to snapshot it (tests/sim/conftest.py).
    on_error_exit: Optional[Callable[["Sim"], None]] = None


INSTRUMENT = Instrumentation()
_live: list["Sim"] = []          # started and not stopped yet
_activity = itertools.count(1)   # bumped on every monitor command
_coverage_seq = itertools.count()


def live_sims() -> list["Sim"]:
    return list(_live)


def activity() -> int:
    """A mark to compare Sim.last_activity with: the Sims a test touched are
    the ones whose last_activity is above the mark taken when it started."""
    return next(_activity)


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
class BusFrame:
    """One frame the arbitrated bus decided (models/renode/VhilCanBus.cs,
    Timeline): times in ns of virtual time. outcome "ok" (sent and
    acknowledged; end = its last EOF bit), "ack" (nobody acknowledged it; end =
    the end of its error frame) or "lost" (lost arbitration with automatic
    retransmission disabled: cancelled; end = the end of the arbitration
    field). free: when the bus was idle again, after intermission."""
    start_ns: int
    end_ns: int
    free_ns: int
    offer_ns: int
    node: str
    id: int
    extended: bool
    remote: bool
    dlc: int
    outcome: str
    data: bytes

    @property
    def start_us(self) -> float:
        return self.start_ns / 1000.0

    @property
    def end_us(self) -> float:
        return self.end_ns / 1000.0


@dataclass(frozen=True)
class Edge:
    t_us: int
    pin: str
    level: bool


@dataclass(frozen=True)
class Payload:
    """One payload a radio sent (models/renode/Nrf24l01p.cs, Payloads): t_us
    when its packet ended and TX_DS was set."""
    t_us: int
    data: bytes


def parse_payloads(text: str) -> list[Payload]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            out.append(Payload(int(parts[0]), bytes.fromhex(parts[1])))
    return out


def parse_frames(text: str) -> list[Frame]:
    out = []
    for line in text.splitlines():
        parts = line.split()   # a zero-length frame has no hex field
        if len(parts) in (3, 4) and parts[0].isdigit() and parts[1].startswith("0x"):
            out.append(Frame(int(parts[0]), int(parts[1], 16), parts[2] == "1",
                             bytes.fromhex(parts[3] if len(parts) == 4 else "")))
    return out


def parse_timeline(text: str) -> list[BusFrame]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) in (10, 11) and parts[0].isdigit() and parts[5].startswith("0x"):
            out.append(BusFrame(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), parts[4],
                                int(parts[5], 16), parts[6] == "1", parts[7] == "1", int(parts[8]),
                                parts[9], bytes.fromhex(parts[10] if len(parts) == 11 else "")))
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
        return '""'
    if isinstance(ids, int):
        ids = [ids]
    return rn.quote(",".join(f"0x{_int(i):X}" for i in ids))


def _int(value) -> int:
    """An integer argument (a whole float too), refused as anything else: a
    bool, a string, a fraction."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise rn.UnsafeText(f"not an integer: {value!r}")
    return int(value)


def _hex(data) -> str:
    return rn.quote(bytes(data).hex())


def _arg(value) -> str:
    """One monitor argument: a bool, a number or a string literal; a string
    that could end its literal or the command is refused (vhil/renode.py)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return rn.quote(value)
    return rn.number(value)


class CanBus:
    """One bus of the system, seen through its VhilCanProbe. On an arbitrated
    bus (the default) the probe is a node like any other: what it sends
    waits for the bus and may lose arbitration, and it acknowledges frames
    unless set_ack(False) makes it listen only; timeline(), load(), stats()
    and nodes() read the bus model (models/renode/VhilCanBus.cs)."""

    def __init__(self, sim: "Sim", name: str, probe: Optional[str] = None):
        self.sim, self.name = sim, name
        self.probe = probe or f"vhil_probe_{rn.ident(name)}"

    @property
    def arbitrated(self) -> bool:
        return self.sim.system.arbitrated(self.name)

    def node(self, name: str) -> "CanBus":
        """Another probe on this bus, its own node (with its own transmit
        queue, ACK and error counter): for a test that needs two senders,
        e.g. sim.can("can_acu").node("logger").send(...). Created the first
        time it is asked for."""
        probe = f"vhil_probe_{rn.ident(self.name)}_{rn.ident(name)}"
        if probe not in self.sim._extra_probes:
            self.sim.monitor(f"emulation CreateVhilCanProbe {rn.quote(probe)}")
            self.sim.monitor(f"connector Connect {probe} {rn.ident(self.name)}")
            self.sim._extra_probes.add(probe)
        return CanBus(self.sim, self.name, probe)

    def _bus(self, command: str) -> str:
        if not self.arbitrated:
            raise ValueError(f"bus '{self.name}' has no arbitration (arbitration: false in "
                             f"{self.sim.system.path.name}, or Sim(hub=...))")
        return self.sim.monitor(f"{rn.ident(self.name)} {command}")

    def timeline(self, since_us: int = 0) -> list[BusFrame]:
        """Every frame the bus decided from since_us on, in start order."""
        return parse_timeline(self._bus(f"Timeline {_int(since_us)}"))

    def load(self, from_us: int, to_us: int) -> float:
        """Fraction of [from_us, to_us) the bus was busy (frames, error frames,
        intermission). Exact on an arbitrated bus."""
        return float(self._bus(f"Load {_int(from_us)} {_int(to_us)}").strip())

    def stats(self) -> dict:
        """frames, ack_errors, lost, busy_ns, late (effects a machine saw after
        their bus time: a frame that started and ended between two sync points
        of a multi-board bus), max_late_ns, dropped_records, eager,
        wake_retries (single-board wakes that found the bus taken and were
        retried 1 us later: host timing in the timeline; 0 since #209)."""
        parts = self._bus("Stats").split()
        return {k: int(v) for k, v in zip(parts[::2], parts[1::2])}

    def nodes(self) -> dict[str, dict]:
        """name -> kind, machine, tec, ack, sent, ack_errors, lost, bitrate."""
        out = {}
        for line in self._bus("Nodes").splitlines():
            p = line.split()
            if len(p) == 9:
                out[p[0]] = {"kind": p[1], "machine": p[2], "tec": int(p[3]), "ack": p[4] == "1",
                             "sent": int(p[5]), "ack_errors": int(p[6]), "lost": int(p[7]),
                             "bitrate": int(p[8])}
        return out

    def set_ack(self, ack: bool) -> None:
        """Whether this probe acknowledges frames (default: yes, as the rest
        of the car would). False: it only listens, like an adapter in
        listen-only mode, so a board alone on the bus gets no ACK."""
        self._bus(f"SetAck {rn.quote(self.probe)} {_arg(bool(ack))}")

    def frames(self, ids=None, since_us: int = 0) -> list[Frame]:
        return parse_frames(self.sim.monitor(f'{self.probe} Frames {_ids(ids)} {_int(since_us)}'))

    def sent(self, ids=None, since_us: int = 0) -> list[Frame]:
        """The frames this probe sent (send, send_at, send_periodic, ...),
        stamped when they went out. frames() and count() never include them:
        they are what the bus delivered to the probe."""
        return parse_frames(self.sim.monitor(f'{self.probe} Sent {_ids(ids)} {_int(since_us)}'))

    def last(self, can_id: int, since_us: int = 0) -> Optional[Frame]:
        frames = self.frames(can_id, since_us)
        return frames[-1] if frames else None

    def count(self, ids=None, since_us: int = 0) -> int:
        return int(self.sim.monitor(f'{self.probe} Count {_ids(ids)} {_int(since_us)}').strip(), 0)

    def send(self, can_id: int, data: bytes = b"", extended: bool = False) -> None:
        self.sim.monitor(f'{self.probe} Send {_int(can_id)} {_hex(data)} {_arg(bool(extended))}')

    def send_batch(self, frames) -> None:
        """Standard frames [(id, data), ...] now, in order, in one call."""
        items = " ".join(f"{_int(can_id)}:{bytes(data).hex()}" for can_id, data in frames)
        if items:
            self.sim.monitor(f'{self.probe} SendBatch {rn.quote(items)}')

    def send_sequence(self, frames, gap_us: int, burst: int = 1) -> None:
        """Standard frames [(id, data), ...] streamed from now at one per
        gap_us on average, `burst` frames every burst * gap_us (each burst is
        a synced action: a burst > 1 is much cheaper to emulate)."""
        items = " ".join(f"{_int(can_id)}:{bytes(data).hex()}" for can_id, data in frames)
        if items:
            self.sim.monitor(f'{self.probe} SendSequence {rn.quote(items)} {int(gap_us)} '
                             f'{int(burst)}')

    # Timing of everything the probe injects (send, send_at, send_periodic,
    # send_sequence): Renode runs it at a sync point, every time.quantum_s of
    # the system (500 us in systems/*.yaml), so a frame asked for at t goes
    # out at the first sync point at or after t (#130).
    def send_at(self, at_us: int, can_id: int, data: bytes = b"", extended: bool = False) -> None:
        self.sim.monitor(f'{self.probe} SendAt {_int(at_us)} {_int(can_id)} {_hex(data)} '
                         f'{_arg(bool(extended))}')

    def send_periodic(self, key: str, can_id: int, data: bytes, period_ms: float,
                      start_us: Optional[int] = None, extended: bool = False) -> None:
        """Send every period_ms from start_us (default: now). The probe's own
        "start 0 = now" reads its clock from the monitor thread, where it is
        0, so it would replay every period since power-on, one per sync
        quantum: now is passed explicitly."""
        key = rn.quote(key)                     # refused before anything is asked of Renode
        if start_us is None:
            start_us = self.sim.now_us()
        self.sim.monitor(f'{self.probe} SendPeriodic {key} {_int(can_id)} '
                         f'{_hex(data)} {int(period_ms * 1000)} {_int(start_us)} '
                         f'{_arg(bool(extended))}')

    def update_periodic(self, key: str, data: bytes) -> None:
        self.sim.monitor(f'{self.probe} UpdatePeriodic {rn.quote(key)} {_hex(data)}')

    def stop_periodic(self, key: str) -> None:
        self.sim.monitor(f'{self.probe} StopPeriodic {rn.quote(key)}')


class BoardIO:
    """GPIO of one board: drive inputs, watch outputs."""

    def __init__(self, sim: "Sim", board: str):
        self.sim, self.board, self.probe = sim, board, f"vhil_gpio_{rn.ident(board)}"

    def gpio(self, pin: str) -> tuple[str, int]:
        """A board pin's GPIO port and number, as the catalogue wires it (the
        board's `gpio`, or its role's re-kind of an analog input), e.g.
        gpio("PB6") -> ("sysbus.gpioPortB", 6). For watch and set_input:
        io.watch(*io.gpio("PB6")), io.set_input(*io.gpio("PF10"), True)."""
        _, kind, target = self.sim.system.resolve(f"{self.board}.{pin}")
        if kind != "gpio":
            raise ValueError(f"{self.board}.{pin} is {kind}, not a GPIO")
        return target["port"], int(target["pin"])

    def set_input(self, port: str, pin: int, level: bool) -> None:
        """Drive an input from outside the MCU, e.g. set_input("sysbus.gpioPortB",
        5, True). The level holds across the board's resets, as a switch or a
        pull-up on the board does (models/renode/VhilProbe.cs, Drive)."""
        self.sim.monitor(f'{self.probe} Drive {rn.quote(rn.path(port))} {_int(pin)} '
                         f'{_arg(bool(level))}', board=self.board)

    def set_voltage(self, pin: str, volts: float) -> None:
        """Drive an analog input pin of the board (catalogue analog_in), e.g.
        set_voltage("PF7", 1.65)."""
        _, kind, target = self.sim.system.resolve(f"{self.board}.{pin}")
        if kind != "analog":
            raise ValueError(f"{self.board}.{pin} is {kind}, not an analog input")
        self.sim.monitor(f"{rn.path(target['adc'])} SetVoltage {round(float(volts) * 1e6)} "
                         f"{_int(target['channel'])}", board=self.board)

    def watch(self, port: str, pin: int) -> str:
        self.sim.monitor(f'{self.probe} Watch {rn.quote(rn.path(port))} {_int(pin)}',
                         board=self.board)
        return f"{port}:{pin}"

    def edges(self, pin: str = "", since_us: int = 0) -> list[Edge]:
        return parse_edges(self.sim.monitor(f'{self.probe} Edges {rn.quote(pin)} {_int(since_us)}'))

    def level(self, pin: str) -> bool:
        return self.sim.monitor(f'{self.probe} Level {rn.quote(pin)}').strip() == "True"

    def sample(self, symbol: str, size: int, period_us: int, first_us: int, now_us: int) -> None:
        """Sample a firmware global (1, 2 or 4 bytes) at first_us and every
        period_us after it, inside the emulation (models/renode/VhilProbe.cs,
        Sample): what read_symbol would read with the run stopped at each of
        those times, without stopping it. now_us: the time the Sim is
        paused at; a sample due by then is taken now."""
        address, _ = elf.symbol(self.sim.firmware[self.board], symbol)
        self.sim.monitor(f'{self.probe} Sample {rn.quote(symbol)} {_int(address)} {_int(size)} '
                         f'{_int(period_us)} {_int(first_us)} {_int(now_us)}')

    def samples(self, now_us: int) -> list[tuple[int, str, int]]:
        """[(t_us, symbol, value)] sampled since the last call, up to now_us
        (the time the Sim is paused at)."""
        out = []
        for line in self.sim.monitor(f'{self.probe} Samples {_int(now_us)}').splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0].isdigit():
                out.append((int(parts[0]), parts[1], int(parts[2])))
        return out


class Radio:
    """A radio device of the system (a model with `interface.radio`, e.g. the
    ECU's nRF24L01+): what it sent. On its board's machine at
    sysbus.<device> (vhil.system, _renode_device)."""

    def __init__(self, sim: "Sim", device: str):
        self.sim, self.device = sim, device
        self.board = sim.system.board_of_device(device)
        self.path = f"sysbus.{rn.ident(device)}"

    def payloads(self, since_us: int = 0) -> list[Payload]:
        return parse_payloads(self.sim.call(self.path, "Payloads", _int(since_us), board=self.board))

    def tx_ds_cleared(self, since_us: int = 0) -> list[tuple[int, int]]:
        """[(set_us, cleared_us)] per TX_DS the firmware cleared."""
        out = []
        for line in self.sim.call(self.path, "TxDsCleared", _int(since_us), board=self.board).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                out.append((int(parts[0]), int(parts[1])))
        return out

    def count(self, what: str = "Transmitted") -> int:
        """Transmitted or ShortCePulses."""
        if what not in ("Transmitted", "ShortCePulses"):
            raise ValueError(f"no count {what!r}")
        return int(self.sim.call(self.path, what, board=self.board).strip(), 0)


class Sim:
    def __init__(self, system: Path | str, firmware: dict[str, Path | str], *,
                 renode: str = DEFAULT_RENODE, advance_immediately: bool = True,
                 seed: int = 1, log_path: Path | None = None,
                 params: dict[str, dict] | None = None,
                 write_protect: dict[str, list[int]] | None = None,
                 trace: Optional[int] = None, coverage_dir: Optional[Path] = None,
                 card_dirs: Iterable[Path | str] = (),
                 arbitration: Iterable[str] = (), hub: Iterable[str] = ()):
        """params overrides device params for this run, e.g.
        {"sd": {"image": "card.img"}}, checked as the system file's are;
        write_protect a board's write-protected flash sectors at power-on, as
        the system file's write_protect, e.g. {"ecu": [0]}. card_dirs adds
        directories an sd-card image may come from, besides the configured
        card-image directory (vhil.system.card_dirs), e.g. a test's tmp_path.
        Every bus is modelled in virtual time (#174) unless the system file
        says `arbitration: false`; arbitration names buses to model so for
        this run all the same, hub buses to leave to Renode's hub, e.g.
        ["can_acu"]. trace and coverage_dir default to INSTRUMENT's."""
        self.system = System(Path(system), extra_card_dirs=card_dirs)
        for name, sectors in (write_protect or {}).items():
            if name not in self.system.boards:
                raise ValueError(f"write_protect: no board '{name}' in {self.system.id}")
            self.system.boards[name].write_protect = list(sectors)
        for bus in arbitration:
            if bus not in self.system.buses:
                raise ValueError(f"arbitration: no bus '{bus}' in {self.system.id}")
            self.system.buses[bus]["arbitration"] = True
        for bus in hub:
            if bus not in self.system.buses:
                raise ValueError(f"hub: no bus '{bus}' in {self.system.id}")
            self.system.buses[bus]["arbitration"] = False
        self.system._check()
        for name, values in (params or {}).items():
            if name not in self.system.devices:
                raise ValueError(f"params: no device '{name}' in {self.system.id}")
            self.system.set_params(name, values)
        self.firmware = {b: Path(p).resolve() for b, p in firmware.items()}
        missing = set(self.system.images()) - set(self.firmware)
        if missing:
            raise ValueError(f"no firmware for boards {sorted(missing)}")
        self.renode, self.advance_immediately, self.seed = renode, advance_immediately, seed
        self.log_path = log_path
        self.trace_blocks = INSTRUMENT.trace if trace is None else trace
        self.coverage_dir = INSTRUMENT.coverage_dir if coverage_dir is None else coverage_dir
        self.coverage_logs: dict[str, Path] = {}
        self.last_activity = 0
        self.app_started: dict[str, int] = {}   # board -> us its app started (wait_for_app)
        self._mach: Optional[str] = None
        self._extra_probes: set[str] = set()     # CanBus.node()
        self._proc = self._monitor = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "Sim":
        script = Path(tempfile.mkdtemp(prefix="vhil-sim-")) / f"{self.system.id}.resc"
        script.write_text(self.system.render_renode(self.firmware))
        port = _free_port()
        log = open(self.log_path, "w") if self.log_path else subprocess.DEVNULL
        # The monitor listens on 127.0.0.1 only (vhil/renode.py, launch).
        self._proc = rn.launch(self.renode, port, stdout=log)
        self._monitor = RenodeMonitor(port, timeout_s=600)
        m = self._monitor
        m.execute(f"emulation SetSeed {_int(self.seed)}")
        m.execute(f"include {rn.file_arg(PROBE_SOURCE)}")
        m.execute(f"include {rn.file_arg(script)}")
        for bus in self.system.buses:
            probe = f"vhil_probe_{rn.ident(bus)}"
            m.execute(f"emulation CreateVhilCanProbe {rn.quote(probe)}")
            m.execute(f"connector Connect {probe} {bus}")
        for board in self.system.boards:
            m.execute(f"emulation CreateVhilGpioProbe {rn.quote('vhil_gpio_' + rn.ident(board))} "
                      f"{rn.quote(board)}")
        m.execute(f"emulation SetGlobalAdvanceImmediately {_arg(bool(self.advance_immediately))}")
        if self.trace_blocks:
            m.execute(f"include {rn.file_arg(TRACE_SOURCE)}")
            for board in self.system.boards:
                m.execute(f"emulation CreateVhilTrace {rn.quote('vhil_trace_' + rn.ident(board))} "
                          f"{rn.quote(board)} {_int(self.trace_blocks)}")
        if self.coverage_dir:
            self._start_coverage(Path(self.coverage_dir))
        _live.append(self)
        return self

    def _start_coverage(self, root: Path) -> None:
        """Renode logs every block it translates, with its disassembly, to the
        CPU's LogFile. A block is translated the first time it runs, so the
        addresses in the log are the code that executed (vhil/coverage.py)."""
        raw = root / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        stem = f"{self.system.id}-{os.getpid()}-{next(_coverage_seq)}"
        for board in self.system.boards:
            log = (raw / f"{stem}-{board}.tblog").resolve()
            self.monitor(f"cpu LogFile {rn.file_arg(log)}", board=board)
            self.monitor("cpu LogTranslatedBlocks true", board=board)
            self.coverage_logs[board] = log
            (raw / f"{stem}-{board}.json").write_text(json.dumps(
                {"system": self.system.id, "board": board, "log": log.name,
                 "images": [str(p) for p in self.images_of(board)]}, indent=1))

    def images_of(self, board: str) -> list[Path]:
        """The ELF images a board's CPU runs: its app, then its bootloader."""
        return [p for p in (self.firmware.get(board), self.firmware.get(f"{board}.bootloader"))
                if p is not None]

    def stop(self) -> None:
        if self in _live:
            _live.remove(self)
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

    def __exit__(self, exc_type, *exc) -> None:
        # An error or a failed assertion, not a skip or a KeyboardInterrupt
        # (pytest's skip and fail are BaseExceptions; fail is "Failed").
        failed = exc_type is not None and (issubclass(exc_type, Exception)
                                           or exc_type.__name__ == "Failed")
        if failed and INSTRUMENT.on_error_exit is not None:
            try:
                INSTRUMENT.on_error_exit(self)
            except Exception:
                pass   # never mask the exception that is on its way out
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

    def in_app(self, board: str) -> bool:
        """Whether a board runs its application: its PC at or past where the
        app's vector table is linked (0x08020000 behind the CAN bootloader,
        which owns sector 0)."""
        b = self.system.boards[board]
        if b.bootloader is None:
            return True
        base = b.firmware.get("load", {}).get("vector_table", APP_BASE)
        return int(self.monitor(f"{rn.ident(b.platform['renode'].get('cpu', 'cpu'))} PC",
                                board=board).strip(), 16) >= base

    def wait_for_app(self, board: Optional[str] = None, timeout_ms: float = 4000,
                     step_ms: float = 10) -> int:
        """Run until `board` (default: every board) runs its application, as
        after a power-on its bootloader jumps to it at the end of its 2 s
        auto-jump window (stm32-can-bootloader ARCHITECTURE.md "Boot flow").
        Polls the PC every step_ms; TimeoutError if a bootloader stays, e.g.
        on a bad image or a boot request.

        Returns when the (last) application started, in us, and records each
        board's in `app_started`: to the ms from the app's HAL tick (uwTick,
        ms since its HAL_Init; stm32h7xx_hal.c), else the poll time."""
        boards = [board] if board is not None else list(self.system.boards)
        self.run_until(lambda: all(self.in_app(b) for b in boards), timeout_ms, step_ms)
        now = self.now_us()
        for b in boards:
            try:
                self.app_started[b] = now - 1000 * self.read_symbol(b, "uwTick", 4)
            except KeyError:
                self.app_started[b] = now
        return max(self.app_started[b] for b in boards)

    # -- access ------------------------------------------------------------

    def can(self, bus: str) -> CanBus:
        if bus not in self.system.buses:
            raise KeyError(f"no bus '{bus}' in {self.system.id} ({', '.join(self.system.buses)})")
        return CanBus(self, bus)

    def io(self, board: str) -> BoardIO:
        if board not in self.system.boards:
            raise KeyError(f"no board '{board}' in {self.system.id}")
        return BoardIO(self, board)

    def radio(self, device: str) -> Radio:
        if device not in dict(self.system.radios()):
            raise KeyError(f"no radio '{device}' in {self.system.id}")
        return Radio(self, device)

    def call(self, path: str, method: str, *args, board: Optional[str] = None) -> str:
        """Call a peripheral or model method, e.g.
        sim.call("sysbus.spi1.isospi.cells7", "SetCell", 2, 3480)."""
        return self.monitor(" ".join([rn.path(path), rn.ident(method)] + [_arg(a) for a in args]),
                            board=board)

    def power_cycle(self, board: str) -> None:
        """Cut and restore a board's power, as the virtual broker's relay does
        (vhil.broker.power_on_commands): a cold boot, the backup domain wiped
        when the board has no VBAT."""
        from vhil.broker import power_on_commands
        vbat = self.system.boards[board].board.get("vbat", True)
        for command in power_on_commands(board, vbat):
            self.monitor(command, board=board)

    def read_symbol(self, board: str, symbol: str, size: int = 1) -> int:
        """Read a firmware global by its linker symbol (1, 2 or 4 bytes). The
        address comes from the board's ELF, not Renode's lookup (vhil/elf.py)."""
        address, _ = elf.symbol(self.firmware[board], symbol)
        op = {1: "ReadByte", 2: "ReadWord", 4: "ReadDoubleWord"}[size]
        return int(self.monitor(f"sysbus {op} {_int(address):#x}", board=board).strip(), 16)

    def monitor(self, command: str, board: Optional[str] = None) -> str:
        if self._monitor is None:
            raise RuntimeError("Sim not started")
        if board is None and len(self.system.boards) == 1:
            board = next(iter(self.system.boards))
        self.last_activity = next(_activity)
        # Select the board's machine only when it isn't selected already: a
        # `mach set` is a round trip as dear as the command.
        if board is not None and board != self._mach:
            self._monitor.execute(f"mach set {rn.quote(rn.ident(board))}")
            self._mach = board
        if command.startswith(("mach ", "include ")):
            self._mach = None    # may select another machine
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


def assert_cadence(items, period_us: int, jitter_us: int, min_count: int = 3) -> None:
    """The items keep a fixed cadence: each is within jitter_us of one grid
    of period_us, so none is missing or doubled and the period does not
    drift, however long the run. What a task woken by osDelayUntil (exact
    kernel ticks, no drift) guarantees for a frame it posts on a bus that
    times frames: the wake is on the grid, and the compute before the post,
    the wait for the bus and the frame's stuff bits are jitter."""
    items = list(items)
    assert len(items) >= min_count, f"only {len(items)} items, need {min_count}"
    offsets = [i.t_us - items[0].t_us - k * period_us for k, i in enumerate(items)]
    lo, hi = min(offsets), max(offsets)
    assert hi - lo <= jitter_us, (
        f"{len(items)} items off a {period_us} us grid by {lo}..{hi} us, more than {jitter_us} us "
        f"of jitter; first at #{next(k for k, o in enumerate(offsets) if o in (lo, hi))}")
