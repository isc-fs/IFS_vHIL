"""Options shared by the test tree. They live here, not in tests/sim, because
pytest only registers options from conftests it loads at startup, and
`pytest tests --ecu-elf ...` would not load tests/sim/conftest.py in time."""
import os
import shutil
from pathlib import Path

import pytest

# Firmware ids (catalog/firmware) a native test may need an image of.
BOARDS = ("ecu", "ams", "udv", "can-bootloader")

_COVERAGE = pytest.StashKey[str]()


def pytest_addoption(parser):
    g = parser.getgroup("vhil-sim", "native tests in virtual time (IFS_vHIL)")
    for board in BOARDS:
        env = f"VHIL_{board.upper().replace('-', '_')}_ELF"
        g.addoption(f"--{board}-elf", default=os.environ.get(env),
                    help=f"{board} image (default: ${env})")
    g.addoption("--sim-log-dir", default=None,
                help="keep each Sim's Renode log here, and failure snapshots in its failures/")
    g.addoption("--vhil-trace", nargs="?", type=int, const=4096, default=0, metavar="BLOCKS",
                help="keep the last BLOCKS translation blocks per CPU (default 4096) for "
                     "failure snapshots; the emulation runs 5-8x slower")
    g.addoption("--vhil-coverage", default=None, metavar="DIR",
                help="write firmware coverage (lcov, function lists, summary) to DIR")


def pytest_configure(config):
    """Instrument every Sim of the run (vhil/sim.py INSTRUMENT), including the
    ones a test module builds itself."""
    from vhil import sim
    sim.INSTRUMENT.trace = config.getoption("--vhil-trace") or 0
    _record_systems(sim, os.environ.get("VHIL_IMPACT_RECORD"))
    _gate_quantum(sim)
    cov = config.getoption("--vhil-coverage")
    if cov:
        # A fresh run: logs left by an earlier one would be counted again.
        shutil.rmtree(Path(cov) / "raw", ignore_errors=True)
        sim.INSTRUMENT.coverage_dir = Path(cov).resolve()


def _record_systems(sim, path):
    """VHIL_IMPACT_RECORD=<file>: append the id of every system a Sim of this
    run starts, one per line (the nightly impact map, scripts/sim-shard.sh,
    scripts/ci-audit.py). Changes nothing a test sees."""
    if not path:
        return
    start = sim.Sim.start

    def recording_start(self, *args, **kwargs):
        with open(path, "a") as f:
            f.write(f"{self.system.id}\n")
        return start(self, *args, **kwargs)

    sim.Sim.start = recording_start


# The test being run (its setup, call and teardown): _gate_quantum asks it
# whether it opted in to the 1 ms quantum.
_ITEM: list = [None]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _ITEM[0] = item
    try:
        yield
    finally:
        _ITEM[0] = None


def _gate_quantum(sim):
    """The gate's guard (#348): every Sim of this test tree (the sim, smoke
    and scenario suites, every required check's) starts at the 500 us sync
    quantum, unless its test is marked `quantum_opt_in` (pytest.ini): the
    1 ms quantum's own determinism test, which checks the opt-in and gates
    nothing else. A Sim started outside a test (a session fixture's first
    use is inside one) is held to 500 us too."""
    from vhil.system import require_gate_quantum
    start = sim.Sim.start

    def gated_start(self, *args, **kwargs):
        item = _ITEM[0]
        if item is None or item.get_closest_marker("quantum_opt_in") is None:
            require_gate_quantum(self.quantum_us, f"{self.system.id}'s Sim")
        return start(self, *args, **kwargs)

    sim.Sim.start = gated_start


def pytest_sessionfinish(session):
    """Every Sim has stopped (their fixtures are torn down): report coverage."""
    cov = session.config.getoption("--vhil-coverage")
    if not cov or not (Path(cov) / "raw").is_dir():
        return
    from vhil import coverage
    covs = coverage.report([Path(cov) / "raw"], Path(cov))
    session.config.stash[_COVERAGE] = coverage.summary_text(covs, files=False)


def pytest_terminal_summary(terminalreporter, config):
    text = config.stash.get(_COVERAGE, None)
    if text:
        terminalreporter.section("firmware coverage")
        terminalreporter.write(text)
        terminalreporter.write_line(f"per file: {config.getoption('--vhil-coverage')}/summary.txt")


def pytest_report_header(config):
    """The Renode the native tests run (RENODE): the release and the vHIL's
    patch set (docker/renode/), so a log says which build it came from."""
    from vhil import renode as rn
    from vhil.bench import DEFAULT_RENODE
    return f"vhil: {rn.build_id(DEFAULT_RENODE)}"
