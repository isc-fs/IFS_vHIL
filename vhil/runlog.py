"""A run's live logs: what the worker, the firmware build, Renode and each
board's UARTs say, as trace `log` records while the run goes on
(docs/integration-contract.md, `log`; docs/live-session.md, "Logs").

Every record carries a `source` and a `level`:

| source | what | its `t_us` |
|---|---|---|
| `worker` | the worker's own notes (firmware, attempt, errors) | virtual time; 0 before power-on, with `wall_s` |
| `build` | `python -m vhil.system build`, line by line, while a run waits for it | 0, with `wall_s` |
| `pytest` | a `pytest` run's output | 0, with `wall_s` |
| `scenario` | stimuli, ops and expects | virtual time they took effect |
| `renode` | Renode's log, filtered (RenodeFilter); `board` when a machine said it | end of the slice it was read in |
| `<board>.<UART>` | what the firmware sent on a UART (`ecu.USART10`) | end of the slice it was read in |

`wall_s` is Unix time (s): the wall time a record was written before the
system was powered on, when there is no virtual time yet. A line read from a
file (Renode's log, a UART) is stamped with the end of the slice the worker
read it in: at most one slice (`slice_ms`) after the firmware wrote it.

RateLimit keeps a chatty source (a UART printing every ms, a build's
compiler lines) from flooding the trace and the socket: at most `per_s` lines
a second per source (virtual seconds, or wall seconds for `wall_s` records),
and the lines over it counted in one record per second (`dropped`). Errors
always pass. The full output stays in the run's artifacts: renode.log,
build.log, uart/<board>.<UART>.txt.
"""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from vhil import peripheral_guard as guard

LEVELS = ("debug", "info", "warning", "error")
# Lines a second per source (VHIL_LOG_LINES_PER_S).
LINES_PER_S = int(os.environ.get("VHIL_LOG_LINES_PER_S", "") or 20)
# A UART line with no newline is cut here, so a stream that never ends a
# line still shows.
UART_LINE_BYTES = 256
# A Renode message with more lines (an exception's stack) shows this many.
CONTINUATION_LINES = 8


def record(t_us: int, text: str, source: str, level: str = "info", **extra) -> dict:
    rec = {"kind": "log", "t_us": int(t_us), "text": text, "source": source, "level": level}
    rec.update({k: v for k, v in extra.items() if v is not None})
    return rec


def wall() -> float:
    return round(time.time(), 3)


def level_of(text: str) -> str:
    """A build or test line's level, from what it says."""
    low = text.lower()
    if re.search(r"\b(error|fatal|traceback)\b", low):
        return "error"
    if re.search(r"\bwarning\b", low):
        return "warning"
    return "info"


# -- the rate limit ------------------------------------------------------------------

class RateLimit:
    """At most `per_s` records a second per source; the rest counted and
    reported once that second is over (a record with `dropped`)."""

    def __init__(self, per_s: int = LINES_PER_S):
        self.per_s = per_s
        self._w: dict[str, list] = {}     # source -> [window start s, shown, dropped, last rec]
        self.dropped: Counter = Counter()  # source -> lines dropped in all

    @staticmethod
    def _t(rec: dict) -> float:
        return rec["wall_s"] if "wall_s" in rec else rec["t_us"] / 1e6

    def _note(self, src: str, w: list, rec: dict) -> dict:
        extra = {"wall_s": rec["wall_s"]} if "wall_s" in rec else {}
        return record(rec["t_us"], f"{w[2]} more {src} line(s) not shown in this second "
                                   f"(at most {self.per_s} a second); the run's artifacts have "
                                   f"them all", src, "warning", dropped=w[2], board=rec.get("board"),
                      **extra)

    def __call__(self, recs: Iterable[dict]) -> list[dict]:
        out = []
        for rec in recs:
            src = rec.get("source", "")
            t = self._t(rec)
            w = self._w.get(src)
            if w is None or t >= w[0] + 1:
                if w is not None and w[2]:
                    out.append(self._note(src, w, rec))
                w = self._w[src] = [t, 0, 0, rec]
            if w[1] < self.per_s or rec.get("level") == "error":
                w[1] += 1
                w[3] = rec
                out.append(rec)
            else:
                w[2] += 1
                self.dropped[src] += 1
        return out

    def flush(self, rec_like: Optional[dict] = None, sources: Optional[Iterable[str]] = None) -> list[dict]:
        """Notes for the lines dropped so far (every source, or `sources`),
        stamped as `rec_like` (default: the last record shown of each)."""
        out = []
        for src, w in self._w.items():
            if w[2] and (sources is None or src in sources):
                out.append(self._note(src, w, rec_like or w[3]))
                w[2] = 0
        return out


# -- files the worker tails ----------------------------------------------------------

class FileTail:
    """The whole lines a file gained since the last read. The file may not
    exist yet (Renode creates its log as it starts)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = 0
        self.partial = b""

    def read(self) -> bytes:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except FileNotFoundError:
            return b""
        return chunk

    def lines(self) -> list[str]:
        data = self.partial + self.read()
        *whole, self.partial = data.split(b"\n")
        return [w.rstrip(b"\r").decode("utf-8", "replace") for w in whole]

    def rest(self) -> list[str]:
        """Whatever is left with no newline (the file's end)."""
        out = self.lines()
        if self.partial:
            out.append(self.partial.rstrip(b"\r").decode("utf-8", "replace"))
            self.partial = b""
        return out


def printable(raw: bytes) -> str:
    """A UART line as text: control bytes other than tab shown as \\xNN."""
    text = raw.decode("utf-8", "replace")
    return "".join(c if c == "\t" or (c >= " " and c != "\x7f") else f"\\x{ord(c):02x}"
                   for c in text)


class UartTail(FileTail):
    """A UART's TX bytes (Renode's file backend) as lines: cut at a newline,
    or at UART_LINE_BYTES when none comes."""

    def lines(self) -> list[str]:
        data = self.partial + self.read()
        out = []
        while True:
            i = data.find(b"\n")
            if i < 0:
                break
            out.append(printable(data[:i].rstrip(b"\r")))
            data = data[i + 1:]
        while len(data) >= UART_LINE_BYTES:
            out.append(printable(data[:UART_LINE_BYTES]))
            data = data[UART_LINE_BYTES:]
        self.partial = data
        return out

    def rest(self) -> list[str]:
        out = self.lines()
        if self.partial.strip(b"\r\n\x00"):
            out.append(printable(self.partial.rstrip(b"\r")))
        self.partial = b""
        return out


# -- Renode's log ------------------------------------------------------------------

# "[14:26:08.9078] [INFO] ecu: Machine started."; with several machines the
# source has the machine first ("ams/watchdog:"), as the peripheral guard reads.
_LINE = re.compile(r"^\[[\d:.]+\] \[(\w+)\] (?:([\w.-]+)/)?(?:([\w.-]+): )?(.*)$")
_LEVEL = {"NOISY": "debug", "DEBUG": "debug", "INFO": "info", "WARNING": "warning",
          "ERROR": "error"}
# Info lines worth showing: a machine starting, a CPU's (re)start, an abort.
_NOTABLE_INFO = re.compile(r"Machine started|Setting initial values|abort|[Rr]eset", re.I)
# Info lines that say nothing to a user, whatever the patterns above match.
_QUIET_INFO = re.compile(r"Machine (paused|resumed)|Disposed|Loading block|Including script|"
                         r"Monitor available|monitor commands|Pseudorandom|System bus created")
# Warnings that are always shown, however often: the firmware's watchdog.
_ALWAYS = re.compile(r"[Ww]atchdog")
# Warnings that never are: the host's limits, not the firmware's.
_NEVER = re.compile(r"Translation cache size")
_NUMBERS = re.compile(r"0x[0-9A-Fa-f]+|\d+")
# Renode's own count of a message said again in a row: "... medium? (3)".
_REPEAT = re.compile(r"\s*\(\d+\)$")


class RenodeFilter:
    """Renode's log as a user wants it live: errors; the watchdog; a machine
    starting or a CPU (re)starting; each unmodelled access the peripheral
    guard's rules (configs/peripherals.yaml) don't explain, once; any other
    warning the first time it is said (numbers aside), its repeats counted
    (summary()). Everything else (the explained accesses, info) stays in the
    renode.log artifact."""

    def __init__(self, rules: Optional[list] = None):
        self.rules = rules if rules is not None else _load_rules()
        self.seen: set = set()
        self.repeats: Counter = Counter()
        self.unmodelled: set = set()
        self._cont = 0          # continuation lines still to show of the last kept message
        self._last: Optional[tuple] = None

    def feed(self, line: str) -> Optional[tuple[str, str, Optional[str]]]:
        """(level, text, board) to show, or None."""
        m = _LINE.match(line)
        if m is None:
            # A message's next line (a stack trace): shown after a kept one.
            if self._cont > 0 and line.strip():
                self._cont -= 1
                return self._last[0], line.rstrip(), self._last[2]
            return None
        self._cont = 0
        level = _LEVEL.get(m.group(1).upper(), "info")
        board, src, msg = m.group(2), m.group(3), m.group(4)
        text = f"{src}: {msg}" if src else msg
        shown = self._judge(level, board, line, text)
        if shown is None:
            return None
        self._last = (shown[0], shown[1], board)
        self._cont = CONTINUATION_LINES
        return shown[0], shown[1], board

    def _judge(self, level: str, board: Optional[str], line: str,
               text: str) -> Optional[tuple[str, str]]:
        if level == "error":
            return level, text
        found = guard.scan(line)
        if found:
            key = next(iter(found))
            if guard.unexplained(line, self.rules) and key not in self.unmodelled:
                self.unmodelled.add(key)
                return "warning", (f"unmodelled hardware: {guard.describe(key)} (not explained in "
                                   f"configs/peripherals.yaml; invariant 7)")
            return None
        if level == "warning":
            if _NEVER.search(text):
                return None
            if _ALWAYS.search(text):
                return level, text
            sig = (board, _NUMBERS.sub("N", _REPEAT.sub("", text)))
            if sig in self.seen:
                self.repeats[sig] += 1
                return None
            self.seen.add(sig)
            return level, text
        if level == "info" and _NOTABLE_INFO.search(text) and not _QUIET_INFO.search(text):
            return level, text
        return None

    def summary(self) -> Optional[str]:
        if not self.repeats:
            return None
        n = sum(self.repeats.values())
        return (f"{n} repeat(s) of {len(self.repeats)} Renode warning(s) not shown; "
                f"the renode.log artifact has them all")


def _load_rules() -> list:
    try:
        return guard.load_rules()
    except OSError:
        return []


# -- a run's logs ------------------------------------------------------------------

class RunLogs:
    """What a `run` run streams besides the worker's own notes: Renode's log
    (filtered), each UART's lines and each board's bootloader-to-app jump,
    read at each slice boundary (poll) and once more when the run ends
    (finish)."""

    def __init__(self, renode_log: Optional[Path] = None, uarts: Optional[dict] = None,
                 limit: Optional[RateLimit] = None, rules: Optional[list] = None):
        self.renode = FileTail(renode_log) if renode_log is not None else None
        self.filter = RenodeFilter(rules)
        self.uarts = {src: UartTail(p) for src, p in (uarts or {}).items()}
        self.limit = limit or RateLimit()
        self._in_app: dict[str, bool] = {}
        self._boot_ok = True

    def add_uarts(self, uarts: dict) -> None:
        for src, p in uarts.items():
            self.uarts.setdefault(src, UartTail(p))

    def _renode(self, t_us: int, lines: list[str]) -> list[dict]:
        out = []
        for line in lines:
            shown = self.filter.feed(line)
            if shown is not None:
                level, text, board = shown
                out.append(record(t_us, text, "renode", level, board=board))
        return out

    def _boot(self, t_us: int, sim) -> list[dict]:
        """A board's jump from its bootloader to its app, and back (a reset),
        as the PC at the slice boundary shows (Sim.in_app)."""
        if sim is None or not self._boot_ok or not hasattr(sim, "in_app"):
            return []
        out = []
        for name, board in sim.system.boards.items():
            if board.bootloader is None:
                continue
            try:
                now = bool(sim.in_app(name))
            except Exception:  # noqa: BLE001 - an aborted machine: say nothing more
                self._boot_ok = False
                return out
            was = self._in_app.get(name)
            self._in_app[name] = now
            if was is None and not now:
                continue
            if now and not was:
                out.append(record(t_us, f"{name}: the bootloader jumped to the app", "renode",
                                  board=name))
            elif was and not now:
                out.append(record(t_us, f"{name}: back in the bootloader (a reset)", "renode",
                                  "warning", board=name))
        return out

    def poll(self, t_us: int, sim=None) -> list[dict]:
        recs = self._renode(t_us, self.renode.lines()) if self.renode else []
        for src, tail in self.uarts.items():
            board = src.split(".", 1)[0]
            recs += [record(t_us, line, src, board=board) for line in tail.lines()]
        recs += self._boot(t_us, sim)
        return self.limit(recs)

    def finish(self, t_us: int) -> list[dict]:
        recs = self._renode(t_us, self.renode.rest()) if self.renode else []
        for src, tail in self.uarts.items():
            board = src.split(".", 1)[0]
            recs += [record(t_us, line, src, board=board) for line in tail.rest()]
        out = self.limit(recs)
        out += self.limit.flush(record(t_us, "", ""))
        s = self.filter.summary()
        if s:
            out.append(record(t_us, s, "renode"))
        return out
