"""Runs (M5.2, #114): the queue and history table, and the /api/runs endpoints.

The `runs` table in SQLite (WAL) is both the job queue and the run history
(docs/architecture/m5-web-app.md). The API inserts a row in state `queued`;
a worker (vhil/worker.py) claims it with one atomic UPDATE, writes the run's
trace to `<results>/<id>/trace.jsonl` as virtual time advances, and sets the
final state. The live WebSocket tails that file, so a finished run replays
from exactly what it streamed.

    queued ─claim─▶ running ─▶ passed | failed | error
       │  ▲              │
       │  └───reclaim────┤     (heartbeat stale; attempts left)
       │                 └─reclaim─▶ error   (heartbeat stale; no attempts left)
       └──────cancel──┴──────▶ cancelled   (the worker stops at its next slice)

A worker that holds a run beats its `heartbeat` (unix seconds) every few
seconds and at every slice. A worker that dies (OOM, a host reboot, docker
kill) leaves its run `running` with a heartbeat that no longer moves: any
worker's next poll reclaims it (RunStore.reclaim) back to `queued`, or to
`error` once it has been attempted MAX_ATTEMPTS times. The heartbeat says
the worker process is alive, not that the run makes progress: a run that
hangs is bounded by its own limits (virtual_ms, a pytest timeout_s).

Each run gets a random `token` when it is created, and the worker writes it
in its trace's first line (a `run` header, never served). The API serves a
trace, its live stream and the run's artifacts only once that header carries
the run's token. A database restored to a snapshot hands out the ids of runs
it rolled back, whose results stay on the runs volume, read-only to the API;
until the worker sets them aside (vhil/worker.py, set_aside), a client of the
new run sees nothing of them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt,
                      field_validator, model_validator)

from vhil import expect as vexpect
from vhil.server.workspace import SYSTEM_ID
from vhil.system import System, SystemError

log = logging.getLogger("vhil.server.runs")

STATES = ("queued", "running", "passed", "failed", "error", "cancelled")
TERMINAL = frozenset({"passed", "failed", "error", "cancelled"})
TRACE_KINDS = frozenset({"frame", "edge", "sample", "log"})
TRACE = "trace.jsonl"
# The kind of a trace's first line, which says whose trace it is (trace_header).
HEADER_KIND = "run"
# A held run's heartbeat period, how stale it may get before another worker
# reclaims the run, and how many times a run is started before a lost worker
# ends it as error instead.
HEARTBEAT_S = 10.0
RECLAIM_AFTER_S = 60.0
MAX_ATTEMPTS = 2
# A `run` scenario's virtual time when the request names none, and what the
# Runs page and the editor offer: every run counts from power-on, and each
# MainLite spends its CAN bootloader's 2 s auto-jump window before its app
# starts (CLAUDE.md invariant 5), so less than 3 s shows little of the app.
DEFAULT_VIRTUAL_MS = 3000


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    return int(v) if v.strip() else default


@dataclass(frozen=True)
class Limits:
    """What one user, or everyone, may ask of the workers (docs/deploy.md,
    "Limits"). Each is an environment variable, read when the API starts
    (the worker reads max_trace_bytes / max_output_bytes)."""
    max_virtual_ms: int = 600_000           # VHIL_MAX_VIRTUAL_MS
    max_active: int = 50                    # VHIL_MAX_QUEUED: queued + running, everyone
    max_active_per_user: int = 10           # VHIL_MAX_QUEUED_PER_USER
    max_stimuli: int = 1000                 # VHIL_MAX_STIMULI
    max_watch: int = 100                    # VHIL_MAX_WATCHES
    max_expect: int = 200                   # VHIL_MAX_EXPECTS
    max_trace_bytes: int = 512 << 20        # VHIL_MAX_TRACE_MB: a run's trace.jsonl
    max_output_bytes: int = 64 << 20        # VHIL_MAX_OUTPUT_MB: a pytest run's output

    @classmethod
    def from_env(cls) -> "Limits":
        d = cls()
        return cls(max_virtual_ms=_env_int("VHIL_MAX_VIRTUAL_MS", d.max_virtual_ms),
                   max_active=_env_int("VHIL_MAX_QUEUED", d.max_active),
                   max_active_per_user=_env_int("VHIL_MAX_QUEUED_PER_USER", d.max_active_per_user),
                   max_stimuli=_env_int("VHIL_MAX_STIMULI", d.max_stimuli),
                   max_watch=_env_int("VHIL_MAX_WATCHES", d.max_watch),
                   max_expect=_env_int("VHIL_MAX_EXPECTS", d.max_expect),
                   max_trace_bytes=_env_int("VHIL_MAX_TRACE_MB", d.max_trace_bytes >> 20) << 20,
                   max_output_bytes=_env_int("VHIL_MAX_OUTPUT_MB", d.max_output_bytes >> 20) << 20)


class QueueFull(Exception):
    """Too many active runs, for the user or for everyone (HTTP 429)."""

# A git ref we pass to `git clone -b` and use in a directory name: no option
# look-alikes, no path climbing.
_REF = re.compile(r"^(?!-)(?!.*\.\.)[\w./-]{1,100}\Z", re.ASCII)
_SELECT = re.compile(r"^tests/(?!.*\.\.)[\w/.-]+\.py(::[\w\[\]\-.,=]+)*$")
_HEX = re.compile(r"^([0-9a-fA-F]{2})*$")
# Names a scenario uses to point into its system: a board or bus instance
# (schema $defs/name) and a board connector or pin. They are matched against
# the system before a run is queued (check_against_system); the shape is
# checked here too, so nothing else can reach a monitor command.
_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
_PIN = r"^[A-Za-z0-9_]{1,64}$"
# A row's own name (a label on the timeline, what a stop_periodic names) and a
# scenario's (its file name: systems/<system>.scenarios/<name>.yaml).
_ROW = r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
SCENARIO_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")


# -- scenario models -------------------------------------------------------------

class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Can(_Model):
    name: Optional[str] = Field(None, pattern=_ROW)
    at_ms: float = Field(0, ge=0)
    bus: str = Field(pattern=_NAME)
    id: int = Field(ge=0, le=0x1FFFFFFF)
    data: str = ""          # hex, up to 64 bytes (CAN FD)
    ext: bool = False

    @field_validator("data")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not _HEX.match(v) or len(v) > 128:
            raise ValueError("data must be hex, at most 64 bytes")
        return v.lower()

    @model_validator(mode="after")
    def _std_id(self):
        if not self.ext and self.id > 0x7FF:
            raise ValueError(f"id 0x{self.id:X} needs ext: true")
        return self


class CanSend(_Can):
    kind: Literal["can_send"]


class CanPeriodic(_Can):
    kind: Literal["can_periodic"]
    period_ms: float = Field(gt=0, le=60_000)
    until_ms: Optional[float] = Field(None, ge=0)   # default: to the end


class GpioSet(_Model):
    kind: Literal["gpio"]
    name: Optional[str] = Field(None, pattern=_ROW)
    at_ms: float = Field(0, ge=0)
    board: str = Field(pattern=_NAME)
    pin: str = Field(pattern=_PIN)      # catalogue gpio name, e.g. "PB5"
    level: bool


class AnalogSet(_Model):
    kind: Literal["analog"]
    name: Optional[str] = Field(None, pattern=_ROW)
    at_ms: float = Field(0, ge=0)
    board: str = Field(pattern=_NAME)
    pin: str = Field(pattern=_PIN)      # catalogue analog_in name, e.g. "PF7"
    volts: float = Field(ge=0, le=3.6)


class StopPeriodic(_Model):
    """Stops the can_periodic named `periodic` (a live session's op too)."""
    kind: Literal["stop_periodic"]
    name: Optional[str] = Field(None, pattern=_ROW)
    at_ms: float = Field(0, ge=0)
    periodic: str = Field(pattern=_ROW)


Stimulus = Annotated[Union[CanSend, CanPeriodic, StopPeriodic, GpioSet, AnalogSet],
                     Field(discriminator="kind")]


class SymbolWatch(_Model):
    kind: Literal["symbol"]
    board: str = Field(pattern=_NAME)
    name: str = Field(pattern=r"^[A-Za-z_]\w{0,127}$")
    size: Literal[1, 2, 4] = 1
    period_ms: float = Field(10, ge=1)


class PinWatch(_Model):
    kind: Literal["pin"]
    board: str = Field(pattern=_NAME)
    pin: str = Field(pattern=_PIN)


Watch = Annotated[Union[SymbolWatch, PinWatch], Field(discriminator="kind")]

# An expect's value: a number, a boolean, or a label (a field's value-table
# entry; high/low for a pin). vhil/expect.py says how each compares.
Label = Annotated[str, Field(pattern=f"^{vexpect.LABEL.pattern}$")]


class Expect(_Model):
    """What the run must show, checked against its trace in virtual time
    (vhil/expect.py): `signal` over [at_ms, until_ms] (default: to the end)."""
    check: Literal["eventually", "always", "never", "period", "count"]
    name: Optional[str] = Field(None, pattern=_ROW)
    at_ms: float = Field(0, ge=0)
    until_ms: Optional[float] = Field(None, ge=0)
    signal: str
    # eventually / always / never
    op: Optional[Literal["==", "!=", "<", "<=", ">", ">="]] = None
    value: Optional[Union[StrictBool, StrictInt, StrictFloat, Label]] = None
    # period
    min_ms: Optional[float] = Field(None, ge=0, le=600_000)
    max_ms: Optional[float] = Field(None, ge=0, le=600_000)
    # count
    min: Optional[int] = Field(None, ge=0)
    max: Optional[int] = Field(None, ge=0)

    @field_validator("signal")
    @classmethod
    def _signal(cls, v: str) -> str:
        vexpect.parse_signal(v)
        return v

    @model_validator(mode="after")
    def _shape(self):
        sig = vexpect.parse_signal(self.signal)
        if self.until_ms is not None and self.until_ms < self.at_ms:
            raise ValueError(f"until_ms {self.until_ms:g} is before at_ms {self.at_ms:g}")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("value must be a finite number")
        given = lambda *names: [n for n in names if getattr(self, n) is not None]  # noqa: E731
        if self.check in vexpect.VALUE_CHECKS:
            if sig.kind == "frame" and not sig.field:
                raise ValueError(f"{self.check} reads a value: frame:<bus>.<message>.<field>")
            if self.value is None:
                raise ValueError(f"{self.check} needs a value")
            if isinstance(self.value, str) and (self.op or "==") not in ("==", "!="):
                raise ValueError(f"a label compares with == or != only, not {self.op}")
            extra = given("min_ms", "max_ms", "min", "max")
        else:
            if sig.kind != "frame" or sig.field:
                raise ValueError(f"{self.check} counts a frame: frame:<bus>.<message>")
            extra = given("op", "value") + (given("min", "max") if self.check == "period"
                                            else given("min_ms", "max_ms"))
            want = ("min_ms", "max_ms") if self.check == "period" else ("min", "max")
            if not given(*want):
                raise ValueError(f"{self.check} needs {' or '.join(want)}")
            lo, hi = (getattr(self, n) for n in want)
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"{want[0]} is more than {want[1]}")
        if extra:
            raise ValueError(f"{self.check} takes no {', '.join(extra)}")
        return self


class RunScenario(_Model):
    kind: Literal["run"]
    # The scenario file it came from (systems/<system>.scenarios/<name>.yaml),
    # if any: the Tests view's last result for it is this run's.
    name: Optional[str] = None
    virtual_ms: int = Field(DEFAULT_VIRTUAL_MS, ge=1, le=600_000)
    # Virtual time per slice: the trace is flushed and cancellation checked
    # after each one.
    slice_ms: int = Field(100, ge=10, le=1000)
    stimuli: list[Stimulus] = []
    watch: list[Watch] = []
    expect: list[Expect] = []

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not SCENARIO_NAME.match(v):
            raise ValueError("name must be a scenario name: lowercase letters, digits and '-', "
                             "at most 64")
        return v

    @model_validator(mode="after")
    def _rows(self):
        """A periodic's name is unique, and a stop names a periodic started
        no later; an expect's window ends by the run's."""
        started: dict[str, float] = {}
        for i, s in enumerate(self.stimuli):
            if isinstance(s, CanPeriodic) and s.name is not None:
                if s.name in started:
                    raise ValueError(f"stimuli[{i}]: a second periodic named '{s.name}'")
                started[s.name] = s.at_ms
        for i, s in enumerate(self.stimuli):
            if isinstance(s, StopPeriodic):
                if s.periodic not in started:
                    raise ValueError(f"stimuli[{i}]: no can_periodic named '{s.periodic}' to stop")
                if s.at_ms < started[s.periodic]:
                    raise ValueError(f"stimuli[{i}]: stops '{s.periodic}' at {s.at_ms:g} ms, "
                                     f"before it starts at {started[s.periodic]:g} ms")
        for i, e in enumerate(self.expect):
            if e.until_ms is not None and e.until_ms > self.virtual_ms:
                raise ValueError(f"expect[{i}]: until_ms {e.until_ms:g} is past the run's "
                                 f"{self.virtual_ms} ms")
        return self


class PytestScenario(_Model):
    kind: Literal["pytest"]
    select: str             # a node id under tests/, e.g. tests/sim/test_x.py::test_y
    timeout_s: int = Field(3600, ge=10, le=6 * 3600)

    @field_validator("select")
    @classmethod
    def _select(cls, v: str) -> str:
        if not _SELECT.match(v):
            raise ValueError("select must be a test node id under tests/ (tests/…/x.py[::name])")
        return v


Scenario = Annotated[Union[RunScenario, PytestScenario], Field(discriminator="kind")]


class RunRequest(_Model):
    system: str
    ref: Optional[str] = None
    # image key (System.images: "<board>", "<board>.bootloader") -> firmware
    # ref; missing or null = the catalogue firmware's default ref.
    firmware: dict[str, Optional[str]] = {}
    scenario: Scenario

    @field_validator("system")
    @classmethod
    def _system(cls, v: str) -> str:
        if not SYSTEM_ID.match(v):
            raise ValueError("system must be a system id: lowercase letters, digits and '-', "
                             "at most 64")
        return v

    @field_validator("firmware")
    @classmethod
    def _refs(cls, v: dict) -> dict:
        for key, ref in v.items():
            if ref is not None and not _REF.match(ref):
                raise ValueError(f"firmware ref for '{key}' is not a plain git ref: {ref!r}")
        return v


def check_against_system(req: RunRequest, system: System, workspace: Path) -> list[str]:
    """What the scenario names that the system (or the workspace) lacks."""
    errors = []
    images = system.images()
    for key in req.firmware:
        if key not in images:
            errors.append(f"firmware: '{key}' is not an image of {system.id} ({', '.join(images)})")
    sc = req.scenario
    if isinstance(sc, PytestScenario):
        if not (workspace / sc.select.split("::", 1)[0]).is_file():
            errors.append(f"select: no file {sc.select.split('::', 1)[0]}")
        return errors
    return errors + check_scenario(sc, system)


def check_scenario(sc: RunScenario, system: System) -> list[str]:
    """What a run scenario's rows name that the system lacks: buses, boards,
    and pins of the right kind. (Messages and fields are the firmware's:
    vhil/server/scenarios.py checks those against its contract.)"""
    errors = []

    def pin(board: str, name: str, kind: str, where: str):
        if board not in system.boards:
            errors.append(f"{where}: no board '{board}' in {system.id}")
            return
        try:
            _, got, _ = system.resolve(f"{board}.{name}")
        except SystemError as e:
            errors.append(f"{where}: {e}")
            return
        if got != kind:
            errors.append(f"{where}: {board}.{name} is {got}, not {kind}")

    for i, s in enumerate(sc.stimuli):
        where = f"stimuli[{i}]"
        if isinstance(s, (CanSend, CanPeriodic)):
            if s.bus not in system.buses:
                errors.append(f"{where}: no bus '{s.bus}' in {system.id}")
        elif isinstance(s, GpioSet):
            pin(s.board, s.pin, "gpio", where)
        elif isinstance(s, AnalogSet):
            pin(s.board, s.pin, "analog", where)
    for i, w in enumerate(sc.watch):
        where = f"watch[{i}]"
        if isinstance(w, PinWatch):
            pin(w.board, w.pin, "gpio", where)
        elif w.board not in system.boards:
            errors.append(f"{where}: no board '{w.board}' in {system.id}")
    for i, e in enumerate(sc.expect):
        where = f"expect[{i}]"
        sig = vexpect.parse_signal(e.signal)
        if sig.kind == "frame":
            if sig.owner not in system.buses:
                errors.append(f"{where}: no bus '{sig.owner}' in {system.id}")
        elif sig.kind == "pin":
            pin(sig.owner, sig.item, "gpio", where)
        elif sig.owner not in system.boards:
            errors.append(f"{where}: no board '{sig.owner}' in {system.id}")
    return errors


# -- the store -----------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    state      TEXT NOT NULL CHECK (state IN ('queued','running','passed','failed','error','cancelled')),
    system     TEXT NOT NULL,
    ref        TEXT NOT NULL DEFAULT '',
    firmware   TEXT NOT NULL DEFAULT '{}',
    scenario   TEXT NOT NULL,
    created    TEXT NOT NULL,
    started    TEXT,
    finished   TEXT,
    virtual_us INTEGER NOT NULL DEFAULT 0,
    summary    TEXT NOT NULL DEFAULT '{}',
    worker     TEXT,
    heartbeat  REAL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    ref_name   TEXT NOT NULL DEFAULT '',
    owner      TEXT NOT NULL DEFAULT '',
    token      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_state ON runs (state, id);
"""
# Columns added after the first release: a database created before them
# gets them on open.
_ADDED = {"heartbeat": "REAL", "attempts": "INTEGER NOT NULL DEFAULT 0",
          "ref_name": "TEXT NOT NULL DEFAULT ''", "owner": "TEXT NOT NULL DEFAULT ''",
          "token": "TEXT NOT NULL DEFAULT ''"}


class RunStore:
    """The runs table. Every call opens its own connection: the API serves
    from a thread pool and workers are other processes, and SQLite's own
    locking (WAL, busy timeout) is what keeps them consistent."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            have = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
            for name, decl in _ADDED.items():
                if name not in have:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _one(self, sql: str, args=()) -> Optional[dict]:
        db = self._connect()
        try:
            rows = db.execute(sql, args).fetchall()
        finally:
            db.close()
        return _row(rows[0]) if rows else None

    def _write(self, sql: str, args=()) -> Optional[dict]:
        """One write under BEGIN IMMEDIATE: the write lock is taken before the
        statement reads, so it never acts on a snapshot another writer has
        moved past (WAL would fail that with SQLITE_BUSY_SNAPSHOT)."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(sql, args).fetchall()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()
        return _row(rows[0]) if rows else None

    def create(self, system: str, ref: str, firmware: dict, scenario: dict,
               ref_name: str = "", owner: str = "", max_active: Optional[int] = None,
               max_active_per_user: Optional[int] = None) -> int:
        """A queued run. `ref`: the workspace commit its system file is read
        at ("" outside git); `ref_name`: the branch/tag/commit it was asked as;
        `owner`: the login of who started it ("dev" in dev mode). Each run gets
        a random token, which its trace's header must carry for the API to
        serve the trace (trace_header). Raises QueueFull when the active runs
        (queued or running) already number `max_active`, or `max_active_per_user`
        of `owner`'s: counted and inserted under one write lock, so
        concurrent requests can't overshoot."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                active = "SELECT count(*) FROM runs WHERE state IN ('queued', 'running')"
                if max_active is not None and \
                        db.execute(active).fetchone()[0] >= max_active:
                    raise QueueFull(f"the queue is full ({max_active} active runs): "
                                    "try again when some have finished")
                if max_active_per_user is not None and db.execute(
                        active + " AND owner = ?", (owner,)).fetchone()[0] \
                        >= max_active_per_user:
                    raise QueueFull(f"{owner or 'you'} already has {max_active_per_user} "
                                    "queued or running runs: wait for one to finish or cancel one")
                row = db.execute(
                    "INSERT INTO runs (state, system, ref, firmware, scenario, created, ref_name, "
                    "owner, token) VALUES ('queued', ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    (system, ref, json.dumps(firmware), json.dumps(scenario), now_iso(), ref_name,
                     owner, uuid.uuid4().hex)).fetchone()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()
        return row["id"]

    def get(self, run_id: int) -> Optional[dict]:
        return self._one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def list(self, limit: int = 100, state: Optional[str] = None,
             system: Optional[str] = None) -> list[dict]:
        where = [(c, v) for c, v in (("state", state), ("system", system)) if v]
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(f"{c} = ?" for c, _ in where)
                                      if where else "") + " ORDER BY id DESC LIMIT ?"
        db = self._connect()
        try:
            rows = db.execute(sql, [v for _, v in where] + [limit]).fetchall()
        finally:
            db.close()
        return [_row(r) for r in rows]

    def last_by_scenario(self, system: str) -> dict[str, dict]:
        """{scenario name: its latest run} of `system`'s runs that ran a
        scenario file (RunScenario.name): the Tests view's last results."""
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM runs WHERE id IN (SELECT max(id) FROM runs WHERE system = ? AND "
                "json_extract(scenario, '$.name') IS NOT NULL "
                "GROUP BY json_extract(scenario, '$.name'))", (system,)).fetchall()
        finally:
            db.close()
        return {r["scenario"]["name"]: r for r in map(_row, rows)}

    def claim(self, worker: str, now: Optional[float] = None) -> Optional[dict]:
        """The oldest queued run, now running for `worker`; None when the
        queue is empty. One statement under the write lock, so two workers
        never get the same row: the second one finds it no longer queued."""
        return self._write(
            "UPDATE runs SET state = 'running', started = ?, worker = ?, heartbeat = ?, "
            "attempts = attempts + 1 "
            "WHERE id = (SELECT id FROM runs WHERE state = 'queued' ORDER BY id LIMIT 1) "
            "AND state = 'queued' RETURNING *", (now_iso(), worker, _now(now)))

    def heartbeat(self, run_id: int, worker: str, now: Optional[float] = None) -> bool:
        """`worker` still holds the run; False once it was reclaimed (or
        finished or cancelled), and the worker should let it go."""
        return self._write("UPDATE runs SET heartbeat = ? WHERE id = ? AND worker = ? "
                           "AND state = 'running' RETURNING id",
                           (_now(now), run_id, worker)) is not None

    def progress(self, run_id: int, virtual_us: int, worker: Optional[str] = None,
                 now: Optional[float] = None) -> bool:
        """Virtual time reached (and, with `worker`, a heartbeat); False when
        the run is no longer running (for that worker)."""
        sql = "UPDATE runs SET virtual_us = ?, heartbeat = ? WHERE id = ? AND state = 'running'"
        args: tuple = (virtual_us, _now(now), run_id)
        if worker is not None:
            sql, args = sql + " AND worker = ?", args + (worker,)
        return self._write(sql + " RETURNING id", args) is not None

    def state(self, run_id: int) -> Optional[str]:
        row = self._one("SELECT state FROM runs WHERE id = ?", (run_id,))
        return row["state"] if row else None

    def holder(self, run_id: int) -> tuple[Optional[str], Optional[str]]:
        """(state, worker) of a run; (None, None) if there is none."""
        row = self._one("SELECT state, worker FROM runs WHERE id = ?", (run_id,))
        return (row["state"], row["worker"]) if row else (None, None)

    def finish(self, run_id: int, state: str, virtual_us: int, summary: dict,
               worker: Optional[str] = None) -> str:
        """Set a running run's final state; returns the state it ended in. A
        run cancelled meanwhile stays cancelled (its results are kept). With
        `worker`, only that worker's run is touched: one reclaimed from it
        (and maybe running elsewhere now) is left to its new holder."""
        if state not in TERMINAL:
            raise ValueError(f"not a final state: {state}")
        mine, args = ("", ()) if worker is None else (" AND worker = ?", (worker,))
        row = self._write(
            "UPDATE runs SET state = ?, finished = ?, virtual_us = ?, summary = ? "
            "WHERE id = ? AND state = 'running'" + mine + " RETURNING state",
            (state, now_iso(), virtual_us, json.dumps(summary), run_id) + args)
        if row:
            return row["state"]
        row = self._write("UPDATE runs SET virtual_us = ?, summary = ? "
                        "WHERE id = ? AND state = 'cancelled'" + mine + " RETURNING state",
                        (virtual_us, json.dumps(summary), run_id) + args)
        return row["state"] if row else (self.state(run_id) or "")

    def reclaim(self, stale_after_s: float = RECLAIM_AFTER_S, max_attempts: int = MAX_ATTEMPTS,
                now: Optional[float] = None) -> list[dict]:
        """Runs whose worker stopped beating for `stale_after_s`: back to
        queued, or to error once attempted `max_attempts` times. Returns
        [{id, state, worker, attempts}] of the runs it moved. A run with no
        heartbeat at all (left by a worker from before heartbeats) counts as
        stale. Both updates run under one write lock, so two workers polling
        at once move each run once."""
        cutoff = _now(now) - stale_after_s
        stale = "state = 'running' AND COALESCE(heartbeat, 0) < ?"
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                dead = db.execute(
                    "UPDATE runs SET state = 'error', finished = ?, summary = json_object("
                    "'error', 'worker ' || COALESCE(worker, '?') || ' stopped heartbeating; "
                    "attempt ' || attempts || ' of ' || ? || ', not retried') "
                    f"WHERE {stale} AND attempts >= ? RETURNING id, state, worker, attempts",
                    (now_iso(), max_attempts, cutoff, max_attempts)).fetchall()
                back = db.execute(
                    "UPDATE runs SET state = 'queued', started = NULL, heartbeat = NULL, "
                    "summary = json_object('reclaimed_from', worker, 'attempt', attempts), "
                    "worker = NULL, virtual_us = 0 "
                    f"WHERE {stale} RETURNING id, state, "
                    "json_extract(summary, '$.reclaimed_from') AS worker, attempts",
                    (cutoff,)).fetchall()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()
        return sorted((dict(r) for r in dead + back), key=lambda r: r["id"])

    def cancel(self, run_id: int) -> Optional[dict]:
        """Queued or running -> cancelled; a finished run is left as it is."""
        self._write("UPDATE runs SET state = 'cancelled', finished = ? "
                  "WHERE id = ? AND state IN ('queued', 'running') RETURNING id",
                  (now_iso(), run_id))
        return self.get(run_id)


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("firmware", "scenario", "summary"):
        if key in d:
            d[key] = json.loads(d[key]) if d[key] else {}
    return d


# -- trace ---------------------------------------------------------------------

# The next page's cursor, on every /trace response. A cursor is opaque to
# clients; it is the byte offset in trace.jsonl just past the last line the
# page consumed, so the next page seeks there instead of re-reading the file
# from the start.
CURSOR_HEADER = "X-Trace-Cursor"


def trace_header(run: dict) -> dict:
    """The first line the worker writes in a run's trace. It carries the
    run's token, so the API can tell the run's own trace from one left at
    its path by an earlier run with the same id."""
    return {"kind": HEADER_KIND, "t_us": 0, "run": run["id"], "token": run.get("token") or "",
            "attempt": run.get("attempts", 0)}


def trace_start(f, token: Optional[str] = None) -> Optional[int]:
    """Where the records start in the open trace `f` (binary): past its
    header, if it has one. With `token`, None unless the first line is a
    whole header carrying that token: the file is not (yet) that run's."""
    f.seek(0)
    first = f.readline()
    header = None
    if first.endswith(b"\n"):
        try:
            rec = json.loads(first)
        except ValueError:
            rec = None
        if isinstance(rec, dict) and rec.get("kind") == HEADER_KIND:
            header = rec
    if token and (header is None or header.get("token") != token):
        return None
    return len(first) if header is not None else 0


def trace_page(path: Path, cursor: int = 0, since_us: int = 0, kinds: Optional[set] = None,
               limit: Optional[int] = None, token: Optional[str] = None) -> tuple[list[bytes], int]:
    """(the matching records as their JSON lines, the cursor after them),
    reading from byte offset `cursor`. Records before since_us or of other
    kinds are skipped (and consumed); the header is never returned. A line
    the worker is still writing is left for the next page, so a cursor also
    resumes a live trace. With `token` (the run's), a trace whose header
    doesn't carry it is another run's: no records, cursor 0."""
    out: list[bytes] = []
    if not path.is_file():
        return out, 0 if token else cursor
    with open(path, "rb") as f:
        start = trace_start(f, token)
        if start is None:
            return out, 0
        pos = max(cursor, start)
        f.seek(pos)
        for line in f:
            if not line.endswith(b"\n"):
                break                     # a line the worker is still writing
            pos += len(line)
            rec = json.loads(line)
            if rec.get("t_us", 0) < since_us or (kinds and rec.get("kind") not in kinds):
                continue
            out.append(line[:-1])
            if limit is not None and len(out) >= limit:
                break
    return out, pos


def read_trace(path: Path, since_us: int = 0, kinds: Optional[set] = None,
               limit: Optional[int] = None, token: Optional[str] = None) -> list[dict]:
    return [json.loads(line) for line in trace_page(path, 0, since_us, kinds, limit, token)[0]]


def owns_results(results: Path, run: dict) -> bool:
    """Whether <results>/<id> is this run's: its trace's header carries the
    run's token. Any directory is a run's from before tokens."""
    token = run.get("token") or ""
    if not token:
        return True
    path = results / str(run["id"]) / TRACE
    if not path.is_file():
        return False
    with open(path, "rb") as f:
        return trace_start(f, token) is not None


def parse_cursor(text: Optional[str], path: Path) -> int:
    """A cursor from a previous page of this trace, checked: it must fall on
    a line boundary of the file (a cursor of another file is refused, not
    misread)."""
    if not text:
        return 0
    if not text.isdigit():
        raise HTTPException(422, f"bad trace cursor {text!r}")
    offset = int(text)
    size = path.stat().st_size if path.is_file() else 0
    if offset > size:
        raise HTTPException(422, f"trace cursor {offset} is past the end of the trace")
    if offset:
        with open(path, "rb") as f:
            f.seek(offset - 1)
            if f.read(1) != b"\n":
                raise HTTPException(422, f"trace cursor {offset} is not at a record boundary")
    return offset


def _kinds(text: Optional[str]) -> Optional[set]:
    if not text:
        return None
    kinds = {k.strip() for k in text.split(",") if k.strip()}
    bad = kinds - TRACE_KINDS
    if bad:
        raise HTTPException(422, f"unknown trace kinds {sorted(bad)} (have {sorted(TRACE_KINDS)})")
    return kinds


# -- artifacts -------------------------------------------------------------------
#
# A run's directory holds what the firmware, the sim and pytest wrote, and a
# pytest scenario runs any test of the workspace: none of it is the app's own
# content. Served from the app's origin as text/html or image/svg+xml it would
# run script there (stored XSS). So no artifact is ever rendered: text is
# text/plain, anything else an attachment, and every response carries nosniff
# and `Content-Security-Policy: sandbox` (no script, a unique origin) in case
# a browser renders it anyway. The run page's views (JUnit, snapshots, logs)
# fetch artifacts as text and escape them.

ARTIFACT_HEADERS = {"X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                    "Cache-Control": "private, no-cache"}


def _is_text(path: Path) -> bool:
    with open(path, "rb") as f:
        head = f.read(8192)
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        return e.start >= len(head) - 3      # a character cut by the 8 KiB window
    return True


def artifact_response(path: Path) -> FileResponse:
    if _is_text(path):
        return FileResponse(path, media_type="text/plain; charset=utf-8",
                            content_disposition_type="inline", filename=path.name,
                            headers=ARTIFACT_HEADERS)
    return FileResponse(path, media_type="application/octet-stream",
                        content_disposition_type="attachment", filename=path.name,
                        headers=ARTIFACT_HEADERS)


# -- runs at a saved ref -------------------------------------------------------------
#
# The editor saves a system as a one-file commit on a branch of the workspace
# (vhil/server/gitstore.py) and never checks it out. A run at such a ref
# reads systems/<id>.yaml as it is at the ref's commit (`git cat-file`, into
# the run's directory) and runs it with the worker's own catalogue, models,
# platforms and code. That is the run CI would make of the branch only if
# the two trees agree on everything but system files, so a ref that changes
# anything else a run reads (CODE_PATHS: catalog/, vhil/, models/, ...) is
# refused instead of run differently. A pytest scenario reads the tests and
# systems of the checked-out tree, so it runs only at the workspace's HEAD.

# What a `run` scenario reads besides its system file. A ref may differ from
# the workspace's HEAD only outside these.
CODE_PATHS = ("catalog/", "vhil/", "models/", "platforms/", "schemas/", "scripts/", "docker/",
              "configs/")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def resolve_ref(root: Path, ref: str) -> Optional[str]:
    """The commit a branch (local, or only on origin), tag or commit names in
    the workspace; None if none."""
    for cand in (ref, f"refs/remotes/origin/{ref}"):
        out = _git(root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}")
        if out.returncode == 0:
            return out.stdout.strip()
    return None


def code_changes(root: Path, a: str, b: str) -> list[str]:
    """Files under CODE_PATHS that differ between commits a and b."""
    out = _git(root, "diff", "--name-only", a, b, "--", *CODE_PATHS)
    if out.returncode != 0:
        raise RuntimeError(f"git diff {a} {b}: {out.stderr.strip()}")
    return [line for line in out.stdout.splitlines() if line]


def code_mismatch(root: Path, head: str, commit: str, ref: str, system_id: str,
                  changed: list[str]) -> str:
    """Why a run at `ref` (`commit`) is refused: what it changes besides its
    system file, and what to do about it. Most often the ref is a branch made
    from the base branch's tip (dev) while the deployment runs an older
    pinned commit: the branch changes no code itself, its base does."""
    files = ", ".join(changed[:10]) + (f" and {len(changed) - 10} more" if len(changed) > 10 else "")
    why = (f"ref {ref} ({commit[:12]}) and this deployment's workspace ({head[:12]}) differ in "
           f"code a run reads: {files}. A run at a saved ref takes only systems/{system_id}.yaml "
           f"from it and runs the workspace's code, so it would not run as in CI. ")
    base = os.environ.get("VHIL_BASE_BRANCH", "dev")
    # Where the editor's saves start without a base (gitstore.GitStore.base_commit).
    tip = resolve_ref(root, f"refs/remotes/origin/{base}") or resolve_ref(root, base)
    fork =_git(root, "merge-base", commit, tip).stdout.strip() if tip else ""
    if fork and fork != head and not code_changes(root, fork, commit):
        return why + (f"{ref} changes no code itself: it is built on {base} at {fork[:12]}, "
                      f"not on the workspace's {head[:12]}. Save the system to a new branch "
                      f"from the editor, which builds on the commit the system was opened at, "
                      f"or run {ref} once the deployment is at that code.")
    return why + f"{ref} changes that code itself: run it where it is checked out (CI)."


def system_at(root: Path, commit: str, system_id: str) -> Optional[str]:
    out = _git(root, "cat-file", "blob", f"{commit}:systems/{system_id}.yaml")
    return out.stdout if out.returncode == 0 else None


def materialise_system(root: Path, commit: str, system_id: str, dest: Path) -> Path:
    """systems/<id>.yaml as of `commit`, written to dest/<id>.yaml (the file
    name is its id, as System requires)."""
    text = system_at(root, commit, system_id)
    if text is None:
        raise RuntimeError(f"no systems/{system_id}.yaml at {commit[:12]}")
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{system_id}.yaml"
    path.write_text(text)
    return path


def workspace_refs(root: Path) -> dict:
    """{head, branches, tags}: what a run can be started at."""
    out = _git(root, "for-each-ref", "--format=%(refname)",
               "refs/heads", "refs/remotes/origin", "refs/tags")
    branches, tags = set(), []
    for ref in out.stdout.splitlines() if out.returncode == 0 else []:
        if ref.startswith("refs/tags/"):
            tags.append(ref.removeprefix("refs/tags/"))
        elif ref != "refs/remotes/origin/HEAD":
            branches.add(ref.removeprefix("refs/heads/").removeprefix("refs/remotes/origin/"))
    head = _git(root, "rev-parse", "HEAD")
    return {"head": head.stdout.strip() if head.returncode == 0 else "",
            "branches": sorted(branches), "tags": sorted(tags, reverse=True)}


# -- the tests a pytest scenario can select -------------------------------------

class TestCatalog:
    """The test files and node ids under `root` of the workspace, for the
    start form's pytest picker: what `pytest --collect-only -q` lists, run
    once and cached until a .py file under `root` (or the tree's conftest)
    changes. If collection fails the files are still listed, with the error."""

    def __init__(self, workspace: Path, root: str = "tests/sim", timeout_s: float = 120):
        self.workspace, self.root, self.timeout_s = Path(workspace), root, timeout_s
        self._lock = threading.Lock()
        self._cached: Optional[tuple[tuple, dict]] = None

    def _sources(self) -> list[Path]:
        tree = self.workspace / self.root
        extra = [self.workspace / n for n in ("tests/conftest.py", "pytest.ini")]
        return sorted(tree.rglob("*.py")) + [p for p in extra if p.is_file()]

    def _signature(self) -> tuple:
        return tuple((str(p), st.st_mtime_ns, st.st_size)
                     for p in self._sources() for st in (p.stat(),))

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.workspace))
                      for p in (self.workspace / self.root).rglob("test_*.py"))

    def collect(self) -> dict:
        with self._lock:
            sig = self._signature()
            if self._cached and self._cached[0] == sig:
                return self._cached[1]
            out: dict = {"root": self.root, "files": self.files(), "tests": []}
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                     self.root], cwd=self.workspace, capture_output=True, text=True,
                    timeout=self.timeout_s)
                out["tests"] = [line.strip() for line in proc.stdout.splitlines()
                                if _SELECT.match(line.strip()) and "::" in line]
                # 5 = nothing collected: an empty tree, not an error.
                if proc.returncode not in (0, 5):
                    tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
                    out["error"] = f"pytest --collect-only exited {proc.returncode}: " + " / ".join(tail)
            except (OSError, subprocess.TimeoutExpired) as e:
                out["error"] = f"pytest --collect-only: {e}"
            self._cached = (sig, out)
            return out


def pickers_router(workspace) -> APIRouter:
    """The start form's pickers: GET /api/tests (TestCatalog) and
    GET /api/workspace/refs (workspace_refs)."""
    catalog = TestCatalog(workspace.root)
    r = APIRouter(tags=["runs"])

    @r.get("/api/tests")
    def tests():
        return catalog.collect()

    @r.get("/api/workspace/refs")
    def refs():
        return workspace_refs(workspace.root)

    return r


# -- the API -------------------------------------------------------------------

def check_limits(req: RunRequest, limits: Limits) -> list[str]:
    """What in the request is over the per-run limits (Limits)."""
    sc, errors = req.scenario, []
    if isinstance(sc, RunScenario):
        if sc.virtual_ms > limits.max_virtual_ms:
            errors.append(f"virtual_ms: {sc.virtual_ms} is over this server's limit of "
                          f"{limits.max_virtual_ms}")
        if len(sc.stimuli) > limits.max_stimuli:
            errors.append(f"stimuli: {len(sc.stimuli)} is over the limit of {limits.max_stimuli}")
        if len(sc.watch) > limits.max_watch:
            errors.append(f"watch: {len(sc.watch)} is over the limit of {limits.max_watch}")
        if len(sc.expect) > limits.max_expect:
            errors.append(f"expect: {len(sc.expect)} is over the limit of {limits.max_expect}")
    return errors


def router(settings, workspace, limits: Optional[Limits] = None) -> APIRouter:
    """/api/runs over settings.db and settings.results."""
    from vhil.server.workspace import NotFound

    limits = limits or Limits.from_env()
    store = RunStore(settings.db)
    results = Path(settings.results)
    r = APIRouter(prefix="/api/runs", tags=["runs"])

    def run_or_404(run_id: int) -> dict:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        return run

    def login(request: Request) -> str:
        user = getattr(request.state, "user", None) or {}
        return user.get("login") or ""

    def may_cancel(request: Request, run: dict) -> bool:
        """Its owner or an admin (VHIL_ADMINS); anyone in dev mode. A run
        from before owners were recorded (owner "") is an admin's."""
        if settings.auth == "dev":
            return True
        who = login(request)
        return bool(who) and (who.lower() == run.get("owner", "").lower()
                              or settings.is_admin(who))

    def shown(request: Request, run: dict) -> dict:
        return {**run, "can_cancel": run["state"] not in TERMINAL and may_cancel(request, run)}

    @r.post("", status_code=201)
    def create(req: RunRequest, request: Request):
        over = check_limits(req, limits)
        if over:
            raise HTTPException(422, over)
        head = workspace.ref()
        commit = head
        if req.ref:
            if not _REF.match(req.ref):
                raise HTTPException(422, f"ref {req.ref!r} is not a plain git ref")
            commit = resolve_ref(workspace.root, req.ref) if head else None
            if not commit:
                raise HTTPException(422, f"no ref '{req.ref}' in the workspace "
                                         f"({'no git' if not head else 'not a branch, tag or commit'})")
        with tempfile.TemporaryDirectory(prefix="vhil-run-") as tmp:
            if commit and commit != head:
                # Runs at a saved ref (above): the ref's system file, this tree's code.
                if isinstance(req.scenario, PytestScenario):
                    raise HTTPException(422, f"a pytest scenario runs the workspace's tests and "
                                             f"systems as checked out: ref {req.ref} is not HEAD")
                changed = code_changes(workspace.root, head, commit)
                if changed:
                    raise HTTPException(422, code_mismatch(workspace.root, head, commit, req.ref,
                                                           req.system, changed))
                try:
                    path = materialise_system(workspace.root, commit, req.system, Path(tmp))
                except RuntimeError:
                    raise HTTPException(422, f"no system '{req.system}' at {req.ref}")
            else:
                try:
                    path = workspace.system_path(req.system)
                except NotFound:
                    raise HTTPException(422, f"no system '{req.system}' in the workspace")
            try:
                system = System(path)
            except SystemError as e:
                raise HTTPException(422, f"system '{req.system}' does not validate: {e}")
        errors = check_against_system(req, system, workspace.root)
        if errors:
            raise HTTPException(422, errors)
        try:
            run_id = store.create(req.system, commit, req.firmware, req.scenario.model_dump(),
                                  ref_name=req.ref or "", owner=login(request),
                                  max_active=limits.max_active,
                                  max_active_per_user=limits.max_active_per_user)
        except QueueFull as e:
            raise HTTPException(429, str(e))
        return {"run_id": run_id}

    @r.get("")
    def history(request: Request, limit: int = Query(100, ge=1, le=1000),
                state: Optional[str] = None, system: Optional[str] = None):
        if state is not None and state not in STATES:
            raise HTTPException(422, f"unknown state '{state}'")
        return [shown(request, run) for run in store.list(limit, state, system)]

    @r.get("/{run_id}")
    def get(run_id: int, request: Request):
        return shown(request, run_or_404(run_id))

    @r.post("/{run_id}/cancel")
    def cancel(run_id: int, request: Request):
        run = run_or_404(run_id)
        if not may_cancel(request, run):
            raise HTTPException(403, f"run {run_id} is {run.get('owner') or 'nobody'}'s: only its "
                                     f"owner or an admin may cancel it")
        if run.get("owner", "").lower() != login(request).lower():
            log.warning("run %s (owner %r) cancelled by admin %r", run_id, run.get("owner"),
                        login(request))
        return shown(request, store.cancel(run_id))

    @r.get("/{run_id}/trace")
    def trace(run_id: int, since_us: int = Query(0, ge=0), kinds: Optional[str] = None,
              limit: int = Query(500_000, ge=1, le=5_000_000), cursor: Optional[str] = None):
        """A page of trace records (a JSON list); the X-Trace-Cursor header
        is the cursor of the next page. Each page costs what it returns:
        pass the cursor back instead of moving since_us."""
        run = run_or_404(run_id)
        path = results / str(run_id) / TRACE
        lines, nxt = trace_page(path, parse_cursor(cursor, path), since_us, _kinds(kinds), limit,
                                token=run.get("token") or None)
        # The lines are already JSON: no parse-and-re-encode of the page.
        return Response(b"[" + b",".join(lines) + b"]", media_type="application/json",
                        headers={CURSOR_HEADER: str(nxt)})

    @r.get("/{run_id}/artifacts")
    def artifacts(run_id: int):
        run = run_or_404(run_id)
        root = results / str(run_id)
        if not root.is_dir() or not owns_results(results, run):
            return []
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    @r.get("/{run_id}/artifacts/{name:path}")
    def artifact(run_id: int, name: str):
        run = run_or_404(run_id)
        root = (results / str(run_id)).resolve()
        path = (root / name).resolve()
        if name.startswith("/") or not path.is_relative_to(root) or not path.is_file() \
                or not owns_results(results, run):
            raise HTTPException(404, f"no artifact '{name}'")
        return artifact_response(path)

    @r.websocket("/{run_id}/live")
    async def live(ws: WebSocket, run_id: int, kinds: Optional[str] = None):
        await ws.accept()
        run = await asyncio.to_thread(store.get, run_id)
        try:
            wanted = _kinds(kinds)
        except HTTPException as e:
            await ws.close(code=4422, reason=str(e.detail)[:120])
            return
        if run is None:
            await ws.close(code=4404, reason=f"no run {run_id}")
            return
        path = results / str(run_id) / TRACE
        token = run.get("token") or None
        # offset: where the next read starts in the file `inode`; None until
        # that file is this run's (trace_start). A stale trace left at a
        # reused id's path is never read, however long the run stays queued.
        tail = {"offset": None, "inode": None}

        def read_more() -> tuple[bool, bytes]:
            """(whether the file was replaced, what it gained since the last read)."""
            try:
                f = open(path, "rb")
            except FileNotFoundError:
                return False, b""
            with f:
                st = os.fstat(f.fileno())
                new = st.st_ino != tail["inode"] or (tail["offset"] is not None
                                                     and st.st_size < tail["offset"])
                if new:
                    # The worker set a stale trace aside, or a reclaimed
                    # run's next attempt started a new one (the last is kept
                    # as trace.attempt<N>.jsonl).
                    tail.update(offset=None, inode=st.st_ino)
                if tail["offset"] is None:
                    tail["offset"] = trace_start(f, token)
                    if tail["offset"] is None:
                        return new, b""
                f.seek(tail["offset"])
                chunk = f.read()
                tail["offset"] = f.tell()
            return new, chunk

        pending = b""
        try:
            while True:
                # Read the state before the file: once it is final, the
                # worker has written everything, so one more read drains it.
                state = await asyncio.to_thread(store.state, run_id)
                new, chunk = await asyncio.to_thread(read_more)
                if new:
                    pending = b""
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        if not line:
                            continue
                        if wanted and json.loads(line).get("kind") not in wanted:
                            continue
                        await ws.send_text(line.decode())
                if state in TERMINAL or state is None:
                    await ws.send_json({"kind": "end", "state": state})
                    await ws.close()
                    return
                await asyncio.sleep(0.2)
        except WebSocketDisconnect:
            return

    return r
