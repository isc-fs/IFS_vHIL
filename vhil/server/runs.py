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
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vhil.system import System, SystemError

STATES = ("queued", "running", "passed", "failed", "error", "cancelled")
TERMINAL = frozenset({"passed", "failed", "error", "cancelled"})
TRACE_KINDS = frozenset({"frame", "edge", "sample", "log"})
TRACE = "trace.jsonl"
# A held run's heartbeat period, how stale it may get before another worker
# reclaims the run, and how many times a run is started before a lost worker
# ends it as error instead.
HEARTBEAT_S = 10.0
RECLAIM_AFTER_S = 60.0
MAX_ATTEMPTS = 2

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
    worker     TEXT,
    heartbeat  REAL,
    attempts   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_state ON runs (state, id);
"""
# Columns added after the first release: a database created before them
# gets them on open.
_ADDED = {"heartbeat": "REAL", "attempts": "INTEGER NOT NULL DEFAULT 0"}


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


def trace_page(path: Path, cursor: int = 0, since_us: int = 0, kinds: Optional[set] = None,
               limit: Optional[int] = None) -> tuple[list[bytes], int]:
    """(the matching records as their JSON lines, the cursor after them),
    reading from byte offset `cursor`. Records before since_us or of other
    kinds are skipped (and consumed). A line the worker is still writing is
    left for the next page, so a cursor also resumes a live trace."""
    out: list[bytes] = []
    if not path.is_file():
        return out, cursor
    pos = cursor
    with open(path, "rb") as f:
        f.seek(cursor)
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
               limit: Optional[int] = None) -> list[dict]:
    return [json.loads(line) for line in trace_page(path, 0, since_us, kinds, limit)[0]]


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


def tests_router(workspace) -> APIRouter:
    """GET /api/tests: the start form's pytest picker (TestCatalog)."""
    catalog = TestCatalog(workspace.root)
    r = APIRouter(prefix="/api/tests", tags=["runs"])

    @r.get("")
    def tests():
        return catalog.collect()

    return r


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
              limit: int = Query(500_000, ge=1, le=5_000_000), cursor: Optional[str] = None):
        """A page of trace records (a JSON list); the X-Trace-Cursor header
        is the cursor of the next page. Each page costs what it returns:
        pass the cursor back instead of moving since_us."""
        run_or_404(run_id)
        path = results / str(run_id) / TRACE
        lines, nxt = trace_page(path, parse_cursor(cursor, path), since_us, _kinds(kinds), limit)
        # The lines are already JSON: no parse-and-re-encode of the page.
        return Response(b"[" + b",".join(lines) + b"]", media_type="application/json",
                        headers={CURSOR_HEADER: str(nxt)})

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
                    if path.stat().st_size < offset:
                        # A reclaimed run's next attempt starts a new file
                        # (the last one is kept as trace.attempt<N>.jsonl).
                        offset, pending = 0, ""
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
