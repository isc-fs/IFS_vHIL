"""Start and stop a virtual bench: Renode with the DUT loaded (unpowered) plus
the virtual broker serving IFS_HIL's protocol on a Unix socket."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import yaml

from vhil.broker import make_backend
from vhil.renode import RenodeMonitor

REPO = Path(__file__).resolve().parent.parent
DEFAULT_RENODE = os.environ.get(
    "RENODE", str(Path.home() / "vhil-tools/renode_1.17.0-portable/renode"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class VirtualBench:
    def __init__(self, ifs_hil: Path, elf: Path, *, renode: str = DEFAULT_RENODE,
                 socketcan: bool = False, socket_path: str = "/tmp/vhil-broker.sock",
                 config: Path = REPO / "configs" / "vbench.yaml",
                 log_path: Path | None = None):
        self.ifs_hil, self.elf = Path(ifs_hil).resolve(), Path(elf).resolve()
        self.renode, self.socketcan = renode, socketcan
        self.socket_path, self.config = socket_path, config
        self.log_path = log_path
        self._proc = self._monitor = None

    def start(self) -> "VirtualBench":
        if not self.elf.is_file():
            raise FileNotFoundError(self.elf)
        port = _free_port()
        log = open(self.log_path, "w") if self.log_path else subprocess.DEVNULL
        self._proc = subprocess.Popen(
            [self.renode, "--disable-gui", "--plain", "-P", str(port)],
            stdout=log, stderr=subprocess.STDOUT)
        self._monitor = RenodeMonitor(port)
        m = self._monitor
        m.execute(f"$elf=@{self.elf}")
        m.execute(f"include @{REPO / 'scripts' / 'ecu.resc'}")
        if self.socketcan:
            m.execute(f"include @{REPO / 'scripts' / 'ecu-socketcan.resc'}")
        # Carriers start unpowered: off their CAN buses, while the emulation
        # runs so virtual time stays paced to host time (see broker.py). The
        # suite's relay fixture powers them, which resets them.
        config = yaml.safe_load(Path(self.config).read_text())
        m.execute("start")
        for carrier in config.get("carriers", []):
            m.execute(f'mach set "{carrier["machine"]}"')
            for controller, hub in carrier.get("can", {}).items():
                m.execute(f"connector Disconnect {controller} {hub}")

        sys.path.insert(0, str(self.ifs_hil))
        from broker.fake_bus import FakeHardwareManager
        from broker.server import serve

        backend = make_backend(FakeHardwareManager, m, config)
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
