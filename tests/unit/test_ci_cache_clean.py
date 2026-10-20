"""scripts/ci-cache-clean.py: what the Actions cache clean-up deletes."""
import datetime as dt
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "ci_cache_clean", Path(__file__).resolve().parents[2] / "scripts" / "ci-cache-clean.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

NOW = dt.datetime(2026, 10, 20, 12, tzinfo=dt.timezone.utc)


def _e(i, key, ref, created_days_ago=0.0, read_days_ago=0.0, size=1):
    t = lambda d: (NOW - dt.timedelta(days=d)).isoformat().replace("+00:00", "Z")
    return {"id": i, "key": key, "ref": ref, "size_in_bytes": size,
            "created_at": t(created_days_ago), "last_accessed_at": t(read_days_ago)}


def test_prefixes():
    assert cc.prefix("index-renode-1-5a70b200#28") == "index-renode-1-5a70b200"
    assert cc.prefix("vhil-fw-v1-Linux-ifs-hil-23c93b") == "vhil-fw-v1-Linux-ifs-hil-"
    assert cc.prefix("gcc-arm-none-eabi-14.2.1") == "gcc-arm-none-eabi-14.2.1"


def test_closed_pr_entries_go_open_ones_stay():
    entries = [_e(1, "buildkit-blob-1-sha256:a", "refs/pull/5/merge"),
               _e(2, "vhil-fw-v1-Linux-sim-x", "refs/pull/6/merge")]
    doomed = {e["id"] for e, _ in cc.plan(entries, {5: "closed", 6: "open"}, NOW)}
    assert doomed == {1}
    assert {e["id"] for e, _ in cc.plan(entries, {5: "closed"}, NOW, only_pr=6)} == set()


def test_dev_keeps_the_newest_of_each_kind_and_read_blobs():
    dev = "refs/heads/dev"
    entries = [_e(1, "index-renode-1-h#27", dev, created_days_ago=2),
               _e(2, "index-renode-1-h#28", dev, created_days_ago=1),
               _e(3, "vhil-fw-v1-Linux-sim-aaa", dev, created_days_ago=3),
               _e(4, "vhil-fw-v1-Linux-sim-bbb", dev, created_days_ago=1),
               _e(5, "vhil-fw-v1-Linux-smoke-ccc", dev, created_days_ago=9),
               _e(6, "buildkit-blob-1-sha256:old", dev, created_days_ago=9, read_days_ago=5),
               _e(7, "buildkit-blob-1-sha256:live", dev, created_days_ago=9, read_days_ago=0.5),
               _e(8, "gcc-arm-none-eabi-14.2.1", dev, created_days_ago=11),
               _e(9, "index-renode-1-h#3", "refs/heads/other", created_days_ago=8, read_days_ago=8)]
    assert {e["id"] for e, _ in cc.plan(entries, {}, NOW)} == {1, 3, 6}
