"""Runs (M5.2, #114): the queue and history table, and the /api/runs endpoints.

The `runs` table in SQLite (WAL) is both the job queue and the run history
(docs/architecture/m5-web-app.md). The API inserts a row in state `queued`;
a worker (vhil/worker.py) claims it with one atomic UPDATE, writes the run's
trace to `<results>/<id>/trace.jsonl` as virtual time advances, and sets the
final state. The live WebSocket tails that file, so a finished run replays
from exactly what it streamed.

    queued ─claim─▶ running ─▶ passed | failed | error
       └──────cancel──┴──────▶ cancelled   (the worker stops at its next slice)
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vhil.system import System, SystemError

STATES = ("queued", "running", "passed", "failed", "error", "cancelled")
TERMINAL = frozenset({"passed", "failed", "error", "cancelled"})
TRACE_KINDS = frozenset({"frame", "edge", "sample", "log"})
TRACE = "trace.jsonl"

# A git ref we pass to `git clone -b` and use in a directory name: no option
# look-alikes, no path climbing.
_REF = re.compile(r"^(?!-)(?!.*\.\.)[\w./-]{1,100}$")
_SELECT = re.compile(r"^tests/(?!.*\.\.)[\w/.-]+\.py(::[\w\[\]\-.,=]+)*$")
_HEX = re.compile(r"^([0-9a-fA-F]{2})*$")


# -- scenario models -------------------------------------------------------------

class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Can(_Model):
    at_ms: float = Field(0, ge=0)
    bus: str
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
    at_ms: float = Field(0, ge=0)
    board: str
    pin: str                # catalogue gpio name, e.g. "PB5"
    level: bool


class AnalogSet(_Model):
    kind: Literal["analog"]
    at_ms: float = Field(0, ge=0)
    board: str
    pin: str                # catalogue analog_in name, e.g. "PF7"
    volts: float = Field(ge=0, le=3.6)


Stimulus = Annotated[Union[CanSend, CanPeriodic, GpioSet, AnalogSet], Field(discriminator="kind")]


class SymbolWatch(_Model):
    kind: Literal["symbol"]
    board: str
    name: str = Field(pattern=r"^[A-Za-z_]\w{0,127}$")
    size: Literal[1, 2, 4] = 1
    period_ms: float = Field(10, ge=1)


class PinWatch(_Model):
    kind: Literal["pin"]
    board: str
    pin: str


Watch = Annotated[Union[SymbolWatch, PinWatch], Field(discriminator="kind")]


class RunScenario(_Model):
    kind: Literal["run"]
    virtual_ms: int = Field(ge=1, le=600_000)
    # Virtual time per slice: the trace is flushed and cancellation checked
    # after each one.
    slice_ms: int = Field(100, ge=10, le=1000)
    stimuli: list[Stimulus] = []
    watch: list[Watch] = []


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
    worker     TEXT
);
CREATE INDEX IF NOT EXISTS runs_state ON runs (state, id);
"""


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

    def create(self, system: str, ref: str, firmware: dict, scenario: dict) -> int:
        row = self._write(
            "INSERT INTO runs (state, system, ref, firmware, scenario, created) "
            "VALUES ('queued', ?, ?, ?, ?, ?) RETURNING id",
            (system, ref, json.dumps(firmware), json.dumps(scenario), now_iso()))
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

    def claim(self, worker: str) -> Optional[dict]:
        """The oldest queued run, now running for `worker`; None when the
        queue is empty. One statement under the write lock, so two workers
        never get the same row: the second one finds it no longer queued."""
        return self._write(
            "UPDATE runs SET state = 'running', started = ?, worker = ? "
            "WHERE id = (SELECT id FROM runs WHERE state = 'queued' ORDER BY id LIMIT 1) "
            "AND state = 'queued' RETURNING *", (now_iso(), worker))

    def progress(self, run_id: int, virtual_us: int) -> None:
        self._write("UPDATE runs SET virtual_us = ? WHERE id = ? AND state = 'running' RETURNING id",
                  (virtual_us, run_id))

    def state(self, run_id: int) -> Optional[str]:
        row = self._one("SELECT state FROM runs WHERE id = ?", (run_id,))
        return row["state"] if row else None

    def finish(self, run_id: int, state: str, virtual_us: int, summary: dict) -> str:
        """Set a running run's final state; returns the state it ended in. A
        run cancelled meanwhile stays cancelled (its results are kept)."""
        if state not in TERMINAL:
            raise ValueError(f"not a final state: {state}")
        row = self._write(
            "UPDATE runs SET state = ?, finished = ?, virtual_us = ?, summary = ? "
            "WHERE id = ? AND state = 'running' RETURNING state",
            (state, now_iso(), virtual_us, json.dumps(summary), run_id))
        if row:
            return row["state"]
        row = self._write("UPDATE runs SET virtual_us = ?, summary = ? "
                        "WHERE id = ? AND state = 'cancelled' RETURNING state",
                        (virtual_us, json.dumps(summary), run_id))
        return row["state"] if row else (self.state(run_id) or "")

    def cancel(self, run_id: int) -> Optional[dict]:
        """Queued or running -> cancelled; a finished run is left as it is."""
        self._write("UPDATE runs SET state = 'cancelled', finished = ? "
                  "WHERE id = ? AND state IN ('queued', 'running') RETURNING id",
                  (now_iso(), run_id))
        return self.get(run_id)


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("firmware", "scenario", "summary"):
        if key in d:
            d[key] = json.loads(d[key]) if d[key] else {}
    return d


# -- trace ---------------------------------------------------------------------

def read_trace(path: Path, since_us: int = 0, kinds: Optional[set] = None,
               limit: Optional[int] = None) -> list[dict]:
    out = []
    if not path.is_file():
        return out
    with open(path) as f:
        for line in f:
            if not line.endswith("\n"):
                break                     # a line the worker is still writing
            rec = json.loads(line)
            if rec.get("t_us", 0) < since_us or (kinds and rec.get("kind") not in kinds):
                continue
            out.append(rec)
            if limit is not None and len(out) >= limit:
                break
    return out


def _kinds(text: Optional[str]) -> Optional[set]:
    if not text:
        return None
    kinds = {k.strip() for k in text.split(",") if k.strip()}
    bad = kinds - TRACE_KINDS
    if bad:
        raise HTTPException(422, f"unknown trace kinds {sorted(bad)} (have {sorted(TRACE_KINDS)})")
    return kinds


# -- the API -------------------------------------------------------------------

def router(settings, workspace) -> APIRouter:
    """/api/runs over settings.db and settings.results."""
    from vhil.server.workspace import NotFound

    store = RunStore(settings.db)
    results = Path(settings.results)
    r = APIRouter(prefix="/api/runs", tags=["runs"])

    def run_or_404(run_id: int) -> dict:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        return run

    @r.post("", status_code=201)
    def create(req: RunRequest):
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
        # Workers run the workspace as checked out; a run at another ref of
        # the system file arrives with the git-backed editor (M5.4, #116).
        head = workspace.ref()
        if req.ref and not (head and head.startswith(req.ref)):
            raise HTTPException(422, f"ref {req.ref} is not the workspace's ({head or 'no git'})")
        run_id = store.create(req.system, head, req.firmware, req.scenario.model_dump())
        return {"run_id": run_id}

    @r.get("")
    def history(limit: int = Query(100, ge=1, le=1000), state: Optional[str] = None,
                system: Optional[str] = None):
        if state is not None and state not in STATES:
            raise HTTPException(422, f"unknown state '{state}'")
        return store.list(limit, state, system)

    @r.get("/{run_id}")
    def get(run_id: int):
        return run_or_404(run_id)

    @r.post("/{run_id}/cancel")
    def cancel(run_id: int):
        run_or_404(run_id)
        return store.cancel(run_id)

    @r.get("/{run_id}/trace")
    def trace(run_id: int, since_us: int = Query(0, ge=0), kinds: Optional[str] = None,
              limit: int = Query(500_000, ge=1, le=5_000_000)):
        run_or_404(run_id)
        return read_trace(results / str(run_id) / TRACE, since_us, _kinds(kinds), limit)

    @r.get("/{run_id}/artifacts")
    def artifacts(run_id: int):
        run_or_404(run_id)
        root = results / str(run_id)
        if not root.is_dir():
            return []
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    @r.get("/{run_id}/artifacts/{name:path}")
    def artifact(run_id: int, name: str):
        run_or_404(run_id)
        root = (results / str(run_id)).resolve()
        path = (root / name).resolve()
        if name.startswith("/") or not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, f"no artifact '{name}'")
        return FileResponse(path)

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
        offset, pending = 0, ""
        try:
            while True:
                # Read the state before the file: once it is final, the
                # worker has written everything, so one more read drains it.
                state = await asyncio.to_thread(store.state, run_id)
                if path.is_file():
                    with open(path) as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                    pending += chunk
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        if not line:
                            continue
                        if wanted and json.loads(line).get("kind") not in wanted:
                            continue
                        await ws.send_text(line)
                if state in TERMINAL or state is None:
                    await ws.send_json({"kind": "end", "state": state})
                    await ws.close()
                    return
                await asyncio.sleep(0.2)
        except WebSocketDisconnect:
            return

    return r
