"""Drive a running Renode through its monitor port, and write the text it reads.

The monitor is line-oriented: send a command, read until the next prompt
(`(machine) ` or `(monitor) `). Renode echoes the command first; that echo
is dropped from the returned text.

Renode starts here with its own monitor port off and a loopback-only one of
ours (`launch`, models/renode/VhilMonitor.cs): Renode 1.17's `-P <port>`
listens on every interface with no authentication, and has no option to bind
it to one address (SocketServerProvider.Start binds IPAddress.Any).

Anything a system file or a run scenario supplies reaches Renode only through
the encoders below (`ident`, `path`, `quote`, `file_arg`, `number`,
`comment`). Each one either returns text that can't change the structure of
the command or script it lands in, or raises UnsafeText: they are the last
line behind the schema and System's checks, so that a gap there can't add a
monitor command or a platform-description entry.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

from vhil.benchclock import host as hostclock

REPO = Path(__file__).resolve().parent.parent
MONITOR_SOURCE = REPO / "models" / "renode" / "VhilMonitor.cs"

_PROMPT = re.compile(rb"\((?:monitor|[\w.-]+)\) $")
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
_ABORT = re.compile(rb"VHIL-ABORT (.*)\n")

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
# Inside a monitor or .repl string literal: nothing that ends it (", \),
# ends the line, or starts a comment, a variable or another command.
_STRING = re.compile(r"[A-Za-z0-9 _.,:/+=@-]*")
# A host path after @: Renode ends it at whitespace.
_FILE = re.compile(r"/?[A-Za-z0-9_.+@-]+(?:/[A-Za-z0-9_.+@-]+)*")


class RenodeError(RuntimeError):
    pass


class MachineAborted(RenodeError):
    """Renode aborted a machine (a CPU abort: models/renode/VhilMonitor.cs
    ends the connection with a VHIL-ABORT line). The emulation can't go on;
    what tlib refused to do is in the Renode log."""


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


def monitor_command(renode: str, port: int) -> list[str]:
    """The command line that starts Renode headless with its monitor on
    127.0.0.1:<port> only (models/renode/VhilMonitor.cs). `-P -1` turns
    Renode's own all-interfaces monitor port off; the -e commands run in its
    hidden monitor before ours listens. They load every vHIL model first, in
    one `include` that Renode's compiler cache answers (vhil/models.py), so
    nothing the process runs later includes a model again."""
    from vhil import models
    return [renode, "--disable-gui", "--plain", "-P", "-1", "-e",
            f"{models.include_command()}; emulation StartVhilMonitor {int(port)}"]


# .NET runtime settings for Renode (#154). Tiered compilation first JITs a
# method unoptimised and promotes it once it is hot, but holds every promotion
# back while the process keeps calling new methods (a "startup" phase,
# TC_CallCountingDelayMs, 100 ms each time). Renode's hot paths start that way
# for every new Renode, which each native test is: with no delay they are
# optimised within the first boot's bootloader window instead of after it.
# A method is promoted after TC_CallCountThreshold calls (30 by default):
# since Renode's own assemblies are ReadyToRun (#278) it starts on native
# code, and at 30 calls the tiered JIT recompiled thousands of methods a boot
# barely uses, on a thread of its own beside the boards' (0.76 CPU s per
# virtual s over a three-board boot, x86). At 300 only the paths that stay hot
# are promoted: boots 20-30 % faster, steady state within -4 % (#260).
# Host speed only; nothing the emulation computes depends on it.
RENODE_ENV = {"DOTNET_TC_CallCountingDelayMs": "0", "DOTNET_TC_CallCountThreshold": "300"}


# Set by a caller that drives Renode in lock-step (vhil.sim): monitor commands
# only between RunFor steps, while every machine is paused. Models may then
# defer work to whoever next looks (Stm32H7Adc3.cs's lazy DMA hand-over),
# which a command landing while a CPU runs (the wall-clock bench) would race.
LOCKSTEP_ENV = {"VHIL_LOCKSTEP": "1"}


# Beside the launcher of a Renode built by docker/renode/build.sh: one line
# naming the release, its commit and the vHIL's patch set.
BUILD_FILE = "VHIL_RENODE_BUILD"
RELEASE_BUILD = f"renode (no {BUILD_FILE}: a release package, without the vHIL's patches)"


def build_id(renode: str) -> str:
    """Which Renode `renode` (a launcher path or a name on PATH) is: the line
    docker/renode/build.sh wrote beside it, or RELEASE_BUILD without one. What
    a run computes is the same either way (docker/renode/README.md); how fast
    it runs is not, so speed results and run summaries record it."""
    found = shutil.which(renode) or renode
    try:
        lines = (Path(found).resolve().parent / BUILD_FILE).read_text().splitlines()
    except OSError:
        return RELEASE_BUILD
    return comment(lines[0].strip()) if lines and lines[0].strip() else RELEASE_BUILD


def launch(renode: str, port: int, stdout=subprocess.DEVNULL, lockstep: bool = False) -> subprocess.Popen:
    """Start Renode with a loopback-only monitor on `port`; connect with
    RenodeMonitor(port), which waits for it to listen. A log file given as
    `stdout` starts with the Renode build (build_id). The models' assembly
    is compiled or checked first (vhil/models.py, VHIL_MODELS)."""
    from vhil import models
    models.ensure(renode)
    env = {**RENODE_ENV, **(LOCKSTEP_ENV if lockstep else {}), **os.environ, **models.env(renode)}
    if hasattr(stdout, "write"):
        line = f"vhil: {build_id(renode)}\n"
        stdout.write(line.encode() if "b" in getattr(stdout, "mode", "") else line)
        stdout.flush()
    return subprocess.Popen(monitor_command(renode, port), stdout=stdout,
                            stderr=subprocess.STDOUT, env=env)


class RenodeMonitor:
    def __init__(self, port: int, host: str = "127.0.0.1",
                 connect_timeout_s: float = 60.0, timeout_s: float = 30.0):
        deadline = hostclock.monotonic() + connect_timeout_s
        while True:
            try:
                self._sock = socket.create_connection((host, port), timeout=timeout_s)
                break
            except OSError:
                if hostclock.monotonic() > deadline:
                    raise
                # Renode takes ~1.5 s to listen; a coarse poll added up to its
                # own period to every start (#260).
                hostclock.sleep(0.05)
        self._lock = threading.Lock()
        self._buf = b""
        self.aborted: str | None = None   # the VHIL-ABORT line, once a machine aborted
        self._read_prompt()

    def _read_prompt(self) -> bytes:
        while True:
            clean = _ANSI.sub(b"", self._buf).replace(b"\r", b"")
            abort = _ABORT.search(clean)
            if abort:
                # The line may come before or after the command's own prompt.
                self.aborted = abort.group(1).decode(errors="replace")
                raise MachineAborted(self.aborted)
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
