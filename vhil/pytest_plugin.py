"""Run IFS_HIL's own pytest suites against the virtual bench.

From an IFS_HIL checkout, with this repo on PYTHONPATH:

    pytest -p vhil.pytest_plugin --vhil-elf ECU08.elf --vhil-socketcan \\
        tests/hil/vcu/test_block_a_boot.py ...

The plugin starts Renode and the virtual broker before IFS_HIL's session
fixtures look for a bench, and points HIL_BROKER_SOCKET at it. IFS_HIL's
tests and conftests run unmodified.
"""
from __future__ import annotations

from pathlib import Path

from vhil.bench import DEFAULT_RENODE, VirtualBench

_bench = None


def pytest_addoption(parser):
    g = parser.getgroup("vhil", "virtual HIL bench (IFS_vHIL)")
    g.addoption("--vhil-elf", help="DUT image to boot (enables the virtual bench)")
    g.addoption("--vhil-renode", default=DEFAULT_RENODE, help="renode launcher")
    g.addoption("--vhil-socketcan", action="store_true",
                help="bridge the CAN hubs to SocketCAN (needs vcan can0..can2)")
    g.addoption("--vhil-socket", default="/tmp/vhil-broker.sock")
    g.addoption("--vhil-log", default="vhil-renode.log", help="Renode log file")


def pytest_configure(config):
    global _bench
    elf = config.getoption("--vhil-elf")
    if not elf:
        return
    _bench = VirtualBench(
        ifs_hil=Path(config.rootpath), elf=Path(elf),
        renode=config.getoption("--vhil-renode"),
        socketcan=config.getoption("--vhil-socketcan"),
        socket_path=config.getoption("--vhil-socket"),
        log_path=Path(config.getoption("--vhil-log")),
    ).start()


def pytest_unconfigure(config):
    if _bench is not None:
        _bench.stop()


def pytest_report_header(config):
    if config.getoption("--vhil-elf"):
        return (f"vhil: virtual bench, image {config.getoption('--vhil-elf')}, "
                f"socketcan={'on' if config.getoption('--vhil-socketcan') else 'off'}")
