"""Start and stop a virtual bench: Renode running a system (systems/*.yaml),
its boards unpowered, plus the virtual broker serving IFS_HIL's protocol on a
Unix socket."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from vhil import renode as rn
from vhil.broker import make_backend, watch_pacing
from vhil.renode import RenodeMonitor
from vhil.system import System

REPO = Path(__file__).resolve().parent.parent
DEFAULT_RENODE = os.environ.get(
    "RENODE", str(Path.home() / "vhil-tools/renode_1.17.0-portable/renode"))

# IFS_HIL's suites measure wall-clock time, so the bench must keep up with the
# host clock. The ECU never sleeps (its idle task spins), so host cost scales
# with the core's MIPS: at the platform's faithful 528 an i9 manages 0.6x real
# time, at Renode's default 100 about 1.3x. The bench trades busy-wait timing
# fidelity for real time; native tests (vhil.sim) keep the platform's value.
WALL_CLOCK_MIPS = 100

PACER_SOURCE = REPO / "models" / "renode" / "VhilPacer.cs"
# A deficit beyond this is forgiven rather than repaid; within it the bench
# catches up, so wall-clock periods stay within this of nominal.
PACER_MAX_LAG_S = 0.05


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class VirtualBench:
    def __init__(self, ifs_hil: Path, firmware: dict[str, Path], *,
                 system: Path = REPO / "systems" / "ecu.yaml",
                 renode: str = DEFAULT_RENODE, socketcan: bool = False,
                 socket_path: str = "/tmp/vhil-broker.sock",
                 log_path: Path | None = None):
        self.ifs_hil = Path(ifs_hil).resolve()
        self.system = System(system)
        self.firmware = {b: Path(p).resolve() for b, p in firmware.items()}
        self.renode, self.socketcan = renode, socketcan
        self.socket_path, self.log_path = socket_path, log_path
        self._proc = self._monitor = None

    def start(self) -> "VirtualBench":
        for image in self.system.images():
            if image not in self.firmware:
                raise ValueError(f"no firmware image for '{image}'")
            if not self.firmware[image].is_file():
                raise FileNotFoundError(self.firmware[image])
        script = Path(tempfile.mkdtemp(prefix="vhil-")) / f"{self.system.id}.resc"
        script.write_text(self.system.render_renode(self.firmware, socketcan=self.socketcan))
        port = _free_port()
        log = open(self.log_path, "w") if self.log_path else subprocess.DEVNULL
        # The monitor listens on 127.0.0.1 only (vhil/renode.py, launch).
        self._proc = rn.launch(self.renode, port, stdout=log)
        self._monitor = RenodeMonitor(port)
        m = self._monitor
        m.execute(f"include {rn.file_arg(script)}")
        # Pace to the wall clock ourselves: Renode's pacing repays every slow
        # stretch by running fast afterwards (see VhilPacer.cs).
        m.execute(f"include {rn.file_arg(PACER_SOURCE)}")
        m.execute("emulation SetGlobalAdvanceImmediately true")
        m.execute(f"emulation EnableVhilPacer {rn.number(PACER_MAX_LAG_S)}")
        for board in self.system.boards:
            m.execute(f"mach set {rn.quote(rn.ident(board))}")
            m.execute(f"cpu PerformanceInMips {WALL_CLOCK_MIPS}")
        # Boards start unpowered: off their CAN buses, while the emulation
        # runs so virtual time stays paced to host time (see broker.py). The
        # suite's relay fixture powers them, which resets them.
        config = self.system.bench_config()
        m.execute("start")
        for entry in config.get("power", []):
            m.execute(f"mach set {rn.quote(rn.ident(entry['machine']))}")
            for controller, hub in entry.get("can", {}).items():
                m.execute(f"connector Disconnect {rn.path(controller)} {rn.ident(hub)}")

        sys.path.insert(0, str(self.ifs_hil))
        from broker.fake_bus import FakeHardwareManager
        from broker.server import serve

        backend = make_backend(FakeHardwareManager, m, config, boot_check=True)
        threading.Thread(target=watch_pacing, args=(m, backend.monitor_lock),
                         daemon=True, name="vhil-pacing").start()
        # A socket left by an earlier run would satisfy _wait_for_socket before
        # this broker has bound it.
        Path(self.socket_path).unlink(missing_ok=True)
        threading.Thread(target=serve, args=(backend, self.socket_path),
                         daemon=True, name="vhil-broker").start()
        os.environ["HIL_BROKER_SOCKET"] = self.socket_path
        self._wait_for_socket()
        return self

    def _wait_for_socket(self, timeout_s: float = 10.0) -> None:
        """Ready means a client can connect: the file exists from bind(),
        a moment before listen()."""
        import time
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(self.socket_path)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"virtual broker never listened on {self.socket_path}")
                time.sleep(0.05)

    def stop(self) -> None:
        if self._monitor is not None:
            try:
                self._monitor.execute("quit")
            except Exception:
                pass
            self._monitor.close()
        if self._proc is not None:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
