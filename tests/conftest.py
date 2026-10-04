"""Options shared by the test tree. They live here, not in tests/sim, because
pytest only registers options from conftests it loads at startup, and
`pytest tests --ecu-elf ...` would not load tests/sim/conftest.py in time."""
import os
import shutil
from pathlib import Path

import pytest

# Firmware ids (catalog/firmware) a native test may need an image of.
BOARDS = ("ecu", "ams", "can-bootloader")

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
    cov = config.getoption("--vhil-coverage")
    if cov:
        # A fresh run: logs left by an earlier one would be counted again.
        shutil.rmtree(Path(cov) / "raw", ignore_errors=True)
        sim.INSTRUMENT.coverage_dir = Path(cov).resolve()


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
