"""Run IFS_HIL's own pytest suites against the virtual bench.

From an IFS_HIL checkout, with this repo on PYTHONPATH:

    pytest -p vhil.pytest_plugin --vhil-elf ECU08.elf --vhil-socketcan \\
        tests/hil/vcu/test_block_a_boot.py ...

The plugin starts Renode and the virtual broker before IFS_HIL's session
fixtures look for a bench, and points HIL_BROKER_SOCKET at it. IFS_HIL's
tests and conftests run unmodified.

--vhil-system picks the system (default systems/ecu.yaml). --vhil-elf is
shorthand for a one-board system; --vhil-firmware BOARD=ELF names each board's
image.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import yaml

from vhil.bench import DEFAULT_RENODE, REPO, VirtualBench
from vhil.system import System

_bench = None


def pytest_addoption(parser):
    g = parser.getgroup("vhil", "virtual HIL bench (IFS_vHIL)")
    g.addoption("--vhil-system", default=str(REPO / "systems" / "ecu.yaml"),
                help="system file to run (systems/*.yaml)")
    g.addoption("--vhil-firmware", action="append", metavar="BOARD=ELF",
                help="image for one board of the system (repeat per board)")
    g.addoption("--vhil-elf", help="image for a one-board system (shorthand)")
    g.addoption("--vhil-renode", default=DEFAULT_RENODE, help="renode launcher")
    g.addoption("--vhil-socketcan", action="store_true",
                help="bridge the CAN buses to their host_netdev (needs those vcan links)")
    g.addoption("--vhil-socket", default="/tmp/vhil-broker.sock")
    g.addoption("--vhil-log", default="vhil-renode.log", help="Renode log file")
    g.addoption("--vhil-peripherals", default=str(REPO / "configs" / "peripherals.yaml"),
                help="unmodelled hardware the firmware may touch (peripheral guard)")
    g.addoption("--vhil-gaps", default=str(REPO / "configs" / "gaps.yaml"),
                help="known model gaps to skip with their reason")


def _firmware(config) -> dict[str, Path]:
    pairs = {}
    for item in config.getoption("--vhil-firmware") or []:
        board, sep, path = item.partition("=")
        if not sep:
            raise pytest.UsageError(f"--vhil-firmware expects BOARD=ELF, got '{item}'")
        pairs[board] = Path(path)
    elf = config.getoption("--vhil-elf")
    if elf:
        boards = list(System(Path(config.getoption("--vhil-system"))).boards)
        if len(boards) != 1:
            raise pytest.UsageError(f"--vhil-elf needs a one-board system; this one has "
                                    f"{boards}. Use --vhil-firmware BOARD=ELF.")
        pairs.setdefault(boards[0], Path(elf))
    return pairs


def pytest_configure(config):
    global _bench
    firmware = _firmware(config)
    if not firmware:
        return
    _bench = VirtualBench(
        ifs_hil=Path(config.rootpath), firmware=firmware,
        system=Path(config.getoption("--vhil-system")),
        renode=config.getoption("--vhil-renode"),
        socketcan=config.getoption("--vhil-socketcan"),
        socket_path=config.getoption("--vhil-socket"),
        log_path=Path(config.getoption("--vhil-log")),
    ).start()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Bring the bridged links back up after IFS_HIL's per-bus bitrate fixture.

    `vcu_can_sp` takes each bus down, set a bitrate
    and sample point, and bring it up again. A vcan link has no bitrate, so
    that call fails and the fixture's error path leaves the link down, which
    makes the Renode bridge report "Network is down". On bench-01 the call
    succeeds.
    """
    yield
    if _bench is None or not _bench.socketcan:
        return
    if fixturedef.argname != "vcu_can_sp":
        return
    links = [b["host_netdev"] for b in _bench.system.buses.values() if b.get("host_netdev")]
    for link in links:
        # vcan reports operstate "unknown" even when up; test IFF_UP instead.
        flags = Path(f"/sys/class/net/{link}/flags")
        if flags.exists() and not int(flags.read_text(), 16) & 0x1:
            subprocess.run(["sudo", "-n", "ip", "link", "set", link, "up"],
                           check=False, timeout=5)


def pytest_collection_modifyitems(config, items):
    if _bench is None:
        return
    gaps = yaml.safe_load(Path(config.getoption("--vhil-gaps")).read_text()) or []
    for item in items:
        for gap in gaps:
            if item.nodeid.startswith(gap["path"]):
                item.add_marker(pytest.mark.skip(reason=f"vhil gap: {gap['why']}"))
                break


def pytest_sessionfinish(session, exitstatus):
    """Stop Renode (so its log is complete), then fail the session if the
    firmware touched hardware the bench does not model and nobody explained
    it in configs/peripherals.yaml."""
    global _bench
    if _bench is None:
        return
    _bench.stop()
    log_path, _bench = _bench.log_path, None
    if log_path is None or not Path(log_path).exists():
        return
    from vhil import peripheral_guard as guard
    findings = guard.unexplained(Path(log_path).read_text(errors="replace"),
                                 guard.load_rules(session.config.getoption("--vhil-peripherals")))
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if findings:
        if tr:
            tr.write_sep("=", "vhil peripheral guard", red=True)
            tr.write_line(guard.report(findings))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    elif tr:
        tr.write_line("vhil peripheral guard: every unmodelled access is explained")


def pytest_unconfigure(config):
    if _bench is not None:
        _bench.stop()


def pytest_report_header(config):
    if _bench is not None:
        images = ", ".join(f"{b}={p.name}" for b, p in _bench.firmware.items())
        return (f"vhil: system {_bench.system.id} ({images}), "
                f"socketcan={'on' if _bench.socketcan else 'off'}")
