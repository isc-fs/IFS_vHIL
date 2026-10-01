"""Drive a running Renode through its monitor port (`renode --port N`).

The monitor is line-oriented: send a command, read until the next prompt
(`(machine) ` or `(monitor) `). Renode echoes the command first; that echo
is dropped from the returned text.
"""
from __future__ import annotations

import re
import socket
import threading
import time

_PROMPT = re.compile(rb"\((?:monitor|[\w.-]+)\) $")
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


class RenodeError(RuntimeError):
    pass


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
        """Run one monitor command and return its output (without the echo)."""
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
