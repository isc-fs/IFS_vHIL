"""vhil.fwprune: which commit builds a prune pass removes, and that
built.txt stays consistent with the directories."""
import fcntl
import os
import time
from pathlib import Path

import pytest

from vhil import fwprune
from vhil.fwprune import PrunePolicy, prune
from vhil.server.githost import FakeBranchDetails, FakeRefLister
from vhil.system import REPO, System, commit_source_dir

NOW = 2_000_000_000.0
DAY = 86400
REPO_ECU = "isc-fs/IFS08-CE-ECU"
RECIPE = "0123456789"


def sha(n: int) -> str:
    return f"{n:012x}" + "a" * 28


@pytest.fixture
def catalog(tmp_path):
    d = tmp_path / "catalog" / "firmware"
    d.mkdir(parents=True)
    (d / "ecu.yaml").write_text(f"id: ecu\nrepo: {REPO_ECU}\nref: dev\nbuild: {{elf: build/E.elf}}\n")
    return tmp_path / "catalog"


@pytest.fixture
def fw(tmp_path):
    d = tmp_path / "fw"
    d.mkdir()
    return d


def make_build(fw: Path, n: int, days_ago: float, firmware: str = "ecu", listed: bool = True,
               size: int = 1000) -> Path:
    src = fw / f"{firmware}+{sha(n)[:12]}.{RECIPE}"
    (src / "build").mkdir(parents=True)
    (src / "build" / "E.elf").write_bytes(b"x" * size)
    t = NOW - days_ago * DAY
    (src / fwprune.LAST_USED).touch()
    os.utime(src / fwprune.LAST_USED, (t, t))
    if listed:
        with open(fw / "built.txt", "a") as f:
            f.write(f"{firmware}={src / 'build' / 'E.elf'}\n")
    return src


def refs(*branches) -> FakeRefLister:
    return FakeRefLister({REPO_ECU: {"branches": [{"name": n, "sha": s} for n, s in branches],
                                     "tags": []}})


def run(fw, catalog, policy=PrunePolicy(), lister=None, **kw):
    kw.setdefault("log", lambda m: None)
    return prune(fw, policy, catalog=catalog, lister=lister or refs(), now=NOW, **kw)


def names(builds):
    return sorted(b.name for b in builds)


def test_keeps_the_last_n_used_and_rewrites_the_index(fw, catalog):
    old = [make_build(fw, i, 30 + i) for i in range(12)]          # 0 is the most recent
    legacy = fw / "ecu@dev" / "build" / "E.elf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"x")
    with open(fw / "built.txt", "a") as f:
        f.write(f"ecu={legacy}\n")
    before = (fw / "built.txt").read_text().splitlines()
    result = run(fw, catalog)
    assert names(result.removed) == sorted(p.name for p in old[10:])
    assert result.freed == 2 * 1000
    assert all(not p.exists() for p in old[10:]) and all(p.exists() for p in old[:10])
    assert legacy.is_file()                                         # ref-name builds stay
    after = (fw / "built.txt").read_text().splitlines()
    assert after == [line for line in before if not any(p.name in line for p in old[10:])]
    assert any("ecu@dev" in line for line in after)


def test_keeps_everything_used_within_the_window(fw, catalog):
    recent = [make_build(fw, i, i) for i in range(13)]              # 0..12 days ago
    stale = make_build(fw, 99, 15)
    result = run(fw, catalog)
    assert names(result.removed) == [stale.name]
    assert all(p.exists() for p in recent)
    by_name = {b.name: b for b in result.kept}
    assert "used within 14 d" in by_name[recent[12].name].keep
    assert not any("last used" in w for w in by_name[recent[12].name].keep)


def test_n_counts_per_firmware(fw, catalog):
    ecu = [make_build(fw, i, 30 + i) for i in range(3)]
    ams = [make_build(fw, 50 + i, 30 + i, firmware="ams") for i in range(3)]
    result = run(fw, catalog, PrunePolicy(keep=2))
    assert names(result.removed) == sorted([ecu[2].name, ams[2].name])


def test_active_branch_heads_and_the_catalogue_ref_stay(fw, catalog):
    for i in range(1, 6):
        make_build(fw, i, 100)
    lister = refs(("dev", sha(1)), ("feat/old", sha(2)), ("feat/pr", sha(3)), ("feat/new", sha(4)))
    policy = PrunePolicy(keep=0, keep_days=0)
    # No branch details (the API unreachable): every branch head stays.
    result = run(fw, catalog, policy, lister=lister, dry_run=True)
    assert names(result.removed) == [f"ecu+{sha(5)[:12]}.{RECIPE}"]
    # With details: dev by name, a PR, a recent head; feat/old is none of them.
    details = FakeBranchDetails({REPO_ECU: {
        "default_branch": "dev", "prs": {"feat/pr": [{"number": 1, "title": "", "url": ""}]},
        "commits": {sha(1): {"date": "2020-01-01T00:00:00Z"},
                    sha(2): {"date": "2020-01-01T00:00:00Z"},
                    sha(3): {"date": "2020-01-01T00:00:00Z"},
                    sha(4): {"date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}}})
    result = run(fw, catalog, policy, lister=lister, details=details)
    assert names(result.removed) == sorted(f"ecu+{sha(n)[:12]}.{RECIPE}" for n in (2, 5))
    assert {b.commit12 for b in result.kept} == {sha(n)[:12] for n in (1, 3, 4)}
    assert all("active head" in b.keep for b in result.kept)


def test_a_repo_that_cant_be_listed_keeps_all_its_builds(fw, catalog):
    make_build(fw, 1, 100)
    gone = make_build(fw, 2, 100, firmware="removed-fw")             # not in the catalogue
    result = run(fw, catalog, PrunePolicy(keep=0, keep_days=0), lister=FakeRefLister({}))
    assert names(result.removed) == [gone.name]
    assert result.kept[0].keep == ["refs unknown"]


def test_queued_and_running_runs_keep_their_commits(fw, catalog, tmp_path):
    from vhil.server.runs import RunStore
    for i in range(1, 5):
        make_build(fw, i, 100)
    store = RunStore(tmp_path / "vhil.db")
    store.create("ecu", "", {}, {"kind": "run"},
                 firmware_commits={"ecu": {"ref": "feat/x", "commit": sha(1)}})
    store.create("ecu", "", {"ecu": sha(2)}, {"kind": "run"})        # a commit as its ref
    running = store.create("ecu", "", {}, {"kind": "run"},
                           firmware_commits={"ecu": {"ref": "feat/y", "commit": sha(3)}})
    done = store.create("ecu", "", {}, {"kind": "run"},
                        firmware_commits={"ecu": {"ref": "feat/z", "commit": sha(4)}})
    store.claim("w1")                                               # the first: running
    for run_id in (running, done):
        store._write("UPDATE runs SET state = ? WHERE id = ?",
                     ("running" if run_id == running else "passed", run_id))
    result = run(fw, catalog, PrunePolicy(keep=0, keep_days=0), db=tmp_path / "vhil.db")
    assert names(result.removed) == [f"ecu+{sha(4)[:12]}.{RECIPE}"]
    assert all("queued/running run" in b.keep for b in result.kept)


def test_a_build_in_progress_skips_the_pass(fw, catalog):
    src = make_build(fw, 1, 100)
    with open(fw / fwprune.BUILD_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = run(fw, catalog, PrunePolicy(keep=0, keep_days=0))
    assert result.skipped and "build lock" in result.skipped and not result.removed
    assert src.exists()
    assert run(fw, catalog, PrunePolicy(keep=0, keep_days=0)).removed


def test_dry_run_removes_nothing(fw, catalog):
    srcs = [make_build(fw, i, 100) for i in range(3)]
    index = (fw / "built.txt").read_text()
    result = run(fw, catalog, PrunePolicy(keep=1, keep_days=0), dry_run=True)
    assert len(result.removed) == 2 and result.freed == 2000
    assert all(p.exists() for p in srcs) and (fw / "built.txt").read_text() == index
    assert "would remove 2 build(s), 0.0 MB to free" in result.report()[-1]


def test_index_goes_first_so_a_crash_leaves_no_dangling_line(fw, catalog, monkeypatch):
    keep = make_build(fw, 1, 0)
    lost = make_build(fw, 2, 100)
    unlisted = make_build(fw, 3, 100, listed=False)                 # a failed build

    class Crash(BaseException):
        pass

    def crash(*a, **k):
        raise Crash
    monkeypatch.setattr(fwprune.shutil, "rmtree", crash)
    with pytest.raises(Crash):
        run(fw, catalog, PrunePolicy(keep=1, keep_days=0))
    # The index already forgets the build; its directory is still there.
    assert lost.name not in (fw / "built.txt").read_text() and lost.exists()
    assert keep.name in (fw / "built.txt").read_text()
    monkeypatch.undo()
    result = run(fw, catalog, PrunePolicy(keep=1, keep_days=0))
    assert names(result.removed) == sorted([lost.name, unlisted.name])
    assert not lost.exists() and not unlisted.exists() and keep.exists()
    assert [b.listed for b in result.removed] == [False, False]
    assert not list(fw.glob("built.txt.*.tmp"))


def test_size_cap_prunes_oldest_first_but_never_protected(fw, catalog):
    head = make_build(fw, 1, 50, size=4000)                          # dev's head
    mid = make_build(fw, 2, 3, size=4000)
    new = make_build(fw, 3, 1, size=4000)
    policy = PrunePolicy(keep=10, keep_days=14, max_bytes=9000)
    result = run(fw, catalog, policy, lister=refs(("dev", sha(1))))
    assert names(result.removed) == [mid.name]                       # head can't go
    assert head.exists() and new.exists()


def test_last_use_falls_back_to_the_stamp(fw):
    src = fw / f"ecu+{sha(1)[:12]}.{RECIPE}"
    src.mkdir()
    (src / ".vhil-build.json").write_text('{"refs": {"dev": "2030-01-01T00:00:00+00:00", '
                                          '"v1": "2029-01-01T00:00:00+00:00"}}')
    assert fwprune.last_used(src) == pytest.approx(1893456000.0)
    fwprune.touch_used(src)
    assert abs(fwprune.last_used(src) - time.time()) < 60


def test_the_resolver_marks_the_builds_it_reuses(tmp_path):
    from vhil.worker import FirmwareResolver
    system = System(REPO / "systems" / "ecu.yaml")
    commits = {"ecu": sha(10), "ecu.bootloader": sha(11)}
    lines = []
    for key, c in commits.items():
        board, _, part = key.partition(".")
        doc = system.boards[board].bootloader if part else system.boards[board].firmware
        elf = commit_source_dir(tmp_path, doc, c) / doc["build"]["elf"]
        elf.parent.mkdir(parents=True)
        elf.write_bytes(b"\x7fELF")
        lines.append(f"{key}={elf}")
    (tmp_path / "built.txt").write_text("\n".join(lines) + "\n")
    out = FirmwareResolver(tmp_path, build=False).resolve(system, {}, commits)
    for key, elf in out.items():
        src = next(p for p in elf.parents if fwprune.BUILD_DIR.fullmatch(p.name))
        assert (src / fwprune.LAST_USED).is_file(), key


def test_due_throttles_across_workers(fw):
    assert fwprune.due(fw, 3600, now=NOW)
    assert not fwprune.due(fw, 3600, now=NOW + 10)
    assert fwprune.due(fw, 3600, now=NOW + 3601)


def test_policy_from_env(monkeypatch):
    assert PrunePolicy.from_env() == PrunePolicy(10, 14.0, 0, 3600.0)
    monkeypatch.setenv("VHIL_FW_KEEP", "3")
    monkeypatch.setenv("VHIL_FW_KEEP_DAYS", "2.5")
    monkeypatch.setenv("VHIL_FW_MAX_GB", "10")
    monkeypatch.setenv("VHIL_FW_PRUNE_INTERVAL_S", "60")
    assert PrunePolicy.from_env() == PrunePolicy(3, 2.5, 10_000_000_000, 60.0)
