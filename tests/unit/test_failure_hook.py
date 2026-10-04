"""The failure-snapshot hook of tests/sim/conftest.py, end to end on a fake Sim:
a deliberately failing inner suite run with pytester. The snapshot lands in
the report next to the original assertion, a pass stays a pass, and a broken
monitor is reported instead of raised."""
from pathlib import Path

pytest_plugins = "pytester"

REPO = Path(__file__).resolve().parents[2]

INNER = '''
import sys
sys.path.insert(0, {repo!r})
import pytest
from vhil import sim as sim_module
from tests.unit.test_snapshot import FakeSim, _image


@pytest.fixture
def fake(tmp_path):
    f = FakeSim(_image(tmp_path))
    sim_module._live.append(f)
    yield f
    sim_module._live.remove(f)


def test_fails(fake):
    fake.last_activity = sim_module.activity()
    assert 1 == 2, "the original assertion"


def test_passes(fake):
    fake.last_activity = sim_module.activity()


def test_untouched_sim_is_not_snapshotted(fake):
    assert False, "no sim touched"


def test_broken_monitor(fake):
    fake.broken = True
    fake.last_activity = sim_module.activity()
    assert False, "second failure"


class WithSim(FakeSim):
    __enter__ = lambda self: self
    __exit__ = sim_module.Sim.__exit__
    stop = lambda self: None


def test_with_block(tmp_path):
    with WithSim(_image(tmp_path)) as s:
        s.last_activity = sim_module.activity()
        assert False, "inside with"
'''


def _run(pytester, *args):
    pytester.makeconftest((REPO / "tests" / "conftest.py").read_text())
    sim_dir = pytester.mkdir("sim")
    (sim_dir / "conftest.py").write_text((REPO / "tests" / "sim" / "conftest.py").read_text())
    (sim_dir / "test_inner.py").write_text(INNER.format(repo=str(REPO)))
    return pytester.runpytest_inprocess("sim", "-p", "no:cacheprovider", *args)


def test_snapshot_in_the_report(pytester):
    result = _run(pytester)
    result.assert_outcomes(failed=4, passed=1)
    out = result.stdout.str()
    assert "the original assertion" in out and "second failure" in out and "inside with" in out
    assert out.count("vhil snapshot") == 3          # not for the untouched one
    assert "ecu @ 1.500000 s: PC 0x08020104 main+0x4" in out
    assert "task 'CanTask'" in out
    assert "snapshot step(s) failed" in out


def test_full_snapshots_go_to_the_log_dir(pytester):
    result = _run(pytester, "--sim-log-dir", "logs")
    result.assert_outcomes(failed=4, passed=1)
    files = sorted(p.name for p in (pytester.path / "logs" / "failures").rglob("*.txt"))
    assert len(files) == 3 and all(f.endswith("-ecu-ecu.txt") for f in files)
    assert "full snapshot: logs/failures/" in result.stdout.str()
