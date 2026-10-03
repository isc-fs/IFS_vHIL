"""Native tests: a system in Renode, driven and observed in virtual time.

    python -m pytest tests/sim --ecu-elf ECU08.elf

Each test module starts its own Sim; tests that need a fresh boot start one
themselves. Images default to $VHIL_<BOARD>_ELF (VHIL_ECU_ELF, ...); a test
whose image is missing is skipped, so `pytest tests` on a host without
firmware still runs the unit tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vhil.bench import REPO
from vhil.sim import Sim
from vhil.system import System


@pytest.fixture(scope="session")
def firmware(request):
    def get(board: str) -> Path:
        elf = request.config.getoption(f"--{board}-elf")
        if not elf or not Path(elf).is_file():
            env = f"VHIL_{board.upper().replace('-', '_')}_ELF"
            pytest.skip(f"no {board} image (--{board}-elf or {env})")
        return Path(elf)
    return get


@pytest.fixture(scope="session")
def make_sim(request, firmware):
    """Factory: make_sim("ecu", advance_immediately=False) -> a started Sim,
    stopped at the end of the session."""
    sims = []
    log_dir = request.config.getoption("--sim-log-dir")

    def make(system: str, **kwargs) -> Sim:
        sys_file = REPO / "systems" / f"{system}.yaml"
        fw = {}
        for b in System(sys_file).boards.values():
            fw[b.name] = firmware(b.name)
            if b.bootloader is not None:
                fw[f"{b.name}.bootloader"] = firmware(b.bootloader["id"])
        sim = Sim(sys_file, fw, **_with_log(kwargs, log_dir, system, len(sims))).start()
        sims.append(sim)
        return sim

    yield make
    for sim in sims:
        sim.stop()


def _with_log(kwargs, log_dir, system, n):
    if log_dir and "log_path" not in kwargs:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        kwargs = dict(kwargs, log_path=Path(log_dir) / f"{system}-{n}.log")
    return kwargs
