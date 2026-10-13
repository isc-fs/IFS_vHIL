"""scripts/sim-balance.py: CI's split of tests/sim over shards (#260)."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sim_balance", REPO / "scripts" / "sim-balance.py")
balance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(balance)


def test_every_file_runs_on_exactly_one_shard():
    files = sorted(p.relative_to(REPO).as_posix() for p in (REPO / "tests" / "sim").glob("test_*.py"))
    shards = balance.split(files, {}, 8, 2)
    assert sorted(f for s in shards for f in s) == files


def test_longest_files_spread_and_unknown_files_count_as_the_median():
    durations = {"a": 100, "b": 90, "c": 10, "d": 10}
    shards = balance.split(["a", "b", "c", "d", "new"], durations, 2, 1)
    assert [sorted(s) for s in shards] == [["a", "c", "d"], ["b", "new"]]


def test_the_split_is_the_same_every_time():
    files = [f"f{i}" for i in range(20)]
    durations = {f: 7 for f in files}
    assert balance.split(files, durations, 3, 2) == balance.split(list(reversed(files)), durations, 3, 2)
