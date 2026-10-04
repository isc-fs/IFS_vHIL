"""Native tests: a system in Renode, driven and observed in virtual time.

    python -m pytest tests/sim --ecu-elf ECU08.elf

Each test module starts its own Sim; tests that need a fresh boot start one
themselves. Images default to $VHIL_<BOARD>_ELF (VHIL_ECU_ELF, ...); a test
whose image is missing is skipped, so `pytest tests` on a host without
firmware still runs the unit tests.

A failing test gets a snapshot of each Sim it touched (below). Options
(tests/conftest.py): --sim-log-dir keeps Renode logs and full snapshots,
--vhil-trace adds the last blocks each CPU ran, --vhil-coverage DIR writes
firmware coverage (vhil/coverage.py).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from vhil import sim as sim_module, snapshot
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


# -- failure snapshots ---------------------------------------------------------
#
# When a test fails (in setup or in its body), each Sim it touched gets a
# snapshot per board (vhil/snapshot.py): a few lines in the report, the full
# text under <sim-log-dir>/failures/. The Sims it touched are the ones that
# ran a monitor command since the test started; a Sim the test built in a
# `with` block is already stopped by then, so it is snapshotted on its way
# out (INSTRUMENT.on_error_exit). Collecting never fails or hides a test: any
# error lands in the report section instead.

_MARK = pytest.StashKey[int]()
_exited: list[tuple[int, str, list]] = []   # (activity mark, system id, snapshots)


def _on_error_exit(sim) -> None:
    _exited.append((sim.last_activity, sim.system.id, snapshot.take_all(sim)))


sim_module.INSTRUMENT.on_error_exit = _on_error_exit


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    item.stash[_MARK] = sim_module.activity()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not report.failed or call.when == "teardown":
        return
    try:
        text = _snapshot_report(item, report.when)
    except Exception as e:  # noqa: BLE001 - never turn into another failure
        text = f"snapshot failed: {type(e).__name__}: {e}"
    if text:
        report.sections.append(("vhil snapshot", text))


def _snapshot_report(item, when: str):
    mark = item.stash.get(_MARK, 0)
    taken = [(s.system.id, snapshot.take_all(s)) for s in sim_module.live_sims()
             if getattr(s, "last_activity", 0) > mark]
    taken += [(system, snaps) for m, system, snaps in _exited if m > mark]
    _exited.clear()
    if not taken:
        return None
    log_dir = item.config.getoption("--sim-log-dir")
    out_dir = None
    if log_dir:
        safe = re.sub(r"[^\w.-]+", "_", item.nodeid).strip("_")[:150]
        out_dir = Path(log_dir) / "failures" / f"{safe}-{when}"
        out_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for n, (system, snaps) in enumerate(taken):
        for snap in snaps:
            lines.append(snap.summary())
            if out_dir is not None:
                path = out_dir / f"{n}-{system}-{snap.board}.txt"
                path.write_text(f"{item.nodeid} ({when})\n" + snap.text())
                lines.append(f"  full snapshot: {path}")
            else:
                lines.append(snap.text(full_ring=False))
    return "\n".join(lines)


def _with_log(kwargs, log_dir, system, n):
    if log_dir and "log_path" not in kwargs:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        kwargs = dict(kwargs, log_path=Path(log_dir) / f"{system}-{n}.log")
    return kwargs
