"""The vHIL's own test suite: every committed scenario of every system
(systems/<system>.scenarios/*.yaml, docs/scenarios.md) run as one test.

    python -m pytest tests/scenarios --junitxml=scenarios.xml       # images from $VHIL_<X>_ELF

Each scenario runs as the web app's worker runs it (vhil/worker.py
execute_run on vhil.sim.Sim, from power-on through each board's CAN
bootloader), and its expects are checked against the trace in virtual time
(vhil/expect.py) with the contract the images' own .def files give. A test
fails when an expect does, listing each with its evidence; each expect is
also a JUnit property of the test (`expect[i] <name>`: pass or fail and the
detail). A scenario whose system's images are missing is skipped, so
`pytest tests` on a host without firmware still runs.

CI (.github/workflows/scenarios.yml) runs it with `full-ci`, nightly and on
dispatch, advisory for now (owner decision: scenario tests don't gate
firmware PRs yet).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")      # vhil.server: the schema and the contract

from vhil import expect as vexpect  # noqa: E402
from vhil.server.decode import system_contract  # noqa: E402
from vhil.server.runs import Limits  # noqa: E402
from vhil.server.scenarios import check_rows, load_text  # noqa: E402
from vhil.system import REPO, System  # noqa: E402
from vhil.worker import TraceWriter, execute_run  # noqa: E402

SCENARIOS = sorted(REPO.glob("systems/*.scenarios/*.yaml"))


def _id(path: Path) -> str:
    return f"{path.parent.name.removesuffix('.scenarios')}/{path.stem}"


@pytest.mark.parametrize("path", SCENARIOS, ids=_id)
def test_scenario(path, images, tmp_path, request, record_property):
    system_id = path.parent.name.removesuffix(".scenarios")
    system_path = REPO / "systems" / f"{system_id}.yaml"
    system = System(system_path)
    run, _, errors = load_text(path.read_text(), system_id, path.stem)
    assert not errors, errors
    firmware = images(system_id)
    elfs = {b: Path(p) for b, p in firmware.items() if "." not in b}
    contract = system_contract(system, elfs)
    errors, _ = check_rows(run, system, contract, elfs, Limits())
    assert not errors, errors
    scenario = run.model_dump()

    from vhil.sim import Sim
    log_dir = request.config.getoption("--sim-log-dir")
    log = Path(log_dir) / f"scenario-{system_id}-{path.stem}.log" if log_dir else None
    trace = TraceWriter(tmp_path / "trace.jsonl")
    with Sim(system_path, firmware, log_path=log) as sim:
        execute_run(sim, scenario, trace)
        end_us = sim.now_us()
    trace.close()
    results = vexpect.evaluate_trace(tmp_path / "trace.jsonl", scenario["expect"], contract,
                                     end_us)
    lines = []
    for r in results:
        verdict = "pass" if r["passed"] else "FAIL"
        record_property(f"expect[{r['index']}] {r['name'] or r['check']}",
                        f"{verdict}: {r['signal']}: {r['detail']}")
        lines.append(f"  {verdict}  {r['name'] or r['check']} {r['signal']}: {r['detail']}")
    failed = [r for r in results if not r["passed"]]
    assert not failed, f"{len(failed)} of {len(results)} expects failed:\n" + "\n".join(lines)
