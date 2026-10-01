"""Run IFS_HIL's own pytest suites against the virtual bench.

From an IFS_HIL checkout, with this repo on PYTHONPATH:

    pytest -p vhil.pytest_plugin --vhil-elf ECU08.elf --vhil-socketcan \\
        tests/hil/vcu/test_block_a_boot.py ...

The plugin starts Renode and the virtual broker before IFS_HIL's session
fixtures look for a bench, and points HIL_BROKER_SOCKET at it. IFS_HIL's
tests and conftests run unmodified.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import yaml

from vhil.bench import DEFAULT_RENODE, REPO, VirtualBench

_bench = None


def pytest_addoption(parser):
    g = parser.getgroup("vhil", "virtual HIL bench (IFS_vHIL)")
    g.addoption("--vhil-elf", help="DUT image to boot (enables the virtual bench)")
    g.addoption("--vhil-renode", default=DEFAULT_RENODE, help="renode launcher")
    g.addoption("--vhil-socketcan", action="store_true",
                help="bridge the CAN hubs to SocketCAN (needs vcan can0..can2)")
    g.addoption("--vhil-socket", default="/tmp/vhil-broker.sock")
    g.addoption("--vhil-log", default="vhil-renode.log", help="Renode log file")
    g.addoption("--vhil-peripherals", default=str(REPO / "configs" / "peripherals.yaml"),
                help="unmodelled hardware the firmware may touch (peripheral guard)")
    g.addoption("--vhil-gaps", default=str(REPO / "configs" / "gaps.yaml"),
                help="known model gaps to skip with their reason")


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
    for link in ("can0", "can1", "can2"):
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
    if config.getoption("--vhil-elf"):
        return (f"vhil: virtual bench, image {config.getoption('--vhil-elf')}, "
                f"socketcan={'on' if config.getoption('--vhil-socketcan') else 'off'}")
