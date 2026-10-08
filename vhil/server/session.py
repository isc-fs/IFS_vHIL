"""Live sessions: the session channel (step 14 of
docs/architecture/editor-workspace.md, feature 3; docs/live-session.md).

A live session is a normal run (`POST /api/runs`) whose scenario says
`live: true`: the worker paces it to wall time, open-ended up to
Limits.max_live_ms, and applies ops to it as it goes. The ops come over

    WS /api/runs/{id}/session

behind the app's auth guard (a session, same origin; vhil/server/auth.py),
and each is exactly a scenario stimulus (docs/scenarios.md: can_send,
can_periodic, stop_periodic, gpio, analog, watch) or a control (pause,
resume, stop, keepalive). One connection holds the session's control (a
lease in the DB, renewed while it is open): the run's owner or an admin, and
in github mode only after its `hello` carried the session's CSRF token. Every
other connection, the owner's second tab included, watches: it sees the acks
and who holds control, and may take control once the holder lets it go.

The API and the workers share only the database and the runs volume, so an
op goes through the `session_ops` table: the API inserts it (pending), the
worker takes the pending ops at its next slice boundary, applies each at the
end of the slice it is about to run (virtual time, deterministically: the
same scheduling a scenario row at that time gets), and settles the row as
applied (with that time) or refused (with why). The worker also writes each
op into the trace, `{kind: "op", t_us, op_id, op, status}`, so a recorded
session is in its trace and replays exactly as a scenario
(`GET /api/runs/{id}/session/scenario`), and a `{kind: "clock"}` record per
slice with the real-time factor.

Messages (JSON), client to server:

    {kind: "hello", csrf?, control?: true}      first; control: false to only watch
    {kind: "op", cid?, op: {kind: <stimulus or control>, ...}}
    {kind: "take"}                               take control if it is free

server to client:

    {kind: "hello", run, live, state, role: "control" | "view", holder, limits}
    {kind: "queued", cid, op_id}                 the op is in; the worker applies it next
    {kind: "refused", cid, detail}               refused here (shape, system, rate, role)
    {kind: "ack", op_id, op, status: "applied" | "refused", at_us, detail, login}
    {kind: "control", role, holder}              control changed hands
    {kind: "end", state}                         the run ended; the socket closes
"""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from vhil.server.runs import (CONTRACT_VERSION, TERMINAL, TRACE, CanPeriodic, Limits, RunScenario,
                              RunStore, Stimulus, check_scenario, read_trace, system_at)
from vhil.system import System, SystemError

STIMULI = ("can_send", "can_periodic", "stop_periodic", "gpio", "analog", "watch")
CONTROLS = ("pause", "resume", "stop", "keepalive")
# A board's debugger (docs/debugger.md; vhil/gdb.py check_op): not a
# stimulus, never in a session's recording.
DEBUG = "debug"
# A settled op's result (a debug query's frames, locals...) in the database.
MAX_RESULT = 64 << 10
# The control lease: how long it holds without a renewal, and how often an
# open connection renews it.
LEASE_S = 15.0
RENEW_S = 5.0
# How often a connection looks for settled ops and the run's end.
POLL_S = 0.05

_STIMULUS = TypeAdapter(Stimulus)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_ops (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run     INTEGER NOT NULL,
    op      TEXT NOT NULL,
    login   TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    state   TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','applied','refused')),
    at_us   INTEGER,
    detail  TEXT NOT NULL DEFAULT '',
    result  TEXT
);
CREATE INDEX IF NOT EXISTS session_ops_run ON session_ops (run, state, id);
CREATE TABLE IF NOT EXISTS session_control (
    run     INTEGER PRIMARY KEY,
    holder  TEXT NOT NULL,
    login   TEXT NOT NULL DEFAULT '',
    expires REAL NOT NULL
);
"""


class SessionStore(RunStore):
    """The runs table plus a live session's ops and its control lease."""

    def __init__(self, path: Path):
        super().__init__(path)
        with self._connect() as db:
            db.executescript(_SCHEMA)
            # A debug op's result (docs/debugger.md): a column added to a
            # database from before the debugger.
            if "result" not in {r[1] for r in db.execute("PRAGMA table_info(session_ops)")}:
                db.execute("ALTER TABLE session_ops ADD COLUMN result TEXT")

    def _tx(self, fn):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                out = fn(db)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()
        return out

    def _all(self, sql: str, args=()) -> list[dict]:
        db = self._connect()
        try:
            return [dict(r) for r in db.execute(sql, args).fetchall()]
        finally:
            db.close()

    # -- ops ----------------------------------------------------------------------

    def add_op(self, run_id: int, op: dict, login: str = "", now: Optional[float] = None) -> int:
        return self._tx(lambda db: db.execute(
            "INSERT INTO session_ops (run, op, login, created) VALUES (?, ?, ?, ?) RETURNING id",
            (run_id, json.dumps(op), login, time.time() if now is None else now)).fetchone()[0])

    def pending(self, run_id: int) -> list[dict]:
        """The run's ops the worker hasn't taken, in the order they came."""
        return [{**r, "op": json.loads(r["op"])} for r in self._all(
            "SELECT id, op, login FROM session_ops WHERE run = ? AND state = 'pending' "
            "ORDER BY id", (run_id,))]

    def settle(self, run_id: int, settled: list[tuple]) -> None:
        """[(op id, applied | refused, at_us, detail[, result])], in one
        write; a debug op's result is kept as JSON, up to MAX_RESULT bytes."""
        def row(s):
            op_id, st, at, detail = s[:4]
            result = s[4] if len(s) > 4 else None
            text = None if result is None else json.dumps(result, separators=(",", ":"))
            if text is not None and len(text) > MAX_RESULT:
                text = json.dumps({"error": f"the result is over {MAX_RESULT >> 10} KiB"})
            return st, at, detail, text, op_id, run_id
        if settled:
            self._tx(lambda db: db.executemany(
                "UPDATE session_ops SET state = ?, at_us = ?, detail = ?, result = ? "
                "WHERE id = ? AND run = ? AND state = 'pending'",
                [row(s) for s in settled]))

    def settled_after(self, run_id: int, after: int) -> list[dict]:
        """Settled ops with an id past `after`, in order, up to the first
        still pending: the worker settles them in that order."""
        rows = self._all("SELECT * FROM session_ops WHERE run = ? AND id > ? ORDER BY id",
                         (run_id, after))
        out = []
        for r in rows:
            if r["state"] == "pending":
                break
            out.append({**r, "op": json.loads(r["op"]),
                        "result": json.loads(r["result"]) if r.get("result") else None})
        return out

    def ops(self, run_id: int) -> list[dict]:
        return [{**r, "op": json.loads(r["op"])} for r in self._all(
            "SELECT * FROM session_ops WHERE run = ? ORDER BY id", (run_id,))]

    def last_activity(self, run_id: int) -> Optional[float]:
        """When the run's last op came (any op, keepalive too); None if none."""
        rows = self._all("SELECT max(created) AS t FROM session_ops WHERE run = ?", (run_id,))
        return rows[0]["t"] if rows else None

    # -- control ------------------------------------------------------------------

    def acquire(self, run_id: int, holder: str, login: str, now: Optional[float] = None,
                lease_s: float = LEASE_S) -> tuple[bool, str]:
        """Take or renew the run's control for `holder`, when it is free, has
        lapsed or is `holder`'s already. (whether it holds it, the holder's
        login)."""
        now = time.time() if now is None else now

        def go(db: sqlite3.Connection):
            row = db.execute("SELECT holder, login, expires FROM session_control WHERE run = ?",
                             (run_id,)).fetchone()
            if row is None or row["holder"] == holder or row["expires"] < now:
                db.execute("INSERT INTO session_control (run, holder, login, expires) "
                           "VALUES (?, ?, ?, ?) ON CONFLICT (run) DO UPDATE SET "
                           "holder = excluded.holder, login = excluded.login, "
                           "expires = excluded.expires", (run_id, holder, login, now + lease_s))
                return True, login
            return False, row["login"]
        return self._tx(go)

    def holder(self, run_id: int, now: Optional[float] = None) -> Optional[dict]:
        """The run's control lease ({holder, login, expires}) if it holds."""
        now = time.time() if now is None else now
        rows = self._all("SELECT holder, login, expires FROM session_control WHERE run = ?",
                         (run_id,))
        return rows[0] if rows and rows[0]["expires"] >= now else None

    def release(self, run_id: int, holder: str) -> None:
        self._tx(lambda db: db.execute(
            "DELETE FROM session_control WHERE run = ? AND holder = ?", (run_id, holder)))


# -- ops ------------------------------------------------------------------------------

class OpError(ValueError):
    """An op refused before it reaches the worker."""


def parse_op(op, system: System) -> dict:
    """An op as the worker takes it: a control ({kind}) or a stimulus checked
    as a scenario row is (RunScenario's models, then against the system).
    Its time is the worker's to give: an `at_ms` is dropped. Raises OpError."""
    if not isinstance(op, dict):
        raise OpError("an op is an object with a kind")
    kind = op.get("kind")
    if kind in CONTROLS:
        if set(op) != {"kind"}:
            raise OpError(f"{kind} takes nothing but its kind")
        return {"kind": kind}
    if kind == DEBUG:
        from vhil.gdb import DebugError, check_op
        try:
            return check_op(op, list(system.boards))
        except DebugError as e:
            raise OpError(str(e)) from None
    if kind not in STIMULI:
        raise OpError(f"unknown op kind {kind!r} (have "
                      f"{', '.join(STIMULI + CONTROLS + (DEBUG,))})")
    body = {k: v for k, v in op.items() if k != "at_ms"}
    try:
        stim = _STIMULUS.validate_python(body)
    except ValidationError as e:
        raise OpError("; ".join(err["msg"].removeprefix("Value error, ") +
                                (f" ({'.'.join(map(str, err['loc'][1:]))})"
                                 if len(err["loc"]) > 1 else "")
                                for err in e.errors())) from None
    if isinstance(stim, CanPeriodic):
        if stim.until_ms is not None:
            raise OpError("a live periodic runs until a stop_periodic: no until_ms")
        if stim.name is None:
            raise OpError("a live periodic needs a name, for the stop_periodic that ends it")
    errors = check_scenario(RunScenario.model_construct(stimuli=[stim], watch=[], expect=[]),
                            system)
    if errors:
        raise OpError("; ".join(e.removeprefix("stimuli[0]: ") for e in errors))
    out = stim.model_dump(exclude_none=True)
    out.pop("at_ms", None)
    if out.get("ext") is False:
        out.pop("ext")
    return out


class RateLimit:
    """A token bucket: `rate` ops a second, bursts of as many."""

    def __init__(self, rate: float, clock=time.monotonic):
        self.rate, self.clock = float(rate), clock
        self.tokens, self.at = float(rate), clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.rate, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


# -- a session as a scenario -------------------------------------------------------------

def session_ops(trace: list[dict]) -> list[dict]:
    """The stimuli a session applied, from its trace's op records, in order."""
    return [r for r in trace if r.get("kind") == "op" and r.get("status") == "applied"
            and (r.get("op") or {}).get("kind") in STIMULI]


def as_scenario(run: dict, trace: list[dict]) -> dict:
    """A live run's applied stimuli as a scenario (docs/scenarios.md): each
    at the virtual time the worker applied it, with the run's slice and its
    length, so running it gives the session's trace again."""
    sc = run.get("scenario") or {}
    stimuli = [{**r["op"], "at_ms": r["t_us"] / 1000} for r in session_ops(trace)]
    end_us = max([run.get("virtual_us") or 0] + [r["t_us"] for r in trace if "t_us" in r])
    doc = {"kind": "scenario", "system": run["system"],
           "description": f"Recorded from live session run {run['id']}"
                          + (f" by {run['owner']}" if run.get("owner") else "")
                          + (f" ({run['started'][:10]})" if run.get("started") else ""),
           "virtual_ms": max(1, math.ceil(end_us / 1000)),
           "slice_ms": int(sc.get("slice_ms") or 50),
           "stimuli": sc.get("stimuli", []) + stimuli}
    if sc.get("watch"):
        doc["watch"] = sc["watch"]
    return doc


# -- the API ------------------------------------------------------------------------------

def ack(row: dict) -> dict:
    """A settled op as the channel's `ack` (a debug op's with its result)."""
    out = {"kind": "ack", "op_id": row["id"], "op": row["op"], "status": row["state"],
           "at_us": row["at_us"], "detail": row["detail"], "login": row["login"]}
    if row.get("result") is not None:
        out["result"] = row["result"]
    return out


def router(settings, workspace, limits: Optional[Limits] = None) -> APIRouter:
    limits = limits or Limits.from_env()
    store = SessionStore(settings.db)
    results = Path(settings.results)
    r = APIRouter(prefix="/api/runs", tags=["session"])

    def may_control(user: Optional[dict], run: dict) -> bool:
        """The run's owner or an admin; anyone in dev mode (as cancel)."""
        if settings.auth == "dev":
            return True
        who = (user or {}).get("login") or ""
        return bool(who) and (who.lower() == (run.get("owner") or "").lower()
                              or settings.is_admin(who))

    def csrf_ok(ws: WebSocket, token) -> bool:
        if settings.auth == "dev":
            return True
        auth = getattr(ws.app.state, "auth", None)
        session = auth.session(ws) if auth else None
        return bool(session) and isinstance(token, str) \
            and hmac.compare_digest(token, auth.csrf_token(session))

    def system_of(run: dict) -> System:
        """The run's system as the worker runs it: at its commit if it has one."""
        if run.get("ref"):
            text = system_at(workspace.root, run["ref"], run["system"])
            if text is not None:
                with tempfile.TemporaryDirectory(prefix="vhil-session-") as tmp:
                    path = Path(tmp) / f"{run['system']}.yaml"
                    path.write_text(text)
                    return System(path)
        return System(workspace.system_path(run["system"]))

    @r.get("/{run_id}/session/scenario")
    def scenario(run_id: int):
        """The session's applied stimuli as a scenario file's data (for the
        editor's "Save session as scenario"; the Commit flow saves it)."""
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        if not (run.get("scenario") or {}).get("live"):
            raise HTTPException(422, f"run {run_id} is not a live session")
        trace = read_trace(results / str(run_id) / TRACE, kinds={"op", "clock"},
                           token=run.get("token") or None)
        ops = session_ops(trace)
        return {"scenario": as_scenario(run, trace), "ops": len(ops),
                "final": run["state"] in TERMINAL}

    @r.websocket("/{run_id}/session")
    async def session(ws: WebSocket, run_id: int):
        await ws.accept()
        run = await asyncio.to_thread(store.get, run_id)
        if run is None:
            await ws.close(code=4404, reason=f"no run {run_id}")
            return
        user = getattr(ws.state, "user", None) or {}
        login = user.get("login") or ""
        live = bool((run.get("scenario") or {}).get("live"))
        me = uuid.uuid4().hex
        state = {"role": "view", "allowed": False, "renewed": 0.0, "acked": 0,
                 "holder": None}
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=30)
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await ws.close(code=4400, reason="expected a hello")
            return
        if not isinstance(hello, dict) or hello.get("kind") != "hello":
            await ws.close(code=4400, reason="expected a hello")
            return
        state["allowed"] = (live and run["state"] not in TERMINAL and may_control(user, run)
                            and csrf_ok(ws, hello.get("csrf")))
        rate = RateLimit(limits.live_ops_per_s)
        system: Optional[System] = None

        async def take() -> None:
            if not state["allowed"]:
                return
            held, who = await asyncio.to_thread(store.acquire, run_id, me, login)
            state["renewed"] = time.monotonic()
            role = "control" if held else "view"
            if (role, who) != (state["role"], state["holder"]):
                state["role"], state["holder"] = role, who
                await ws.send_json({"kind": "control", "role": role, "holder": who})

        if hello.get("control", True):
            held, who = (await asyncio.to_thread(store.acquire, run_id, me, login)) \
                if state["allowed"] else (False, None)
            state["renewed"] = time.monotonic()
            state["role"], state["holder"] = ("control" if held else "view"), who
        if state["holder"] is None:
            lease = await asyncio.to_thread(store.holder, run_id)
            state["holder"] = lease["login"] if lease else None
        await ws.send_json({
            "kind": "hello", "contract": CONTRACT_VERSION, "run": run_id, "live": live,
            "state": run["state"],
            "role": state["role"], "holder": state["holder"], "may_control": state["allowed"],
            "limits": {"ops_per_s": limits.live_ops_per_s,
                       "max_periodic": limits.live_max_periodic,
                       "idle_s": limits.live_idle_s,
                       "max_ops": limits.max_stimuli},
            "slice_ms": (run.get("scenario") or {}).get("slice_ms")})

        async def on_message(msg) -> None:
            nonlocal system
            if not isinstance(msg, dict):
                return
            if msg.get("kind") == "take":
                await take()
                return
            if msg.get("kind") != "op":
                await ws.send_json({"kind": "refused", "cid": None,
                                    "detail": f"unknown message kind {msg.get('kind')!r}"})
                return
            cid = msg.get("cid")
            cid = cid if isinstance(cid, (str, int)) and len(str(cid)) <= 64 else None

            async def refuse(detail: str) -> None:
                await ws.send_json({"kind": "refused", "cid": cid, "detail": detail})

            if state["role"] != "control":
                return await refuse("this connection watches: another holds the session's "
                                    "control" if state["allowed"] else
                                    "only the run's owner or an admin controls its session")
            if not rate.take():
                return await refuse(f"over {limits.live_ops_per_s} ops a second "
                                    "(VHIL_LIVE_OPS_PER_S)")
            try:
                if system is None:
                    system = await asyncio.to_thread(system_of, run)
                op = parse_op(msg.get("op"), system)
            except (OpError, SystemError) as e:
                return await refuse(str(e))
            except Exception as e:  # noqa: BLE001 - the system file went missing, say so
                return await refuse(f"the run's system: {e}")
            if op["kind"] in STIMULI:
                past = await asyncio.to_thread(store.ops, run_id)
                done = [p for p in past if p["state"] != "refused" and p["op"]["kind"] in STIMULI]
                if len(done) >= limits.max_stimuli:
                    return await refuse(f"the session has {len(done)} ops, this server's limit "
                                        "(VHIL_MAX_STIMULI): its recording must stay a scenario")
                if op["kind"] == "can_periodic" and any(
                        p["op"]["kind"] == "can_periodic" and p["op"].get("name") == op["name"]
                        for p in done):
                    return await refuse(f"a periodic named '{op['name']}' ran in this session "
                                        "already: name it anew")
            op_id = await asyncio.to_thread(store.add_op, run_id, op, login)
            await ws.send_json({"kind": "queued", "cid": cid, "op_id": op_id})

        inbox: asyncio.Queue = asyncio.Queue()

        async def receive() -> None:
            try:
                while True:
                    try:
                        await inbox.put(await ws.receive_json())
                    except ValueError:
                        await inbox.put(None)       # not JSON: ignored
            except (WebSocketDisconnect, RuntimeError):
                await inbox.put(StopAsyncIteration)

        reader = asyncio.create_task(receive())
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(inbox.get(), timeout=POLL_S)
                except asyncio.TimeoutError:
                    msg = None
                if msg is StopAsyncIteration:
                    return
                if msg is not None:
                    await on_message(msg)
                    continue
                for row in await asyncio.to_thread(store.settled_after, run_id, state["acked"]):
                    state["acked"] = row["id"]
                    await ws.send_json(ack(row))
                if state["role"] == "control" and time.monotonic() - state["renewed"] > RENEW_S:
                    await take()
                elif state["role"] == "view" and time.monotonic() - state["renewed"] > RENEW_S:
                    state["renewed"] = time.monotonic()
                    lease = await asyncio.to_thread(store.holder, run_id)
                    who = lease["login"] if lease else None
                    if who != state["holder"]:
                        state["holder"] = who
                        await ws.send_json({"kind": "control", "role": "view", "holder": who})
                st = await asyncio.to_thread(store.state, run_id)
                if st in TERMINAL or st is None:
                    # Drain the last acks, then say so.
                    for row in await asyncio.to_thread(store.settled_after, run_id,
                                                       state["acked"]):
                        state["acked"] = row["id"]
                        await ws.send_json(ack(row))
                    await ws.send_json({"kind": "end", "state": st})
                    await ws.close()
                    return
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            reader.cancel()
            if state["role"] == "control":
                # Not awaited: a cancelled handler (the server stopping, the
                # client gone mid-send) must still let control go, at once
                # rather than when the lease lapses.
                store.release(run_id, me)

    return r
