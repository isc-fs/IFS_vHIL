"""The web app's run worker (M5.2, #114; docs/architecture/m5-web-app.md).

    python -m vhil.worker [--once] [--poll S] [--fw-dir DIR] [--no-build]

Claims queued runs from the server's SQLite table (vhil/server/runs.py) and
executes their scenario with the same code CI runs: a `run` scenario drives
`vhil.sim.Sim` directly, a `pytest` scenario runs pytest in a subprocess.

A `run` advances virtual time in slices (slice_ms, default 100 ms). After
each slice the frames, GPIO edges and samples it produced are appended to
`<results>/<id>/trace.jsonl` in virtual-time order and flushed, so the API's
live WebSocket streams them while the run goes on; then the run's DB state is
read, and a run cancelled from the API stops there. Each slice also writes a
`bus_load` record per CAN bus (#174): the fraction of the slice the bus was
busy, exact on a bus with `arbitration: true` (models/renode/VhilCanBus.cs),
else estimated from the frames seen at 500 kbit/s (vhil/canframe.py,
`exact: false`); the summary carries each bus's mean and peak.

Firmware: each image key of the system ("<board>", "<board>.bootloader")
needs an ELF at the ref the run asks for (default: the catalogue's). An image
that `vhil.system build` already produced at that ref, listed in
`<fw-dir>/built.txt` (what `scripts/vhil-docker.sh fw` writes), is reused;
otherwise the worker runs `python -m vhil.system build --workdir <fw-dir>`
for the system, as CI does, and appends to built.txt. Reuse matches the ref
by name, not commit: a branch that moved since its last build runs the old
image until someone rebuilds it (`scripts/vhil-docker.sh fw`). --no-build
makes a missing image an error instead.

Liveness: while it holds a run the worker beats the run's heartbeat from a
background thread (every --heartbeat S) and at every slice, and before each
claim it reclaims runs whose heartbeat went stale (--reclaim-after S): a
worker that died mid-run leaves a run another worker picks up again, at most
MAX_ATTEMPTS times in all (vhil/server/runs.py). A worker that finds its run
reclaimed from under it (it stalled past the timeout) drops it without
writing a final state. A run's next attempt starts a fresh trace; the last
one is kept as trace.attempt<N>.jsonl. A first attempt that finds its results
directory already there (a database restored to a snapshot hands out the ids
of runs it rolled back, whose results stay on the runs volume) moves it to
`<results>/.orphaned/<id>-<time>` instead of appending to it. Every trace
starts with a header carrying the run's token (vhil/server/runs.py,
trace_header): until it is there, the API serves nothing at the run's path, so
a client that opens a queued run never sees the stale trace it replaces.
"""
from __future__ import annotations

import argparse
import fcntl
import heapq
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

from vhil import canframe
from vhil.server.config import Settings
from vhil.server.runs import (HEARTBEAT_S, MAX_ATTEMPTS, RECLAIM_AFTER_S, TRACE, Limits,
                              RunStore, code_changes, materialise_system, trace_header)
from vhil.server.workspace import Workspace
from vhil.system import System, built_images, image_path

DEFAULT_FW_DIR = Path(os.environ.get("VHIL_FW_DIR", "/vhil/fw"))


class Cancelled(Exception):
    """The run was cancelled; .summary is what it had done by then."""

    def __init__(self, summary: Optional[dict] = None):
        super().__init__("cancelled")
        self.summary = summary or {}


class Lost(Exception):
    """The run was reclaimed from this worker; its new holder finishes it."""


class Heartbeat:
    """Beats a held run's heartbeat every `period_s` from a daemon thread,
    so phases with no slices (a firmware build, a pytest subprocess, Renode
    starting) keep the run claimed. It stops beating once the store says the
    run is no longer this worker's running run; the executor finds that out
    itself at its next check (Worker._execute, cancelled)."""

    def __init__(self, store: RunStore, run_id: int, worker: str, period_s: float):
        self.store, self.run_id, self.worker, self.period_s = store, run_id, worker, period_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"heartbeat-{run_id}", daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.period_s):
            try:
                if not self.store.heartbeat(self.run_id, self.worker):
                    return
            except Exception as e:  # noqa: BLE001 - a busy DB is retried next beat
                print(f"heartbeat run {self.run_id}: {e}", file=sys.stderr, flush=True)

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()


def set_aside(run_dir: Path) -> Path:
    """Move a stale results directory to <results>/.orphaned/<id>-<time>, so a
    new run with its id starts empty and the old files are kept for whoever
    wants them."""
    dest = run_dir.parent / ".orphaned" / f"{run_dir.name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.name.split('.')[0]}.{n}")
        n += 1
    run_dir.rename(dest)
    return dest


class TraceLimit(Exception):
    """The run's trace reached Limits.max_trace_bytes; the run ends as error."""


class TraceWriter:
    """Appends JSON-lines trace records; each write() is one flushed batch.

    With `max_bytes`, a batch that would take the file past it is dropped,
    a log record says so, and TraceLimit is raised: a run can't fill the
    shared results volume (a fast periodic sender or symbol watch over a long
    run). After that only log records (the run's own end) are written.

    With `header`, a new file starts with it as its first line (the run's
    token, vhil/server/runs.py trace_header)."""

    def __init__(self, path: Path, max_bytes: Optional[int] = None,
                 header: Optional[dict] = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_bytes = max_bytes
        self._f = open(path, "a")
        self._size = self._f.tell()
        self.full = False
        if header is not None and self._size == 0:
            line = json.dumps(header, separators=(",", ":")) + "\n"
            self._f.write(line)
            self._f.flush()
            self._size += len(line)

    def write(self, records: list[dict]) -> None:
        records.sort(key=lambda r: r["t_us"])   # stable: same-time order kept
        if self.full:
            records = [r for r in records if r.get("kind") == "log"]
        text = "".join(json.dumps(rec, separators=(",", ":")) + "\n" for rec in records)
        if self.max_bytes is not None and not self.full and self._size + len(text) > self.max_bytes:
            self.full = True
            t_us = records[0]["t_us"] if records else 0
            note = json.dumps({"kind": "log", "t_us": t_us,
                               "text": f"trace limit reached ({self.max_bytes >> 20} MiB): "
                                       "stopping the run"}, separators=(",", ":")) + "\n"
            self._f.write(note)
            self._f.flush()
            self._size += len(note)
            raise TraceLimit(f"the trace reached this server's limit of "
                             f"{self.max_bytes >> 20} MiB (VHIL_MAX_TRACE_MB)")
        self._f.write(text)
        self._f.flush()
        self._size += len(text)

    def log(self, t_us: int, text: str) -> None:
        self.write([{"kind": "log", "t_us": t_us, "text": text}])

    def close(self) -> None:
        self._f.close()


# -- the `run` scenario ------------------------------------------------------------

def execute_run(sim, scenario: dict, trace: TraceWriter, *,
                cancelled: Callable[[], bool] = lambda: False,
                progress: Callable[[int], None] = lambda us: None) -> dict:
    """Drive a started Sim through a `run` scenario (runs.RunScenario as a
    dict); returns the summary. Times in the scenario are virtual time from
    the system's power-on. Raises Cancelled when `cancelled()` turns true at
    a slice boundary."""
    end_us = int(scenario["virtual_ms"]) * 1000
    slice_us = int(scenario.get("slice_ms", 100)) * 1000
    system = sim.system
    frames = {bus: 0 for bus in system.buses}     # received from the bus
    sent = {bus: 0 for bus in system.buses}       # the scenario's own CAN stimuli
    loads: dict[str, list[float]] = {bus: [] for bus in system.buses}
    counts = {"edges": 0, "samples": 0}

    # Timed actions: (t_us, seq, fn). CAN frames are scheduled in the probe
    # up front (exact virtual time); GPIO and analog are applied when time
    # reaches them, so the run stops there first.
    events: list = []
    seq = 0

    def at(t_us: int, fn) -> None:
        nonlocal seq
        heapq.heappush(events, (t_us, seq, fn))
        seq += 1

    # A CAN stimulus is in the trace as the frames the probe sent, stamped
    # when they went out (`src: "stimulus"`, CanBus.sent); a periodic
    # sender's start and stop and every other stimulus as log records.
    notes: list = []

    def note(t_us: int, text: str) -> None:
        nonlocal seq
        heapq.heappush(notes, (t_us, seq, text))
        seq += 1

    def gpio_target(board: str, pin: str) -> tuple[str, int]:
        _, kind, target = system.resolve(f"{board}.{pin}")
        if kind != "gpio":
            raise ValueError(f"{board}.{pin} is {kind}, not gpio")
        return target["port"], target["pin"]

    for i, s in enumerate(scenario.get("stimuli", [])):
        t_us = int(s.get("at_ms", 0) * 1000)
        kind = s["kind"]
        what = f"stimulus {kind}"
        if kind in ("can_send", "can_periodic"):
            what += f" {s['bus']} 0x{s['id']:X} [{s.get('data', '')}]"
        if kind == "can_send":
            sim.can(s["bus"]).send_at(t_us, s["id"], bytes.fromhex(s.get("data", "")),
                                      s.get("ext", False))
        elif kind == "can_periodic":
            key = f"stim{i}"
            bus = sim.can(s["bus"])
            # SendPeriodic's start 0 means "now", which is the same thing at t=0.
            bus.send_periodic(key, s["id"], bytes.fromhex(s.get("data", "")), s["period_ms"],
                              start_us=t_us, extended=s.get("ext", False))
            note(t_us, f"{what} every {s['period_ms']} ms")
            if s.get("until_ms") is not None:
                at(int(s["until_ms"] * 1000), lambda bus=bus, key=key: bus.stop_periodic(key))
                note(int(s["until_ms"] * 1000), f"{what} stopped")
        elif kind == "gpio":
            port, pin = gpio_target(s["board"], s["pin"])
            at(t_us, lambda b=s["board"], port=port, pin=pin, lv=s["level"]:
               sim.io(b).set_input(port, pin, lv))
            note(t_us, f"{what} {s['board']}.{s['pin']} = {int(s['level'])}")
        elif kind == "analog":
            at(t_us, lambda b=s["board"], pin=s["pin"], v=s["volts"]: sim.io(b).set_voltage(pin, v))
            note(t_us, f"{what} {s['board']}.{s['pin']} = {s['volts']} V")
        else:
            raise ValueError(f"unknown stimulus kind '{kind}'")

    pin_names: dict[tuple[str, str], str] = {}     # (board, "port:pin") -> "PB4"
    samplers = []                                  # [next_t_us, period_us, watch]
    for w in scenario.get("watch", []):
        if w["kind"] == "pin":
            port, pin = gpio_target(w["board"], w["pin"])
            pin_names[(w["board"], sim.io(w["board"]).watch(port, pin))] = w["pin"]
        elif w["kind"] == "symbol":
            samplers.append([0, int(w.get("period_ms", 10) * 1000), w])
        else:
            raise ValueError(f"unknown watch kind '{w['kind']}'")
    edge_boards = sorted({b for b, _ in pin_names})

    now = sim.now_us()
    since = now          # frames/edges from here on belong to the next slice
    slice_end = now + slice_us
    batch: list[dict] = []   # this slice's records, written when it ends
    while True:
        while events and events[0][0] <= now:
            heapq.heappop(events)[2]()
        for s in samplers:
            if s[0] <= now:
                w = s[2]
                batch.append({"kind": "sample", "t_us": now, "board": w["board"], "name": w["name"],
                              "value": sim.read_symbol(w["board"], w["name"], w.get("size", 1))})
                counts["samples"] += 1
                while s[0] <= now:
                    s[0] += s[1]
        slice_done = now >= slice_end or now >= end_us
        if slice_done:
            for bus in system.buses:
                seen = sim.can(bus).frames(since_us=since)
                mine = sim.can(bus).sent(since_us=since)
                for f in seen:
                    batch.append({"kind": "frame", "t_us": f.t_us, "bus": bus, "id": f.id,
                                  "ext": f.extended, "data": f.data.hex()})
                    frames[bus] += 1
                for f in mine:
                    batch.append({"kind": "frame", "t_us": f.t_us, "bus": bus, "id": f.id,
                                  "ext": f.extended, "data": f.data.hex(), "src": "stimulus"})
                    sent[bus] += 1
                if now >= since:
                    record = bus_load(sim, bus, since, now + 1, seen + mine)
                    loads[bus].append(record["load"])
                    batch.append(record)
            for board in edge_boards:
                for e in sim.io(board).edges(since_us=since):
                    batch.append({"kind": "edge", "t_us": e.t_us, "board": board,
                                  "pin": pin_names.get((board, e.pin), e.pin), "level": int(e.level)})
                    counts["edges"] += 1
            while notes and notes[0][0] <= now:
                t, _, text = heapq.heappop(notes)
                batch.append({"kind": "log", "t_us": t, "text": text})
            since = now + 1
            slice_end = now + slice_us
            # Everything in a slice lies in (previous end, now], so the file
            # stays in virtual-time order across slices.
            trace.write(batch)
            batch = []
            progress(now)
            if now >= end_us:
                break
            if cancelled():
                raise Cancelled({"frames": frames, "sent": sent, **counts,
                                 "bus_load": load_summary(loads)})
        target = min([end_us, slice_end] + ([events[0][0]] if events else [])
                     + [s[0] for s in samplers])
        new = sim.run_for(us=max(target - now, 1))
        if new <= now:
            raise RuntimeError(f"virtual time stuck at {now} us")
        now = new
    return {"virtual_ms": scenario["virtual_ms"], "frames": frames, "sent": sent, **counts,
            "bus_load": load_summary(loads)}


def bus_load(sim, bus: str, from_us: int, to_us: int, frames) -> dict:
    """A `bus_load` trace record for [from_us, to_us): the bus model's busy
    time on an arbitrated bus, else an estimate from the frames the probe saw
    and sent there (Renode's hub has no timing; vhil/canframe.py)."""
    can = sim.can(bus)
    if can.arbitrated:
        load, exact = can.load(from_us, to_us), True
    else:
        load, exact = canframe.load(frames, to_us - from_us), False
    return {"kind": "bus_load", "t_us": to_us - 1, "bus": bus, "load": round(load, 4),
            "window_us": to_us - from_us, "exact": exact}


def load_summary(loads: dict[str, list[float]]) -> dict:
    """bus -> {mean, peak} of its per-slice loads."""
    return {bus: {"mean": round(sum(v) / len(v), 4), "peak": max(v)}
            for bus, v in loads.items() if v}


# -- the `pytest` scenario -----------------------------------------------------------

def junit_summary(path: Path) -> dict:
    if not path.is_file():
        return {}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    out = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for s in suites:
        for k in out:
            out[k] += int(s.get(k, 0))
    return out


def image_env(system: System, firmware: dict[str, Path]) -> dict[str, str]:
    """VHIL_<X>_ELF as tests/conftest.py reads them: board name for an app,
    firmware id for a bootloader (VHIL_CAN_BOOTLOADER_ELF)."""
    env = {}
    for key, elf in firmware.items():
        board, _, part = key.partition(".")
        name = system.boards[board].bootloader["id"] if part == "bootloader" else board
        env[f"VHIL_{name.upper().replace('-', '_')}_ELF"] = str(elf)
    return env


def execute_pytest(scenario: dict, run_dir: Path, workspace: Path, env: dict,
                   trace: TraceWriter, cancelled: Callable[[], bool],
                   max_output_bytes: Optional[int] = None) -> tuple[str, dict]:
    junit = run_dir / "junit.xml"
    cmd = [sys.executable, "-m", "pytest", scenario["select"], "-v", "-p", "no:cacheprovider",
           "--sim-log-dir", str(run_dir / "sim-logs"), f"--junitxml={junit}"]
    trace.log(0, "$ " + " ".join(cmd))
    deadline = time.monotonic() + scenario.get("timeout_s", 3600)
    with open(run_dir / "pytest.txt", "w") as out:
        proc = subprocess.Popen(cmd, cwd=workspace, env={**os.environ, **env},
                                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        reason = None
        while proc.poll() is None:
            try:
                if cancelled():
                    reason = "cancelled"
            except Lost:
                reason = "lost"     # stop pytest before letting the run go
            if not reason and time.monotonic() > deadline:
                reason = "timeout"
            if not reason and max_output_bytes is not None \
                    and (run_dir / "pytest.txt").stat().st_size > max_output_bytes:
                reason = "output"
            if reason:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                break
            time.sleep(1)
    rc = proc.returncode
    summary = {"returncode": rc, **junit_summary(junit)}
    trace.log(0, f"pytest exited {rc}" + (f" ({reason})" if reason else ""))
    if reason == "lost":
        raise Lost()
    if reason == "cancelled":
        raise Cancelled(summary)
    if reason == "timeout":
        return "error", {**summary, "error": f"timed out after {scenario.get('timeout_s')} s"}
    if reason == "output":
        return "error", {**summary, "error": f"pytest output passed this server's limit of "
                                             f"{max_output_bytes >> 20} MiB (VHIL_MAX_OUTPUT_MB)"}
    if rc == 0:
        if summary.get("tests") and summary["skipped"] == summary["tests"]:
            # e.g. no firmware image: nothing was tested, which isn't a pass.
            return "error", {**summary, "error": "every test was skipped"}
        return "passed", summary
    if rc == 1:
        return "failed", summary
    return "error", {**summary, "error": f"pytest exited {rc}"}


# -- firmware --------------------------------------------------------------------

class FirmwareResolver:
    """Image key -> ELF for a system at the requested refs (module doc)."""

    def __init__(self, fw_dir: Path = DEFAULT_FW_DIR, build: bool = True,
                 log: Callable[[str], None] = print):
        self.fw_dir, self.build, self.log = Path(fw_dir), build, log

    def _built(self) -> dict[Path, str]:
        return built_images(self.fw_dir)

    def expected(self, system: System, refs: dict) -> dict[str, tuple[str, Path]]:
        """key -> (ref, the ELF path vhil.system build gives it)."""
        out = {}
        for key in system.images():
            board, _, part = key.partition(".")
            fw = system.boards[board].bootloader if part else system.boards[board].firmware
            # The run's ref, else the system's (firmware_ref / bootloader_ref),
            # else the catalogue's.
            ref = refs.get(key) or system.boards[board].ref("bootloader" if part else "firmware")
            out[key] = (ref, image_path(self.fw_dir, fw, ref).resolve())
        return out

    def resolve(self, system: System, refs: dict) -> dict[str, Path]:
        want = self.expected(system, refs)
        built = self._built()
        missing = {k: ref for k, (ref, p) in want.items() if p not in built or not p.is_file()}
        if missing:
            if not self.build:
                raise RuntimeError(f"no built image for {missing} in {self.fw_dir}/built.txt")
            self.fw_dir.mkdir(parents=True, exist_ok=True)
            # One build at a time per firmware directory: a build replaces
            # its source trees.
            with open(self.fw_dir / ".build.lock", "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                built = self._built()
                if any(p not in built or not p.is_file() for _, p in want.values()):
                    cmd = [sys.executable, "-m", "vhil.system", "build", str(system.path),
                           "--workdir", str(self.fw_dir)]
                    for key, (ref, _) in want.items():
                        cmd += ["--ref", f"{key}={ref}"]
                    self.log("building firmware: " + " ".join(cmd))
                    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
                    with open(self.fw_dir / "built.txt", "a") as f:
                        f.write(out.stdout)
        out = {k: p for k, (_, p) in want.items()}
        for k, p in out.items():
            if not p.is_file():
                raise RuntimeError(f"firmware build gave no {k} image at {p}")
        return out


# -- the worker --------------------------------------------------------------------

def _default_sim(system_path: Path, firmware: dict, log_path: Path):
    from vhil.sim import Sim
    return Sim(system_path, firmware, log_path=log_path)


class Worker:
    def __init__(self, settings: Settings, *, worker_id: Optional[str] = None,
                 sim_factory: Callable = _default_sim,
                 resolver: Optional[FirmwareResolver] = None,
                 heartbeat_s: float = HEARTBEAT_S, reclaim_after_s: float = RECLAIM_AFTER_S,
                 max_attempts: int = MAX_ATTEMPTS, limits: Optional[Limits] = None):
        if reclaim_after_s <= 2 * heartbeat_s:
            raise ValueError("reclaim_after_s must be more than two heartbeats")
        self.settings = settings
        self.store = RunStore(settings.db)
        # Host-network containers share a hostname and are all pid 1.
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.sim_factory = sim_factory
        self.resolver = resolver or FirmwareResolver()
        self.heartbeat_s, self.reclaim_after_s = heartbeat_s, reclaim_after_s
        self.max_attempts = max_attempts
        self.limits = limits or Limits.from_env()

    def reclaim(self) -> list[dict]:
        moved = self.store.reclaim(self.reclaim_after_s, self.max_attempts)
        for r in moved:
            print(f"run {r['id']}: reclaimed from {r['worker']} after attempt {r['attempts']} "
                  f"-> {r['state']}", flush=True)
        return moved

    def run_once(self) -> Optional[int]:
        """Claim and execute one queued run; its id, or None if none was queued."""
        self.reclaim()
        run = self.store.claim(self.id)
        if run is None:
            return None
        run_dir = Path(self.settings.results) / str(run["id"])
        if run["attempts"] == 1 and run_dir.exists():
            orphan = set_aside(run_dir)
            print(f"run {run['id']}: results already at {run_dir} (from before a database "
                  f"restore?) moved to {orphan}", flush=True)
        if run["attempts"] > 1 and (run_dir / TRACE).is_file():
            (run_dir / TRACE).rename(run_dir / f"trace.attempt{run['attempts'] - 1}.jsonl")
        trace = TraceWriter(run_dir / TRACE, max_bytes=self.limits.max_trace_bytes,
                            header=trace_header(run))
        with Heartbeat(self.store, run["id"], self.id, self.heartbeat_s):
            return self._execute(run, run_dir, trace)

    def _execute(self, run: dict, run_dir: Path, trace: TraceWriter) -> int:
        run_id = run["id"]
        state, virtual_us, summary = "error", 0, {}
        progress = {"us": 0}

        def cancelled() -> bool:
            """True once cancelled; raises Lost once the run is someone else's."""
            st, holder = self.store.holder(run_id)
            if holder != self.id:
                raise Lost()
            if st == "cancelled":
                return True
            if st != "running":
                raise Lost()
            return False

        def on_progress(us: int) -> None:
            progress["us"] = us
            self.store.progress(run_id, us, worker=self.id)

        try:
            if run["attempts"] > 1:
                prev = (run.get("summary") or {}).get("reclaimed_from") or "a worker"
                trace.log(0, f"attempt {run['attempts']} of {self.max_attempts}: reclaimed from "
                             f"{prev}, whose heartbeat stopped")
            system_path = self._system_file(run, run_dir, trace)
            system = System(system_path)
            firmware = self.resolver.resolve(system, run["firmware"])
            trace.log(0, f"worker {self.id}; firmware " +
                      ", ".join(f"{k}={p}" for k, p in firmware.items()))
            scenario = run["scenario"]
            if scenario["kind"] == "run":
                sim = self.sim_factory(system_path, firmware, run_dir / "renode.log")
                with sim:
                    summary = execute_run(sim, scenario, trace, cancelled=cancelled,
                                          progress=on_progress)
                state, virtual_us = "passed", progress["us"]
            elif scenario["kind"] == "pytest":
                state, summary = execute_pytest(scenario, run_dir, Path(self.settings.workspace),
                                                image_env(system, firmware), trace, cancelled,
                                                max_output_bytes=self.limits.max_output_bytes)
            else:
                raise ValueError(f"unknown scenario kind '{scenario['kind']}'")
            summary["firmware"] = {k: str(p) for k, p in firmware.items()}
            if run.get("ref"):
                summary["system_ref"] = run["ref"]
        except Lost:
            trace.log(progress["us"], f"worker {self.id} lost the run: it was reclaimed")
            trace.close()
            print(f"run {run_id}: lost (reclaimed by another worker)", flush=True)
            return run_id
        except Cancelled as e:
            state, virtual_us = "cancelled", progress["us"]
            summary = {**e.summary, "cancelled_at_us": virtual_us}
            trace.log(virtual_us, "cancelled")
        except BaseException as e:  # noqa: BLE001 - every failure ends the run as error
            state, virtual_us = "error", progress["us"]
            summary = {"error": f"{type(e).__name__}: {e}"}
            (run_dir / "worker-error.txt").write_text(traceback.format_exc())
            trace.log(virtual_us, f"error: {summary['error']}")
            if not isinstance(e, Exception):
                self.store.finish(run_id, state, virtual_us, summary, worker=self.id)
                trace.close()
                raise
        final = self.store.finish(run_id, state, virtual_us, summary, worker=self.id)
        trace.close()
        print(f"run {run_id}: {final} ({virtual_us} us)", flush=True)
        return run_id

    def _system_file(self, run: dict, run_dir: Path, trace: TraceWriter) -> Path:
        """The system file the run executes: systems/<id>.yaml as of the
        run's commit, written into its directory, so the shared workspace's
        HEAD and working tree never move (vhil/server/runs.py, runs at a
        saved ref). A run queued outside git uses the working tree's file."""
        ws = Path(self.settings.workspace)
        commit = run.get("ref") or ""
        if not commit:
            return ws / "systems" / f"{run['system']}.yaml"
        head = Workspace(ws).ref()
        name = run.get("ref_name") or "HEAD"
        if run["scenario"]["kind"] == "pytest":
            if commit != head:
                raise RuntimeError(f"the workspace is at {head[:12] or 'no commit'}, not "
                                   f"{commit[:12]} ({name}): a pytest run needs its ref checked out")
            path = ws / "systems" / f"{run['system']}.yaml"
        else:
            changed = code_changes(ws, head, commit) if commit != head else []
            if changed:
                raise RuntimeError(f"{name} ({commit[:12]}) and the workspace ({head[:12]}) differ "
                                   f"in what a run reads: {', '.join(changed[:10])}")
            path = materialise_system(ws, commit, run["system"], run_dir / "system")
        trace.log(0, f"system systems/{run['system']}.yaml at {name} ({commit[:12]})")
        return path

    def loop(self, poll_s: float = 1.0) -> None:
        print(f"worker {self.id}: {self.settings.db}", flush=True)
        while True:
            if self.run_once() is None:
                time.sleep(poll_s)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m vhil.worker", description=__doc__.splitlines()[0])
    p.add_argument("--once", action="store_true", help="run at most one queued run, then exit")
    p.add_argument("--poll", type=float, default=1.0, help="seconds between queue polls")
    p.add_argument("--fw-dir", type=Path, default=DEFAULT_FW_DIR,
                   help="firmware build directory with built.txt (default $VHIL_FW_DIR or /vhil/fw)")
    p.add_argument("--no-build", action="store_true", help="never build firmware; reuse only")
    p.add_argument("--heartbeat", type=float, default=HEARTBEAT_S,
                   help=f"seconds between a held run's heartbeats (default {HEARTBEAT_S:g})")
    p.add_argument("--reclaim-after", type=float, default=RECLAIM_AFTER_S,
                   help="seconds without a heartbeat before a running run is reclaimed "
                        f"(default {RECLAIM_AFTER_S:g}; the same on every worker)")
    a = p.parse_args(argv)
    # docker stop sends SIGTERM: end the current run as error, not silently.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    worker = Worker(Settings.from_env(), resolver=FirmwareResolver(
        a.fw_dir, build=not a.no_build, log=lambda m: print(m, file=sys.stderr, flush=True)),
        heartbeat_s=a.heartbeat, reclaim_after_s=a.reclaim_after)
    if a.once:
        worker.run_once()
    else:
        worker.loop(a.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
