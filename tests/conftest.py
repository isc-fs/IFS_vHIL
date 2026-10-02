"""Options shared by the test tree. They live here, not in tests/sim, because
pytest only registers options from conftests it loads at startup, and
`pytest tests --ecu-elf ...` would not load tests/sim/conftest.py in time."""
import os

BOARDS = ("ecu", "ams")


def pytest_addoption(parser):
    g = parser.getgroup("vhil-sim", "native tests in virtual time (IFS_vHIL)")
    for board in BOARDS:
        g.addoption(f"--{board}-elf", default=os.environ.get(f"VHIL_{board.upper()}_ELF"),
                    help=f"{board} image (default: $VHIL_{board.upper()}_ELF)")
    g.addoption("--sim-log-dir", default=None, help="keep each Sim's Renode log here")
