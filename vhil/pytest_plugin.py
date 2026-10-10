"""Run IFS_HIL's own pytest suites against the virtual bench.

From an IFS_HIL checkout, with this repo on PYTHONPATH:

    pytest -p vhil.pytest_plugin --vhil-elf ECU08.elf --vhil-socketcan \\
        tests/hil/vcu/test_block_a_boot.py ...

The plugin starts Renode and the virtual broker before IFS_HIL's session
fixtures look for a bench, and points HIL_BROKER_SOCKET at it. IFS_HIL's
tests and conftests run unmodified.

Cases IFS_HIL gates on a bench operator's step (an env var plus pulling the
AMS's card, reading it, or a bus-off adapter) run with the bench doing the
step itself (vhil/bench_operator.py).

--vhil-system picks the system (default systems/ecu.yaml). --vhil-elf is
shorthand for a one-board system; --vhil-firmware BOARD=ELF names each board's
image.

The tests run on the bench's time, not the host's (vhil/benchclock.py, #243):
a host that emulates the system slower than real time makes them take longer,
not miss their deadlines. Each test's report says how fast the bench ran
(section "vhil bench time, setup to teardown"), and the session's summary
for the whole run.
--vhil-host-clock keeps them on the host's clock instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import yaml

from vhil import bench_operator as operator
from vhil.bench import DEFAULT_RENODE, REPO, VirtualBench
from vhil.benchclock import BenchClock, BenchStalled, host
from vhil.system import System

_bench = None
_operator = None
_clock: BenchClock | None = None
_stepped: dict = {}         # item nodeid -> the step to do after it
_started: dict = {}         # item nodeid -> (host s, bench s) at its start
_rates: list = []           # (nodeid, bench s, host s) per test


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
    g.addoption("--vhil-host-clock", action="store_true",
                help="time the tests by the host's clock, not the bench's (vhil/benchclock.py)")


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
    global _bench, _operator, _clock
    firmware = _firmware(config)
    if not firmware:
        return
    # The AMS's card is an image the operator steps can pull and read.
    params, card_dirs = operator.prepare(Path(config.getoption("--vhil-system")))
    _bench = VirtualBench(
        ifs_hil=Path(config.rootpath), firmware=firmware,
        system=Path(config.getoption("--vhil-system")),
        renode=config.getoption("--vhil-renode"),
        socketcan=config.getoption("--vhil-socketcan"),
        socket_path=config.getoption("--vhil-socket"),
        log_path=Path(config.getoption("--vhil-log")),
        params=params, card_dirs=card_dirs,
    ).start()
    # Before collection: IFS_HIL's gated modules read their env vars on import.
    _operator = operator.Operator(_bench)
    if not config.getoption("--vhil-host-clock"):
        _clock = BenchClock(_bench.clock_path).install()


def _bench_now():
    """(host s, bench s) now, or None without the bench clock or once the
    emulation stalled (the test that saw it fails with BenchStalled)."""
    if _clock is None:
        return None
    try:
        return host.monotonic(), _clock.virtual()
    except BenchStalled:
        return None


def pytest_runtest_logstart(nodeid, location):
    now = _bench_now()
    if now is not None:
        _started[nodeid] = now


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """After teardown: how much bench time the test took, in how much host
    time. Below real time the test simply took longer on the host; its
    deadlines were the firmware's."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "teardown":
        return
    start, now = _started.pop(item.nodeid, None), _bench_now()
    if start is None or now is None:
        return
    host_s, bench_s = now[0] - start[0], now[1] - start[1]
    _rates.append((item.nodeid, bench_s, host_s))
    rate = bench_s / host_s if host_s > 0 else float("nan")
    # pytest prints a teardown report's sections whose name says teardown.
    report.sections.append(("vhil bench time, setup to teardown", f"{bench_s:.2f} s of bench (firmware) time in "
                            f"{host_s:.2f} s of host time: {rate:.2f}x real time"))


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
    # Gap paths are relative to the IFS_HIL checkout pytest runs from. Not the
    # nodeid: that is relative to the rootdir, which a pytest.ini further up
    # (this repo's, when IFS_HIL is checked out inside it) moves.
    for item in items:
        key = _key(config, item)
        if key is None:
            continue
        for gap in gaps:
            if "systems" in gap and _bench.system.id not in gap["systems"]:
                continue
            if key.startswith(gap["path"]):
                item.add_marker(pytest.mark.skip(reason=gap_reason(gap)))
                break
        # The adapter IFS_HIL's bus-off stub stands for is the fault hook here.
        if (_operator is not None and _operator.busoff
                and key.split("::")[0] in operator.BUSOFF_MODULES
                and hasattr(item.module, "_inject_busoff")):
            item.module._inject_busoff = _operator.inject_busoff


def _key(config, item) -> str | None:
    """path::name of a test, the path relative to the IFS_HIL checkout pytest
    runs from (configs/gaps.yaml, operator.STEPS)."""
    try:
        path = item.path.relative_to(config.invocation_params.dir).as_posix()
    except ValueError:
        return None
    return path + "::" + item.name


def _skipped(item) -> bool:
    """Whether a skip or a true skipif marker skips the test before it runs."""
    if any(True for _ in item.iter_markers("skip")):
        return True
    for mark in item.iter_markers("skipif"):
        conditions = mark.args or (mark.kwargs.get("condition"),)
        if any(not isinstance(c, str) and c for c in conditions):
            return True
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """The bench operator's step before a gated case, before its fixtures
    (vhil/bench_operator.py)."""
    if _operator is None:
        return
    steps = operator.STEPS.get(_key(item.config, item) or "")
    if steps is None or _skipped(item):
        return
    before, after = steps
    _operator.step(before)
    _stepped[item.nodeid] = after


def pytest_runtest_teardown(item, nextitem):
    if _operator is not None and item.nodeid in _stepped:
        _operator.step(_stepped.pop(item.nodeid))


def gap_reason(gap: dict) -> str:
    """The skip reason for a configs/gaps.yaml entry: its why, and the native
    tests that cover the case instead, if any."""
    reason = f"vhil gap: {gap['why']}"
    if gap.get("replaced_by"):
        reason += "; native: " + ", ".join(gap["replaced_by"])
    return reason


def pytest_sessionfinish(session, exitstatus):
    """Stop Renode (so its log is complete), then fail the session if the
    firmware touched hardware the bench does not model and nobody explained
    it in configs/peripherals.yaml."""
    global _bench, _clock
    if _bench is None:
        return
    if _clock is not None:
        _clock.uninstall()
        _clock = None
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
    if _clock is not None:
        _clock.uninstall()
    if _bench is not None:
        _bench.stop()


def pytest_terminal_summary(terminalreporter):
    """The bench's speed over the session: how much longer than real time it
    took, and the slowest test."""
    if not _rates:
        return
    bench_s = sum(r[1] for r in _rates)
    host_s = sum(r[2] for r in _rates)
    slowest = min((r for r in _rates if r[2] > 1.0), key=lambda r: r[1] / r[2], default=None)
    line = (f"vhil bench clock: {bench_s:.0f} s of bench time in {host_s:.0f} s of host time "
            f"({bench_s / host_s:.2f}x real time)")
    if slowest is not None:
        line += f"; slowest {slowest[0]} at {slowest[1] / slowest[2]:.2f}x"
    terminalreporter.write_line(line)


def pytest_report_header(config):
    if _bench is not None:
        images = ", ".join(f"{b}={p.name}" for b, p in _bench.firmware.items())
        return (f"vhil: system {_bench.system.id} ({images}), "
                f"socketcan={'on' if _bench.socketcan else 'off'}, "
                f"clock={'host' if _clock is None else 'bench'}")
