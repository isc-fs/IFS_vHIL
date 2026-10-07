"""Debug a board of a running Sim with GDB (feature 4 of
docs/architecture/editor-workspace.md, step 16; docs/debugger.md).

    dbg = Debugger(sim, "ecu", workdir)
    dbg.start()                                  # attach; the board runs on
    dbg.break_insert({"function": "ecu::Controller::step"})
    sim.run_for(ms=50)    # in another thread: it blocks while the board is halted
    stop = dbg.wait_stop(timeout_s=30)           # {reason, frame, ...}
    dbg.locals(); dbg.registers(); dbg.evaluate("in.apps1_raw")
    dbg.resume("next")                           # or step, finish, continue

Each board gets Renode's own GDB stub (models/renode/VhilGdb.cs): on
127.0.0.1 only, and answering only the packets a debugger that looks needs,
never `qRcmd` (Renode monitor commands, which reach host files) nor memory
or register writes. The pinned toolchain's GDB (arm-none-eabi-gdb 14.2.Rel1,
or $VHIL_GDB) talks to it in MI mode as a subprocess, loading the board's
ELF (built with -g) with auto-loading and debuginfod off.

Nothing reaches GDB but the commands below, built from validated values:
a location is a function name, file:line or an address; an expression is a
C lvalue path (names, `.`, `->`, `[n]`, unary `*` and `&`), with no calls,
assignments, casts or `$` (convenience variables and functions such as
`$_shell`). Anything else raises DebugError before GDB sees it.

Lockstep: a halted CPU holds Renode's time source, so the whole emulation
stops with it. `emulation RunFor` blocks until the board runs again; every
other board stops at the next sync point (the system's time quantum), and
no firmware timeout fires while it is held. Renode's stub writes each halt's
virtual time into a file (the monitor is busy in RunFor), read by
halt_times().
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from vhil import renode as rn
from vhil.bench import _free_port
from vhil.renode import REPO

GDB_SOURCE = REPO / "models" / "renode" / "VhilGdb.cs"

# Limits on what one stop or query returns, so a trace record or a result
# stays small whatever the firmware's types.
MAX_FRAMES = 32
MAX_VARS = 64
MAX_VALUE = 512
MAX_WATCHES = 16
MAX_BREAKPOINTS = 16
DISASM_BEFORE, DISASM_AFTER = 32, 96     # bytes around the pc

RUN_CMDS = ("continue", "next", "step", "finish")
_MI_RUN = {"continue": "-exec-continue", "next": "-exec-next", "step": "-exec-step",
           "finish": "-exec-finish"}


class DebugError(ValueError):
    """A debug op refused, with why."""


# -- what may reach GDB ------------------------------------------------------------

_FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_~][A-Za-z0-9_]*)*")
_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+@-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.+@-]*)*")
_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*"
_EXPR = re.compile(rf"[*&]{{0,3}}{_NAME}(?:(?:\.|->)[A-Za-z_][A-Za-z0-9_]*|\[[0-9]{{1,6}}\])*")


def location(loc: Any) -> str:
    """A breakpoint location as GDB reads it, from {function} | {file, line}
    | {address}."""
    if not isinstance(loc, dict) or len(loc) not in (1, 2):
        raise DebugError("a location is {function}, {file, line} or {address}")
    if set(loc) == {"function"}:
        f = loc["function"]
        if not isinstance(f, str) or len(f) > 200 or not _FUNCTION.fullmatch(f):
            raise DebugError(f"not a function name: {f!r}")
        return f
    if set(loc) == {"file", "line"}:
        f, line = loc["file"], loc["line"]
        if not isinstance(f, str) or len(f) > 300 or not _FILE.fullmatch(f.lstrip("/")) \
                or "/../" in f"/{f}/" or "/./" in f"/{f}/":
            raise DebugError(f"not a source file: {f!r}")
        if isinstance(line, bool) or not isinstance(line, int) or not 0 < line < 10_000_000:
            raise DebugError(f"not a line number: {line!r}")
        return f"{f}:{line}"
    if set(loc) == {"address"}:
        a = loc["address"]
        if isinstance(a, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,8}", a):
            a = int(a, 16)
        if isinstance(a, bool) or not isinstance(a, int) or not 0 <= a < 1 << 32:
            raise DebugError(f"not an address: {a!r}")
        return f"*0x{a:08x}"
    raise DebugError("a location is {function}, {file, line} or {address}")


def expression(expr: Any) -> str:
    """An expression to read: a C lvalue path (module doc)."""
    if not isinstance(expr, str):
        raise DebugError("an expression is a string")
    text = expr.strip()
    if not text or len(text) > 200 or not _EXPR.fullmatch(text):
        raise DebugError(f"not a variable path (names, '.', '->', '[n]', '*', '&'): {expr!r}")
    return text


def frame_index(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < MAX_FRAMES:
        raise DebugError(f"not a frame: {value!r}")
    return value


def mi_quote(text: str) -> str:
    """An MI c-string (the value is already checked; this only escapes)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- MI output ------------------------------------------------------------------------

class _Parser:
    """GDB/MI output syntax (GDB manual, "GDB/MI Output Syntax")."""

    def __init__(self, text: str):
        self.s, self.i = text, 0

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def cstring(self) -> str:
        assert self.s[self.i] == '"'
        self.i += 1
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < len(self.s):
                n = self.s[self.i + 1]
                if n in "01234567" and re.match(r"[0-7]{3}", self.s[self.i + 1:self.i + 4]):
                    out.append(chr(int(self.s[self.i + 1:self.i + 4], 8)))
                    self.i += 4
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(n, n))
                self.i += 2
                continue
            if c == '"':
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1
        return "".join(out)

    def value(self):
        c = self.peek()
        if c == '"':
            return self.cstring()
        if c == "{":
            self.i += 1
            out: dict = {}
            while self.peek() not in ("}", ""):
                k, v = self.result()
                out[k] = v
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return out
        if c == "[":
            self.i += 1
            out_l: list = []
            while self.peek() not in ("]", ""):
                if self.peek() in '"{[':
                    out_l.append(self.value())
                else:
                    out_l.append(dict([self.result()]))
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return out_l
        raise ValueError(f"bad MI value at {self.i}: {self.s[self.i:self.i + 20]!r}")

    def result(self):
        m = re.compile(r"[A-Za-z0-9_-]+").match(self.s, self.i)
        if not m or self.s[m.end():m.end() + 1] != "=":
            raise ValueError(f"bad MI result at {self.i}: {self.s[self.i:self.i + 20]!r}")
        self.i = m.end() + 1
        return m.group(0), self.value()

    def results(self) -> dict:
        out = {}
        while self.peek() == ",":
            self.i += 1
            k, v = self.result()
            out[k] = v
        return out


def parse_line(line: str) -> Optional[dict]:
    """One MI output line as {type, token?, class?, results | text}; None for
    the prompt. type: result (^), exec (*), status (+), notify (=), console
    (~), target (@), log (&)."""
    line = line.rstrip("\r\n")
    if not line or line.strip() == "(gdb)":
        return None
    m = re.match(r"(\d*)([\^*+=~@&])", line)
    if not m:
        return {"type": "text", "text": line}
    token, sigil = m.group(1), m.group(2)
    rest = line[m.end():]
    kind = {"^": "result", "*": "exec", "+": "status", "=": "notify", "~": "console",
            "@": "target", "&": "log"}[sigil]
    if kind in ("console", "target", "log"):
        p = _Parser(rest)
        return {"type": kind, "text": p.cstring() if rest.startswith('"') else rest}
    cm = re.match(r"[A-Za-z0-9_-]+", rest)
    cls = cm.group(0) if cm else ""
    p = _Parser(rest[len(cls):])
    try:
        res = p.results()
    except (ValueError, AssertionError, IndexError):
        res = {"raw": rest[len(cls):]}
    out = {"type": kind, "class": cls, "results": res}
    if token:
        out["token"] = int(token)
    return out


def _clip(value) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_VALUE else text[:MAX_VALUE] + "…"


def frame_of(f: dict) -> dict:
    """A frame from MI's frame tuple: {level?, func, file, line, addr}."""
    out = {"func": f.get("func") or "??", "addr": f.get("addr", "")}
    if f.get("fullname") or f.get("file"):
        out["file"] = f.get("fullname") or f.get("file")
        out["line"] = int(f.get("line") or 0)
    if "level" in f:
        out["level"] = int(f["level"])
    return out


def find_gdb() -> str:
    """$VHIL_GDB, else the pinned toolchain's arm-none-eabi-gdb if it runs
    here, else gdb-multiarch."""
    if os.environ.get("VHIL_GDB"):
        return os.environ["VHIL_GDB"]
    for cand in ("arm-none-eabi-gdb", "gdb-multiarch"):
        path = shutil.which(cand)
        if path is None:
            continue
        try:
            subprocess.run([path, "--version"], capture_output=True, timeout=20, check=True)
        except (OSError, subprocess.SubprocessError):
            continue
        return path
    raise RuntimeError("no GDB: install the Arm GNU toolchain's arm-none-eabi-gdb (needs "
                       "libncurses5) or gdb-multiarch, or set VHIL_GDB")


# -- a board's debugger --------------------------------------------------------------

class Debugger:
    """GDB attached to one board of a started Sim (module doc). The board is
    `state` "running" or "stopped"; `stop` is the last stop's
    {reason, frame, bkpt?, signal?}."""

    def __init__(self, sim, board: str, workdir: Path, gdb: Optional[str] = None,
                 timeout_s: float = 30.0):
        if board not in sim.system.boards:
            raise DebugError(f"no board '{board}' in {sim.system.id}")
        self.sim, self.board = sim, board
        self.elf = Path(sim.firmware[board])
        self.workdir = Path(workdir)
        self.gdb_path = gdb or find_gdb()
        self.timeout_s = timeout_s
        self.halt_log = self.workdir / f"{rn.ident(board)}.halts"
        self.state = "detached"
        self.stop: Optional[dict] = None
        self.breakpoints: dict[int, dict] = {}
        self.watches: list[str] = []
        self._proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._token = 0
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._regnames: Optional[list[str]] = None

    # -- lifecycle --------------------------------------------------------------

    def start(self, resume: bool = True) -> None:
        """Start the board's stub (with the monitor free: between RunFors)
        and attach GDB. Attaching halts the board (before its next
        instruction, as a breakpoint does); with `resume` it runs on at once,
        else it stays stopped there, a stop like any other (`events`), for
        breakpoints to go in before it runs."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        if not getattr(self.sim, "_vhil_gdb_loaded", False):
            self.sim.monitor(f"include {rn.file_arg(GDB_SOURCE)}")
            self.sim._vhil_gdb_loaded = True
        port = _free_port()
        self.sim.monitor(f"machine StartVhilGdbServer {int(port)} "
                         f"{rn.quote(str(self.halt_log.resolve()))}", board=self.board)
        self._proc = subprocess.Popen(
            [self.gdb_path, "--interpreter=mi3", "-nx", "-nh", "-q",
             "-iex", "set auto-load off", "-iex", "set debuginfod enabled off",
             "-iex", "set confirm off", "-iex", "set pagination off",
             "-iex", "set width 0", "-iex", "set height 0", str(self.elf)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=self.workdir, start_new_session=True)
        threading.Thread(target=self._read, name=f"gdb-{self.board}", daemon=True).start()
        for setting in ("mi-async on", "print elements 32", "print repeats 8",
                        "print max-depth 2", "print pretty off", "print frame-arguments scalars",
                        "remotetimeout 10"):
            self._mi(f"-gdb-set {setting}")
        self._mi(f"-target-select remote 127.0.0.1:{int(port)}", timeout_s=60)
        self._drain()
        self.state = "stopped"
        self.stop = {**(self.stop or {}), "reason": "attached"}
        self._events = [{"event": "stopped", **self.stop}]
        if resume:
            self._mi("-exec-continue")
            self.state, self.stop = "running", None
            self._events = []       # attaching's own stop and resume

    def close(self) -> None:
        """Let the board go: every breakpoint deleted, running again, GDB
        gone. Safe while the board is halted in a RunFor (which then goes
        on), and when GDB already died."""
        if self._proc is None:
            return
        try:
            self._drain()
            if self.state == "stopped":
                if self.breakpoints:
                    self._mi("-break-delete", timeout_s=5)
                self._mi("-exec-continue", timeout_s=5)
        except Exception:  # noqa: BLE001 - GDB gone: closing the socket lets the CPU go
            pass
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._proc = None
        self.state = "detached"
        self.breakpoints.clear()

    # -- MI plumbing ------------------------------------------------------------

    def _read(self) -> None:
        proc = self._proc
        for line in proc.stdout:
            self._lines.put(line)
        self._lines.put("")     # EOF

    def _handle(self, rec: dict) -> None:
        if rec["type"] == "exec":
            if rec["class"] == "stopped":
                self.state = "stopped"
                r = rec["results"]
                stop = {"reason": r.get("reason", "signal-received" if r.get("signal-name")
                                       else "stopped")}
                if isinstance(r.get("frame"), dict):
                    stop["frame"] = frame_of(r["frame"])
                if r.get("bkptno"):
                    stop["bkpt"] = int(r["bkptno"])
                if r.get("signal-name"):
                    stop["signal"] = r["signal-name"]
                if r.get("return-value") is not None:
                    stop["return"] = _clip(r["return-value"])
                self.stop = stop
                self._events.append({"event": "stopped", **stop})
            elif rec["class"] == "running":
                self.state, self.stop = "running", None
                self._events.append({"event": "running"})

    def _next_line(self, timeout: float) -> Optional[str]:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line == "":
            raise DebugError(f"GDB for {self.board} exited")
        return line

    def _drain(self) -> None:
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                return
            if line == "":
                raise DebugError(f"GDB for {self.board} exited")
            rec = parse_line(line)
            if rec is not None:
                self._handle(rec)

    def _mi(self, command: str, timeout_s: Optional[float] = None) -> dict:
        """Send one MI command (built here, from checked values) and return
        its result record's results; DebugError on ^error."""
        if self._proc is None or self._proc.poll() is not None:
            raise DebugError(f"GDB for {self.board} is not running")
        if "\n" in command or "\r" in command:
            raise DebugError("an MI command is one line")
        with self._lock:
            self._token += 1
            token = self._token
            self._proc.stdin.write(f"{token}{command}\n")
            self._proc.stdin.flush()
            deadline = time.monotonic() + (timeout_s or self.timeout_s)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise DebugError(f"GDB did not answer {command.split()[0]} in time")
                line = self._next_line(min(left, 0.5))
                if line is None:
                    continue
                rec = parse_line(line)
                if rec is None:
                    continue
                if rec["type"] == "result" and rec.get("token") == token:
                    if rec["class"] == "error":
                        raise DebugError(str(rec["results"].get("msg", "GDB error")))
                    return rec["results"]
                self._handle(rec)

    def events(self) -> list[dict]:
        """The stops and resumes since the last call ({event, ...})."""
        self._drain()
        out, self._events = self._events, []
        return out

    def wait_stop(self, timeout_s: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            self._drain()
            if self.state == "stopped" and self.stop is not None:
                return self.stop
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{self.board} did not stop within {timeout_s} s")
            line = self._next_line(min(left, 0.2))
            if line is not None:
                rec = parse_line(line)
                if rec is not None:
                    self._handle(rec)

    def halt_times(self) -> list[tuple[int, str]]:
        """Every halt so far: (virtual us, Renode's reason)."""
        try:
            text = self.halt_log.read_text()
        except OSError:
            return []
        out = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                out.append((int(parts[0]), parts[1]))
        return out

    def _need_stopped(self) -> None:
        self._drain()
        if self.state != "stopped":
            raise DebugError(f"{self.board} is running: break first")

    # -- ops ----------------------------------------------------------------

    def break_insert(self, loc: dict) -> dict:
        """A breakpoint (the board stopped, or between RunFors with GDB's
        target running: GDB interrupts the board for it, mi-async)."""
        where = location(loc)
        if len(self.breakpoints) >= MAX_BREAKPOINTS:
            raise DebugError(f"{len(self.breakpoints)} breakpoints already, the limit")
        bkpt = self._mi(f"-break-insert {mi_quote(where)}").get("bkpt") or {}
        b = {"number": int(bkpt.get("number", 0)), "location": loc,
             "func": bkpt.get("func", ""), "addr": bkpt.get("addr", "")}
        if bkpt.get("fullname") or bkpt.get("file"):
            b["file"], b["line"] = bkpt.get("fullname") or bkpt.get("file"), int(bkpt.get("line", 0))
        self.breakpoints[b["number"]] = b
        return b

    def break_delete(self, number: Any) -> None:
        if isinstance(number, bool) or not isinstance(number, int) or number not in self.breakpoints:
            raise DebugError(f"no breakpoint {number!r}")
        self._mi(f"-break-delete {int(number)}")
        del self.breakpoints[number]

    def resume(self, how: str) -> None:
        """continue | next | step | finish, from a stop."""
        if how not in RUN_CMDS:
            raise DebugError(f"not a way to resume: {how!r}")
        self._need_stopped()
        self._mi(_MI_RUN[how])
        self.state, self.stop = "running", None

    def frames(self) -> list[dict]:
        self._need_stopped()
        r = self._mi(f"-stack-list-frames 0 {MAX_FRAMES - 1}")
        return [frame_of(f.get("frame", f)) for f in r.get("stack", [])][:MAX_FRAMES]

    def locals(self, frame: Any = 0) -> list[dict]:
        """Arguments and locals of a frame: {name, type, value?, arg?}."""
        self._need_stopped()
        n = frame_index(frame)
        r = self._mi(f"-stack-list-variables --thread 1 --frame {n} --simple-values")
        out = []
        for v in r.get("variables", [])[:MAX_VARS]:
            item = {"name": v.get("name", ""), "type": v.get("type", "")}
            if "value" in v:
                item["value"] = _clip(v["value"])
            if v.get("arg"):
                item["arg"] = True
            out.append(item)
        return out

    def registers(self) -> list[dict]:
        """The core registers (r0-r12, sp, lr, pc, xpsr) and the rest GDB
        names, in hex."""
        self._need_stopped()
        if self._regnames is None:
            self._regnames = self._mi("-data-list-register-names").get("register-names", [])
        r = self._mi("-data-list-register-values --skip-unavailable x")
        out = []
        for v in r.get("register-values", []):
            n = int(v.get("number", -1))
            name = self._regnames[n] if 0 <= n < len(self._regnames) else ""
            if name:
                out.append({"name": name, "value": _clip(v.get("value", ""))})
        return out[:48]

    def evaluate(self, expr: Any, frame: Any = 0) -> str:
        self._need_stopped()
        text = expression(expr)
        n = frame_index(frame)
        return _clip(self._mi(f"-data-evaluate-expression --thread 1 --frame {n} "
                              f"{mi_quote(text)}").get("value", ""))

    def set_watches(self, exprs: Any) -> list[str]:
        if not isinstance(exprs, list) or len(exprs) > MAX_WATCHES:
            raise DebugError(f"watches are a list of at most {MAX_WATCHES} expressions")
        self.watches = [expression(e) for e in exprs]
        return self.watches

    def watch_values(self) -> list[dict]:
        out = []
        for e in self.watches:
            try:
                out.append({"expr": e, "value": self.evaluate(e)})
            except DebugError as err:
                out.append({"expr": e, "error": _clip(str(err))})
        return out

    def disassemble(self, address: Any = None) -> list[dict]:
        """The instructions around `address` (default: the pc)."""
        self._need_stopped()
        if address is None:
            address = int((self.stop or {}).get("frame", {}).get("addr") or "0", 16)
        if isinstance(address, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,8}", address):
            address = int(address, 16)
        if isinstance(address, bool) or not isinstance(address, int) or not 0 <= address < 1 << 32:
            raise DebugError(f"not an address: {address!r}")
        start = max(0, (address - DISASM_BEFORE) & ~1)
        r = self._mi(f"-data-disassemble -s 0x{start:x} -e 0x{address + DISASM_AFTER:x} -- 0")
        return [{"addr": i.get("address", ""), "func": i.get("func-name", ""),
                 "offset": int(i.get("offset", 0) or 0), "inst": _clip(i.get("inst", ""))}
                for i in r.get("asm_insns", [])][:64]

    def snapshot(self) -> dict:
        """What a stop records: the stack, the top frame's locals, the
        registers and the watches."""
        out: dict = {}
        for key, fn in (("frames", self.frames), ("locals", self.locals),
                        ("registers", self.registers), ("watches", self.watch_values)):
            try:
                out[key] = fn()
            except DebugError as e:
                out.setdefault("errors", {})[key] = _clip(str(e))
        return out

    def interrupt(self) -> None:
        """Halt the board before its next instruction, with the monitor free
        (between RunFors): the stop (SIGINT) arrives once the board next
        executes, in the RunFor after this. Not GDB's -exec-interrupt: the
        stub's Ctrl-C pauses the CPUs, which the next RunFor resumes
        (models/renode/VhilGdb.cs, VhilGdbBreak)."""
        self._drain()
        if self.state == "running":
            self.sim.monitor("machine VhilGdbBreak", board=self.board)


# -- a live session's debuggers -----------------------------------------------------

# A live session's `debug` op (vhil/server/session.py): {kind: "debug", board,
# cmd, ...}. Queries read a stopped board and leave no `op` record in the
# trace; the rest change what runs.
QUERY_CMDS = ("frames", "locals", "registers", "eval", "disassemble")
EDIT_CMDS = ("break", "clear", "watches")
DEBUG_CMDS = ("attach", "detach", "interrupt") + EDIT_CMDS + RUN_CMDS + QUERY_CMDS
DEFERRED = "deferred"


def check_op(op: Any, boards) -> dict:
    """A `debug` op with its fields checked (DebugError says why): what the
    API queues and the worker applies."""
    if not isinstance(op, dict):
        raise DebugError("a debug op is an object")
    board, cmd = op.get("board"), op.get("cmd")
    if not isinstance(board, str) or board not in boards:
        raise DebugError(f"no board {board!r} to debug (have {', '.join(boards)})")
    if cmd not in DEBUG_CMDS:
        raise DebugError(f"unknown debug cmd {cmd!r} (have {', '.join(DEBUG_CMDS)})")
    allowed = {"break": {"location"}, "clear": {"number"}, "watches": {"exprs"},
               "locals": {"frame"}, "eval": {"expr", "frame"},
               "disassemble": {"address"}}.get(cmd, set())
    extra = set(op) - {"kind", "board", "cmd"} - allowed
    if extra:
        raise DebugError(f"{cmd} takes no {', '.join(sorted(extra))}")
    out = {"kind": "debug", "board": board, "cmd": cmd}
    if cmd == "break":
        location(op.get("location"))
        out["location"] = dict(op["location"])
    elif cmd == "clear":
        n = op.get("number")
        if isinstance(n, bool) or not isinstance(n, int) or not 0 < n < 1 << 20:
            raise DebugError(f"not a breakpoint number: {n!r}")
        out["number"] = n
    elif cmd == "watches":
        exprs = op.get("exprs")
        if not isinstance(exprs, list) or len(exprs) > MAX_WATCHES:
            raise DebugError(f"watches are a list of at most {MAX_WATCHES} expressions")
        out["exprs"] = [expression(e) for e in exprs]
    elif cmd in ("locals", "eval"):
        if op.get("frame") is not None:
            out["frame"] = frame_index(op["frame"])
        if cmd == "eval":
            out["expr"] = expression(op.get("expr"))
    elif cmd == "disassemble" and op.get("address") is not None:
        out["address"] = _address(op["address"])
    return out


def _address(a: Any) -> int:
    if isinstance(a, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,8}", a):
        a = int(a, 16)
    if isinstance(a, bool) or not isinstance(a, int) or not 0 <= a < 1 << 32:
        raise DebugError(f"not an address: {a!r}")
    return a


class DebugHub:
    """A live session's debuggers, one per board debugged (the worker's
    side; vhil/worker.py execute_run). `apply` takes one checked op and
    gives (status, result, detail, trace records), status "applied",
    "refused" or DEFERRED. An edit (break, clear, watches) for a board that
    runs is deferred: the hub interrupts the board and, when it stops for
    that during the next RunFor (`service`), applies the edits and lets it
    run on, with no stop to show; their results come from `service`."""

    def __init__(self, sim, workdir: Path, factory=Debugger):
        self.sim, self.workdir, self.factory = sim, Path(workdir), factory
        self.boards: dict[str, Debugger] = {}
        self.edits: dict[str, list[tuple[dict, dict]]] = {}   # board -> [(row, op)]
        self.wanted: set[str] = set()       # boards a user's interrupt is to stop

    def active(self) -> bool:
        return bool(self.boards)

    def stopped(self) -> list[str]:
        return [b for b, d in self.boards.items() if d.state == "stopped"]

    def where(self) -> Optional[dict]:
        """The first stopped board's stop, for the clock record."""
        for b in self.stopped():
            stop = self.boards[b].stop or {}
            out = {"board": b, "reason": stop.get("reason", "stopped")}
            out.update({k: v for k, v in (stop.get("frame") or {}).items()
                        if k in ("func", "file", "line", "addr")})
            return out
        return None

    def _record(self, now: int, board: str, event: str, **kw) -> dict:
        return {"kind": "debug", "t_us": now, "board": board, "event": event, **kw}

    def _breakpoints(self, now: int, board: str) -> dict:
        d = self.boards[board]
        return self._record(now, board, "breakpoints",
                            breakpoints=list(d.breakpoints.values()), watches=list(d.watches))

    def apply(self, op: dict, now: int, *, held: bool, paused: bool = False,
              row: Optional[dict] = None) -> tuple:
        """One op at virtual time `now`. held: the emulation is held at a
        breakpoint (a RunFor waits on a stopped board, so the monitor is
        busy); paused: the live session is paused (no RunFor runs)."""
        board, cmd = op["board"], op["cmd"]
        recs: list[dict] = []
        try:
            d = self.boards.get(board)
            if d is None and cmd in ("attach", "break", "watches", "interrupt"):
                if held:
                    raise DebugError("the system is held at a breakpoint: attach another "
                                     "board once it runs again")
                d = self.factory(self.sim, board, self.workdir)
                # Attaching stops the board: a break or watch goes in there and
                # it runs on; an interrupt keeps that stop (it is the stop).
                d.start(resume=cmd == "attach")
                self.boards[board] = d
                recs.append(self._record(now, board, "attached", elf=str(d.elf)))
                if cmd == "interrupt":
                    self.wanted.add(board)
                    return "applied", None, "", recs
                if cmd in ("break", "watches"):
                    try:
                        result = self._edit(d, op)
                    finally:
                        d.resume("continue")
                        d.events()          # attaching's stop and resume, not shown
                    recs.append(self._breakpoints(now, board))
                    return "applied", result, "", recs
            if cmd == "attach":
                return "applied", self.state(board), "", recs
            if d is None:
                raise DebugError(f"{board} has no debugger: attach first")
            if cmd == "detach":
                d.close()
                del self.boards[board]
                self.edits.pop(board, None)
                self.wanted.discard(board)
                recs.append(self._record(now, board, "detached"))
                return "applied", None, "", recs
            if cmd == "interrupt":
                if d.state == "stopped":
                    return "applied", self.state(board), "", recs
                if held or paused:
                    raise DebugError("the emulation is not running: nothing to interrupt "
                                     "(resume the session first)")
                self.wanted.add(board)
                d.interrupt()
                return "applied", None, "", recs
            if cmd in EDIT_CMDS:
                if d.state != "stopped":
                    if held:
                        raise DebugError(f"{board} runs, and the system is held at another "
                                         "board's breakpoint: change it when that one goes on")
                    if paused:
                        raise DebugError(f"{board} runs and the session is paused: resume "
                                         "it to change its breakpoints")
                    self.edits.setdefault(board, []).append((row or {}, op))
                    d.interrupt()
                    return DEFERRED, None, "", recs
                result = self._edit(d, op)
                recs.append(self._breakpoints(now, board))
                return "applied", result, "", recs
            if cmd in RUN_CMDS:
                d.resume(cmd)
                return "applied", None, "", recs
            if cmd == "frames":
                return "applied", d.frames(), "", recs
            if cmd == "locals":
                return "applied", d.locals(op.get("frame", 0)), "", recs
            if cmd == "registers":
                return "applied", d.registers(), "", recs
            if cmd == "eval":
                return "applied", {"expr": op["expr"],
                                   "value": d.evaluate(op["expr"], op.get("frame", 0))}, "", recs
            if cmd == "disassemble":
                return "applied", d.disassemble(op.get("address")), "", recs
            raise DebugError(f"unknown debug cmd {cmd!r}")
        except DebugError as e:
            return "refused", None, str(e), recs

    def _edit(self, d: Debugger, op: dict):
        if op["cmd"] == "break":
            return d.break_insert(op["location"])
        if op["cmd"] == "clear":
            d.break_delete(op["number"])
            return {"number": op["number"]}
        return {"watches": d.set_watches(op["exprs"])}

    def state(self, board: str) -> dict:
        d = self.boards[board]
        return {"board": board, "state": d.state, "breakpoints": list(d.breakpoints.values()),
                "watches": list(d.watches), "stop": d.stop}

    def service(self, now: int) -> tuple[list, list, list]:
        """Each board's stops since the last call: (the deferred ops settled
        [(row, status, result, detail)], trace records, the stops to show
        [(board, stop)]). A stop that only applied deferred edits is not
        shown: the board runs on."""
        settled, recs, shown = [], [], []
        for board, d in list(self.boards.items()):
            for ev in d.events():
                if ev["event"] != "stopped" or d.state != "stopped":
                    continue
                pending = self.edits.pop(board, [])
                for row, op in pending:
                    try:
                        settled.append((row, "applied", self._edit(d, op), ""))
                    except DebugError as e:
                        settled.append((row, "refused", None, str(e)))
                if pending:
                    recs.append(self._breakpoints(now, board))
                if pending and ev.get("signal") == "SIGINT" and board not in self.wanted:
                    d.resume("continue")
                    continue
                self.wanted.discard(board)
                shown.append((board, {k: v for k, v in ev.items() if k != "event"}))
        return settled, recs, shown

    def stop_record(self, now: int, board: str, stop: dict) -> dict:
        """A shown stop's trace record: where, when (Renode's halt time,
        `at_us`), the stack, the top frame's locals, the registers and the
        watches."""
        d = self.boards[board]
        rec = self._record(now, board, "stopped", **stop)
        halts = d.halt_times()
        if halts:
            rec["at_us"] = halts[-1][0]
        rec.update(d.snapshot())
        return rec

    def resume_all(self, how: str = "continue") -> list[str]:
        out = []
        for b in self.stopped():
            self.boards[b].resume(how)
            out.append(b)
        return out

    def close_all(self) -> None:
        for d in self.boards.values():
            try:
                d.close()
            except Exception:  # noqa: BLE001 - every board is let go
                pass
        self.boards.clear()
        self.edits.clear()
        self.wanted.clear()
