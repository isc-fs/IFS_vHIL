#!/usr/bin/env python3
"""Snapshots of the web app's state (docs/deploy.md, "Backups").

    backup.py loop             snapshot every VHIL_BACKUP_INTERVAL_H hours (the service)
    backup.py once             one snapshot now
    backup.py list             the snapshots, newest first
    backup.py restore <name>   put a snapshot's DB back (stop api and worker first)

A snapshot is a directory <VHIL_BACKUP_DIR>/<UTC time>/ with

    vhil.db         the run table, copied with SQLite's online backup API: a
                    consistent copy of the live WAL database while the api and
                    workers keep writing (a raw file copy of vhil.db without
                    its -wal can lose or tear recent commits); checked with
                    PRAGMA integrity_check before it counts
    branches.bundle the workspace clone's local branches (the systems users
                    saved from the browser, some maybe never pushed), as a git
                    bundle; absent when there are none

Snapshots beyond the newest VHIL_BACKUP_KEEP are deleted. Run traces and
firmware builds are not included: traces are large and only history, firmware
is rebuilt from its source (docs/deploy.md says how to copy traces if wanted).
Only the Python standard library and git: runs in the ifs-vhil image.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.environ.get("VHIL_DB", "/data/db/vhil.db"))
WORKSPACE = Path(os.environ.get("VHIL_WORKSPACE", "/workspace"))
DEST = Path(os.environ.get("VHIL_BACKUP_DIR", "/backups"))
KEEP = int(os.environ.get("VHIL_BACKUP_KEEP", "14"))
INTERVAL_H = float(os.environ.get("VHIL_BACKUP_INTERVAL_H", "24"))
STAMP = "%Y%m%dT%H%M%SZ"


def log(msg: str) -> None:
    print(f"backup: {msg}", flush=True)


def snapshots() -> list[Path]:
    """Complete snapshots, newest first (a .partial directory never counts)."""
    if not DEST.is_dir():
        return []
    out = []
    for p in DEST.iterdir():
        try:
            datetime.strptime(p.name, STAMP)
        except ValueError:
            continue
        if (p / "vhil.db").is_file():
            out.append(p)
    return sorted(out, key=lambda p: p.name, reverse=True)


def copy_db(src: Path, dst: Path) -> None:
    """src -> dst through SQLite's backup API, then an integrity check."""
    if not src.is_file():
        raise FileNotFoundError(f"no database at {src}")
    # Read-write open: a read-only one can't join a WAL database whose -shm
    # it may need to create. The backup itself only reads.
    s = sqlite3.connect(src, timeout=30)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")   # one self-contained file
    finally:
        s.close()
    try:
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        d.close()
    if ok != "ok":
        raise RuntimeError(f"integrity check of the copy failed: {ok}")


def bundle_branches(dst: Path) -> int:
    """The workspace's local branches as a git bundle; how many."""
    if not (WORKSPACE / ".git").exists():
        return 0
    refs = subprocess.run(["git", "-C", str(WORKSPACE), "for-each-ref", "--format=%(refname)",
                           "refs/heads/"], check=True, capture_output=True, text=True)
    branches = refs.stdout.split()
    if branches:
        subprocess.run(["git", "-C", str(WORKSPACE), "bundle", "create", "-q", str(dst),
                        "--branches"], check=True, capture_output=True, text=True)
    return len(branches)


def once() -> Path:
    stamp = datetime.now(timezone.utc).strftime(STAMP)
    final = DEST / stamp
    tmp = DEST / f"{stamp}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        copy_db(DB, tmp / "vhil.db")
        n = bundle_branches(tmp / "branches.bundle")
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    with sqlite3.connect(final / "vhil.db") as c:
        runs = c.execute("SELECT count(*) FROM runs").fetchone()[0]
    log(f"{final}: {runs} runs, {n} branches")
    for old in snapshots()[KEEP:]:
        shutil.rmtree(old)
        log(f"pruned {old.name}")
    return final


def restore(name: str) -> None:
    snap = DEST / name
    if not (snap / "vhil.db").is_file():
        raise SystemExit(f"no snapshot {snap} (see `backup.py list`)")
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.is_file():
        # Keep what is being replaced, next to it, until someone deletes it.
        keep = DB.with_name(f"{DB.name}.before-restore-{datetime.now(timezone.utc).strftime(STAMP)}")
        copy_db(DB, keep)
        log(f"current database saved as {keep}")
    # Through the backup API into the database itself, so a stale -wal/-shm
    # next to it is replaced consistently rather than replayed over the copy.
    s = sqlite3.connect(f"file:{snap / 'vhil.db'}?mode=ro", uri=True)
    d = sqlite3.connect(DB, timeout=30)
    try:
        s.backup(d)
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        s.close()
        d.close()
    if ok != "ok":
        raise SystemExit(f"restored database fails its integrity check: {ok}")
    log(f"restored {DB} from {snap.name}")
    if (snap / "branches.bundle").is_file():
        log(f"saved branches: git -C {WORKSPACE} fetch {snap / 'branches.bundle'} "
            f"'refs/heads/*:refs/heads/*'  (docs/deploy.md)")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "once"
    if cmd == "once":
        once()
    elif cmd == "loop":
        log(f"every {INTERVAL_H} h into {DEST}, keeping {KEEP}")
        while True:
            try:
                once()
            except Exception as e:  # noqa: BLE001 - log and retry next interval
                log(f"FAILED: {type(e).__name__}: {e}")
            time.sleep(INTERVAL_H * 3600)
    elif cmd == "list":
        for p in snapshots():
            print(p.name)
    elif cmd == "restore" and len(argv) == 2:
        restore(argv[1])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
