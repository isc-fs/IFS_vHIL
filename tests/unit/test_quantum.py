"""#348: the sync quantum is 500 us for every system file and the whole gate;
a non-gate run may opt in to 1 ms, and nothing else is accepted."""
import re
from pathlib import Path

import pytest

from vhil.system import (GATE_QUANTUM_US, QUANTA_US, REPO, System, SystemError, main,
                         quantum_us, require_gate_quantum)

SYSTEMS = sorted((REPO / "systems").glob("*.yaml")) + sorted(
    (REPO / "scripts" / "speed-systems").glob("*.yaml"))
ECU_AMS = REPO / "systems" / "ecu-ams.yaml"


def test_only_500_us_and_1_ms_are_accepted():
    assert QUANTA_US == (500, 1000) and GATE_QUANTUM_US == 500
    assert quantum_us(500) == 500 and quantum_us(1000) == 1000 and quantum_us("1000") == 1000
    for bad in (0, 100, 250, 750, 2000, -500, True, 1000.0, "1 ms", "", None):
        with pytest.raises(SystemError, match="only 500 or 1000 us"):
            quantum_us(bad)


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_every_system_file_is_at_the_gate_quantum(path):
    """1 ms is a run option, never a system file's: every system the gate
    runs (and fingerprints) syncs every 500 us."""
    assert System(path).sync_quantum_us() == GATE_QUANTUM_US


def _script(system: System) -> str:
    return system.render_renode({b: Path(f"/fw/{b}.elf") for b in system.boards}, lockstep=True)


def test_the_gate_quantum_leaves_the_generated_script_as_it_was():
    plain = _script(System(ECU_AMS))
    s = System(ECU_AMS)
    assert s.set_quantum_us(500) == 500
    assert _script(s) == plain
    assert 'emulation SetGlobalQuantum "0.0005"' in plain


def test_the_1ms_opt_in_sets_the_run_quantum_only():
    s = System(ECU_AMS)
    assert s.set_quantum_us(1000) == 1000 and s.sync_quantum_us() == 1000
    script = _script(s)
    assert 'emulation SetGlobalQuantum "0.001"' in script and "0.0005" not in script
    assert System(ECU_AMS).sync_quantum_us() == 500       # the file is untouched
    with pytest.raises(SystemError):
        s.set_quantum_us(2000)


def test_render_takes_the_quantum_and_refuses_others(capsys):
    assert main(["render", str(ECU_AMS), "--quantum-us", "1000"]) == 0
    assert 'SetGlobalQuantum "0.001"' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["render", str(ECU_AMS), "--quantum-us", "750"])


def test_the_gate_refuses_the_1ms_quantum():
    require_gate_quantum(500, "x")
    with pytest.raises(SystemError, match="gate runs at 500 us only"):
        require_gate_quantum(1000, "a fingerprint run")


# Where the gate's jobs are defined: none of them may pick a quantum.
GATE_FILES = (sorted((REPO / ".github" / "workflows").glob("*.yml"))
              + [REPO / p for p in ("pytest.ini", "scripts/vhil-docker.sh", "scripts/sim-shard.sh",
                                    "scripts/run-ifs-hil.sh", "scripts/fingerprint.py",
                                    "ci/impact.yaml")])


@pytest.mark.parametrize("path", GATE_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_no_gate_job_selects_a_quantum(path):
    text = path.read_text()
    assert not re.search(r"--quantum-us|quantum_us\s*[=:]", text), (
        f"{path.relative_to(REPO)} selects a sync quantum: the gate runs at 500 us only (#348)")


def test_a_run_may_opt_in_but_a_scenario_file_may_not():
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from vhil.server.runs import RunScenario
    from vhil.server.scenarios import as_data, load_text
    assert RunScenario(kind="run").quantum_us == 500
    assert RunScenario(kind="run", quantum_us=1000).quantum_us == 1000
    with pytest.raises(ValidationError):
        RunScenario(kind="run", quantum_us=750)
    run, _, errors = load_text("kind: scenario\nsystem: ecu\nvirtual_ms: 3000\nquantum_us: 1000\n",
                               "ecu", "x")
    assert run is None and any(e.startswith("quantum_us:") for e in errors)
    run, desc, errors = load_text("kind: scenario\nsystem: ecu\nvirtual_ms: 3000\n", "ecu", "x")
    assert not errors and "quantum_us" not in as_data(run, desc)


def test_a_test_not_marked_quantum_opt_in_cant_start_a_sim_at_1ms():
    """tests/conftest.py: a Sim at 1 ms starts only in a test marked
    quantum_opt_in; anywhere else it is refused before Renode starts."""
    from vhil.sim import Sim
    images = {k: f"/fw/{k}.elf" for k in System(ECU_AMS).images()}
    sim = Sim(ECU_AMS, images, quantum_us=1000)
    assert sim.quantum_us == 1000
    with pytest.raises(SystemError, match="gate runs at 500 us only"):
        sim.start()
    assert Sim(ECU_AMS, images).quantum_us == 500
    with pytest.raises(SystemError):
        Sim(ECU_AMS, images, quantum_us=2000)
