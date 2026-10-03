"""Options shared by the test tree. They live here, not in tests/sim, because
pytest only registers options from conftests it loads at startup, and
`pytest tests --ecu-elf ...` would not load tests/sim/conftest.py in time."""
import os

# Firmware ids (catalog/firmware) a native test may need an image of.
BOARDS = ("ecu", "ams", "can-bootloader")


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


def pytest_configure(config):
    """Instrument every Sim of the run (vhil/sim.py INSTRUMENT), including the
    ones a test module builds itself."""
    from vhil import sim
    sim.INSTRUMENT.trace = config.getoption("--vhil-trace") or 0
