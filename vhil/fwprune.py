"""Pruning of the firmware volume's commit builds (#244 keyed them by commit).

    python -m vhil.worker prune [--dry-run] [--fw-dir DIR]
    scripts/vhil-docker.sh prune [--dry-run]

The worker builds each commit a run asks for into
`<fw-dir>/<firmware>+<commit[:12]>.<recipe id>/` (vhil.system
commit_source_dir) and lists its images in `<fw-dir>/built.txt`, so the
volume grows by a build (~150 MB to ~1 GB) per commit run. This removes the
ones no one is likely to run again. Per firmware id it keeps:

- the `keep` (VHIL_FW_KEEP, 10) builds used most recently,
- every build used within `keep_days` (VHIL_FW_KEEP_DAYS, 14),

and never removes a build that is

- (a) the head of an active branch of its repo (githost.active_branches: the
  default branch, dev, main, the catalogue's ref, a branch with an open PR, a
  head within VHIL_FIRMWARE_ACTIVE_DAYS) or the commit of the catalogue's
  ref. When GitHub's API can't be asked (the workers' egress allows
  github.com only) every branch's head counts as active; when the repo's
  refs can't be listed at all, none of that firmware's builds is removed;
- (b) the commit of a queued or running run (its firmware_commits, or a
  commit given as its ref);
- (c) being built: the prune takes the build lock (`.build.lock`) without
  waiting and skips the whole pass while a build holds it.

With `max_bytes` (VHIL_FW_MAX_GB, 0 = no cap) the builds the first two rules
keep are removed oldest first while the commit builds together are bigger;
(a)-(c) still hold.

Last use is the mtime of the build's `.vhil-last-used`, which the worker's
resolver touches whenever a run reuses or builds it (touch_used), else the
newest time its stamp records a ref naming it, else the directory's mtime.
The resolver touches it under a shared `.prune.lock` and a prune decides and
deletes under the exclusive one, so a build a run has just picked is never
removed under it (with keep_days > 0).

Index: built.txt is rewritten without the removed builds' lines (to a
temporary file, then renamed over it) before any directory is deleted. A
crash in between leaves a directory no line names, which the next pass
removes again; a crash before leaves both. A commit-build directory no line
names (a failed build) is subject to the same rules.

Builds at a ref name (`<firmware>@<ref>/`: `scripts/vhil-docker.sh fw`, the
warm-up in docs/deploy.md, builds from before #244) are never removed: the
resolver's fallback when a ref can't be resolved to a commit uses them, and
the local jobs read them from built.txt by name. Remove them by hand.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml

from vhil.system import COMMIT, read_stamp

LAST_USED = ".vhil-last-used"
BUILD_LOCK = ".build.lock"
PRUNE_LOCK = ".prune.lock"
LAST_PRUNE = ".last-prune"
# <firmware id>+<commit[:12]>.<recipe id> (vhil.system commit_source_dir).
BUILD_DIR = re.compile(r"(?P<id>[A-Za-z0-9_-]+)\+(?P<commit>[0-9a-f]{12})\.(?P<recipe>[0-9a-f]{10})")


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


@dataclass(frozen=True)
class PrunePolicy:
    keep: int = 10                  # per firmware id, by last use
    keep_days: float = 14.0         # used within
    max_bytes: int = 0              # all commit builds together; 0 = no cap
    interval_s: float = 3600.0      # the worker's passes, at most this often

    @classmethod
    def from_env(cls) -> "PrunePolicy":
        return cls(keep=int(_env_float("VHIL_FW_KEEP", cls.keep)),
                   keep_days=_env_float("VHIL_FW_KEEP_DAYS", cls.keep_days),
                   max_bytes=int(_env_float("VHIL_FW_MAX_GB", 0) * 1e9),
                   interval_s=_env_float("VHIL_FW_PRUNE_INTERVAL_S", cls.interval_s))


@dataclass
class Build:
    path: Path
    firmware: str
    commit12: str
    last_used: float
    size: int
    listed: bool
    keep: list[str] = field(default_factory=list)      # why it stays

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class PruneResult:
    removed: list[Build] = field(default_factory=list)
    kept: list[Build] = field(default_factory=list)
    skipped: Optional[str] = None                      # why no pass was made
    dry_run: bool = False

    @property
    def freed(self) -> int:
        return sum(b.size for b in self.removed)

    def report(self) -> list[str]:
        if self.skipped:
            return [f"firmware prune skipped: {self.skipped}"]
        verb = "would remove" if self.dry_run else "removed"
        lines = [f"{verb} {b.name} ({_mb(b.size)}, last used {_when(b.last_used)}"
                 + ("" if b.listed else ", not in built.txt") + ")" for b in self.removed]
        lines += [f"kept {b.name} ({_mb(b.size)}, last used {_when(b.last_used)}: "
                  f"{', '.join(b.keep)})" for b in self.kept]
        lines.append(f"firmware prune: {verb} {len(self.removed)} build(s), "
                     f"{_mb(self.freed)} {'to free' if self.dry_run else 'freed'}; "
                     f"kept {len(self.kept)} ({_mb(sum(b.size for b in self.kept))})")
        return lines


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def _when(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _epoch(text) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_used(src: Path) -> float:
    """When a build was last used (module doc)."""
    try:
        return (Path(src) / LAST_USED).stat().st_mtime
    except OSError:
        pass
    times = [t for t in map(_epoch, (read_stamp(src).get("refs") or {}).values()) if t]
    if times:
        return max(times)
    return Path(src).stat().st_mtime


def touch_used(src: Path) -> None:
    """Record that a run used the build in `src` now. Best effort: a
    read-only volume (a --no-build worker) can't record it."""
    try:
        (Path(src) / LAST_USED).touch()
    except OSError:
        pass


@contextmanager
def reuse_lock(fw_dir: Path):
    """Shared: a prune can't decide or delete while it is held (module doc)."""
    try:
        f = open(Path(fw_dir) / PRUNE_LOCK, "a")
    except OSError:                     # no directory yet, or read-only
        yield
        return
    with f:
        fcntl.flock(f, fcntl.LOCK_SH)
        yield


def commit_builds(fw_dir: Path) -> list[Path]:
    return sorted(p for p in Path(fw_dir).iterdir() if p.is_dir() and BUILD_DIR.fullmatch(p.name))


def _listed_dirs(fw_dir: Path, lines: list[str]) -> dict[str, Path]:
    """built.txt line -> the top-level directory of fw_dir its ELF is in."""
    root, out = Path(fw_dir).resolve(), {}
    for line in lines:
        _, sep, path = line.partition("=")
        if not sep:
            continue
        try:
            rel = Path(path.strip()).resolve().relative_to(root)
        except ValueError:
            continue
        if rel.parts:
            out[line] = root / rel.parts[0]
    return out


def write_index(fw_dir: Path, lines: list[str]) -> None:
    """built.txt, replaced atomically."""
    listing = Path(fw_dir) / "built.txt"
    tmp = listing.with_name(f"built.txt.{os.getpid()}.tmp")
    tmp.write_text("".join(f"{line}\n" for line in lines))
    os.replace(tmp, listing)


def catalogue_firmware(catalog: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((Path(catalog) / "firmware").glob("*.yaml")):
        doc = yaml.safe_load(p.read_text()) or {}
        if doc.get("id"):
            out[doc["id"]] = doc
    return out


def protected_heads(firmware: dict[str, dict], built: dict[str, set[str]], lister,
                    details=None, active_days: float = 30.0,
                    log: Callable[[str], None] = print) -> dict[str, Optional[set[str]]]:
    """firmware id -> the commit[:12]s rule (a) protects, or None when the
    repo's refs can't be listed (then none of its builds may go). `built`:
    firmware id -> the commit[:12]s built, the only branches whose activity
    matters (fewer GitHub API calls)."""
    from vhil.server.githost import HostError, HostUnavailable, active_branches, commit_of, fresh_refs
    out: dict[str, Optional[set[str]]] = {}
    for fid, commits in built.items():
        doc = firmware.get(fid)
        if doc is None:                 # no longer in the catalogue: no heads
            out[fid] = set()
            continue
        try:
            refs = fresh_refs(lister, doc["repo"])
        except HostError as e:
            log(f"{fid}: can't list {doc['repo']} ({e}): keeping all of its builds")
            out[fid] = None
            continue
        heads = set()
        cat = commit_of(refs, doc["ref"])
        if cat:
            heads.add(cat[:12])
        branches = [b for b in refs["branches"] if b["sha"][:12] in commits]
        info = None
        if branches and details is not None:
            try:
                info = details.details(doc["repo"], branches)
            except (HostError, HostUnavailable) as e:
                log(f"{fid}: branch activity unavailable ({e}): every branch head is kept")
        if info is None:
            heads |= {b["sha"][:12] for b in branches}
        else:
            for b in active_branches(branches, info, catalogue_ref=doc["ref"], days=active_days):
                # A head the API gave no date for (a capped lookup) counts as active.
                if b["active"] or b["date"] is None:
                    heads.add(b["sha"][:12])
        out[fid] = heads
    return out


def run_commits(db: Optional[Path]) -> set[str]:
    """commit[:12]s of the queued and running runs (rule b). Raises when the
    database exists but can't be read: a pass must not go on without it."""
    if db is None or not Path(db).is_file():
        return set()
    con = sqlite3.connect(Path(db), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        rows = con.execute("SELECT firmware, firmware_commits FROM runs "
                           "WHERE state IN ('queued', 'running')").fetchall()
    finally:
        con.close()
    out = set()
    for firmware, commits in rows:
        for text in (firmware, commits):
            try:
                data = json.loads(text or "{}")
            except ValueError:
                continue
            for v in (data.values() if isinstance(data, dict) else []):
                c = v.get("commit") if isinstance(v, dict) else v
                if isinstance(c, str) and COMMIT.fullmatch(c):
                    out.add(c[:12])
    return out


def plan(builds: list[Build], policy: PrunePolicy, heads: dict[str, Optional[set[str]]],
         running: set[str], now: float) -> tuple[list[Build], list[Build]]:
    """(remove, keep) under the policy (module doc). Sets each build's
    `keep` reasons."""
    hard: set[int] = set()
    by_fw: dict[str, list[Build]] = {}
    for b in builds:
        by_fw.setdefault(b.firmware, []).append(b)
        h = heads.get(b.firmware, set())
        if h is None:
            b.keep.append("refs unknown")
        elif b.commit12 in h:
            b.keep.append("active head")
        if b.commit12 in running:
            b.keep.append("queued/running run")
        if b.keep:
            hard.add(id(b))
    for group in by_fw.values():
        group.sort(key=lambda b: -b.last_used)
        for i, b in enumerate(group):
            if i < policy.keep:
                b.keep.append(f"{i + 1} of the {policy.keep} last used")
            if now - b.last_used <= policy.keep_days * 86400:
                b.keep.append(f"used within {policy.keep_days:g} d")
    kept = [b for b in builds if b.keep]
    remove = [b for b in builds if not b.keep]
    if policy.max_bytes:
        total = sum(b.size for b in kept)
        for b in sorted((b for b in kept if id(b) not in hard), key=lambda b: b.last_used):
            if total <= policy.max_bytes:
                break
            b.keep.clear()
            total -= b.size
            remove.append(b)
        kept = [b for b in kept if b.keep]
    return remove, kept


def prune(fw_dir: Path, policy: PrunePolicy, *, catalog: Path, lister=None, details=None,
          db: Optional[Path] = None, active_days: float = 30.0, dry_run: bool = False,
          now: Optional[float] = None, log: Callable[[str], None] = print) -> PruneResult:
    """One pass (module doc)."""
    fw_dir = Path(fw_dir)
    result = PruneResult(dry_run=dry_run)
    if not fw_dir.is_dir():
        result.skipped = f"no {fw_dir}"
        return result
    lock_mode = "r" if dry_run else "a"
    try:
        build_lock = open(fw_dir / BUILD_LOCK, lock_mode)
    except FileNotFoundError:
        build_lock = None               # nothing ever built here (dry run)
    try:
        if build_lock is not None:
            try:
                fcntl.flock(build_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result.skipped = "a firmware build holds the build lock"
                return result
        with _exclusive(fw_dir / PRUNE_LOCK, dry_run):
            return _pass(fw_dir, policy, result, catalog=catalog, lister=lister,
                         details=details, db=db, active_days=active_days,
                         now=time.time() if now is None else now, log=log)
    finally:
        if build_lock is not None:
            build_lock.close()


@contextmanager
def _exclusive(path: Path, dry_run: bool):
    try:
        f = open(path, "r" if dry_run else "a")
    except FileNotFoundError:
        yield
        return
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _pass(fw_dir: Path, policy: PrunePolicy, result: PruneResult, *, catalog: Path, lister,
          details, db, active_days: float, now: float, log) -> PruneResult:
    listing = fw_dir / "built.txt"
    lines = listing.read_text().splitlines() if listing.is_file() else []
    listed = _listed_dirs(fw_dir, lines)
    listed_dirs = set(listed.values())
    builds = []
    for src in commit_builds(fw_dir):
        m = BUILD_DIR.fullmatch(src.name)
        builds.append(Build(src, m["id"], m["commit"], last_used(src), _size(src),
                            src.resolve() in listed_dirs))
    if not builds:
        return result
    running = run_commits(db)
    if lister is None:
        from vhil.server.githost import LsRemote
        lister = LsRemote()
    built: dict[str, set[str]] = {}
    for b in builds:
        built.setdefault(b.firmware, set()).add(b.commit12)
    heads = protected_heads(catalogue_firmware(catalog), built, lister, details,
                            active_days, log)
    remove, kept = plan(builds, policy, heads, running, now)
    result.removed, result.kept = remove, kept
    if result.dry_run or not remove:
        return result
    gone = {b.path.resolve() for b in remove}
    # The index first: a line never names a directory that is going.
    keep_lines = [line for line in lines if listed.get(line) not in gone]
    if keep_lines != lines:
        write_index(fw_dir, keep_lines)
    for b in remove:
        shutil.rmtree(b.path, ignore_errors=True)
        if b.path.exists():
            log(f"couldn't remove all of {b.path}")
    return result


def due(fw_dir: Path, interval_s: float, now: Optional[float] = None) -> bool:
    """True when no pass was made in the last interval_s (any worker:
    `.last-prune`'s mtime, shared through the volume), and marks one made."""
    now = time.time() if now is None else now
    stamp = Path(fw_dir) / LAST_PRUNE
    try:
        if now - stamp.stat().st_mtime < interval_s:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        return False
    return True


def branch_details(token: Optional[str] = None):
    """GitHub's branch details for rule (a), anonymous unless a token is set."""
    from vhil.server.githost import GitHubBranches
    return GitHubBranches(token or None, ttl_s=_env_float("VHIL_BRANCHES_TTL_S", 120))


def active_days() -> float:
    return _env_float("VHIL_FIRMWARE_ACTIVE_DAYS", 30.0)
