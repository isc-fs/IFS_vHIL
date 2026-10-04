"""Drive a running Renode through its monitor port, and write the text it reads.

The monitor is line-oriented: send a command, read until the next prompt
(`(machine) ` or `(monitor) `). Renode echoes the command first; that echo
is dropped from the returned text.

Anything a system file or a run scenario supplies reaches Renode only through
the encoders below (`ident`, `path`, `quote`, `file_arg`, `number`,
`comment`). Each one either returns text that can't change the structure of
the command or script it lands in, or raises UnsafeText: they are the last
line behind the schema and System's checks, so that a gap there can't add a
monitor command or a platform-description entry.
"""
from __future__ import annotations

import math
import re
import socket
import threading
import time
from pathlib import Path

_PROMPT = re.compile(rb"\((?:monitor|[\w.-]+)\) $")
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
# Inside a monitor or .repl string literal: nothing that ends it (", \),
# ends the line, or starts a comment, a variable or another command.
_STRING = re.compile(r"[A-Za-z0-9 _.,:/+=@-]*")
# A host path after @: Renode ends it at whitespace.
_FILE = re.compile(r"/?[A-Za-z0-9_.+@-]+(?:/[A-Za-z0-9_.+@-]+)*")


class RenodeError(RuntimeError):
    pass


class UnsafeText(ValueError):
    """A value that would not stay one token of the Renode text it goes into."""


def ident(value: str) -> str:
    """A name Renode reads as one word: a machine, hub, peripheral or variable."""
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise UnsafeText(f"not a Renode identifier: {value!r}")
    return value


def path(value: str) -> str:
    """A dotted peripheral path or type name, e.g. sysbus.spi1 or SPI.Ltc6811."""
    if not isinstance(value, str) or not PATH.fullmatch(value):
        raise UnsafeText(f"not a Renode peripheral path: {value!r}")
    return value


def quote(value: str) -> str:
    """A double-quoted string literal (monitor command or platform description)."""
    if not isinstance(value, str) or len(value) > 4096 or not _STRING.fullmatch(value):
        raise UnsafeText(f"not a safe Renode string: {value!r}")
    return f'"{value}"'


def file_arg(value) -> str:
    """`@<absolute path>` for a host file."""
    text = Path(value).resolve().as_posix()
    if not _FILE.fullmatch(text) or "/../" in text + "/":
        raise UnsafeText(f"not a safe Renode file path: {text!r}")
    return "@" + text


def number(value) -> str:
    """An integer or a finite real, as Renode parses it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnsafeText(f"not a number: {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise UnsafeText(f"not a finite number: {value!r}")
        return repr(value)
    return str(value)


def comment(value) -> str:
    """Text for a `#` comment or a script header: one line of printable ASCII."""
    return re.sub(r"[^\x20-\x7e]", "?", str(value))


class RenodeMonitor:
    def __init__(self, port: int, host: str = "127.0.0.1",
                 connect_timeout_s: float = 60.0, timeout_s: float = 30.0):
        deadline = time.monotonic() + connect_timeout_s
        while True:
            try:
                self._sock = socket.create_connection((host, port), timeout=timeout_s)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        self._lock = threading.Lock()
        self._buf = b""
        self._read_prompt()

    def _read_prompt(self) -> bytes:
        while True:
            clean = _ANSI.sub(b"", self._buf).replace(b"\r", b"")
            m = _PROMPT.search(clean)
            if m:
                self._buf = b""
                return clean[:m.start()]
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RenodeError("Renode closed the monitor connection")
            self._buf += chunk

    def execute(self, command: str) -> str:
        """Run one monitor command and return its output (without the echo).
        One command is one line: a line break would let the rest run as
        commands of their own."""
        if "\n" in command or "\r" in command or "\x00" in command:
            raise UnsafeText(f"a monitor command is one line: {command!r}")
        with self._lock:
            self._sock.sendall(command.encode() + b"\n")
            out = self._read_prompt().decode(errors="replace")
        lines = [ln for ln in out.split("\n") if ln.strip()]
        if lines and lines[0].strip().endswith(command.strip()):
            lines = lines[1:]
        text = "\n".join(lines)
        if "There was an error" in text or "Could not" in text:
            raise RenodeError(f"{command!r}: {text}")
        return text

    def close(self) -> None:
        self._sock.close()
