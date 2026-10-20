"""Delete GitHub Actions cache entries no run will read again (docs/ci.md).

    python scripts/ci-cache-clean.py [--pr N] [--dry-run] [--summary FILE]

The repository's cache is 10 GB; past it GitHub evicts the least recently
used entries, which were the Renode build's own (every job then rebuilt
Renode, ~7 min each). What is deleted:

1. Every entry of a pull request that is closed or merged (refs/pull/N/merge:
   only that pull request's runs can read it). With --pr N, just that one's
   (the `pull_request: closed` event).
2. On dev, entries superseded by a newer one of the same kind: every entry
   but the newest of each key prefix (BuildKit's index `index-<scope>-1-<hash>`
   without its `#n`, a firmware store `vhil-fw-v1-<os>-<workflow>-`, any
   other key as itself), and the BuildKit layer blobs (`buildkit-blob-*`,
   one per layer digest, no prefix to compare) not read for BLOB_IDLE_DAYS:
   every heavy run reads the current build's layers, and the nightly runs
   daily, so a blob idle that long belongs to a build no index names.

Other branches' entries are left alone. Needs `gh` (GH_TOKEN with actions:
write; pull-requests: read for the states).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys

REPO = "isc-fs/IFS_vHIL"
DEV = "refs/heads/dev"
BLOB_IDLE_DAYS = 3
PR_REF = re.compile(r"refs/pull/(\d+)/merge")


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def caches() -> list[dict]:
    out = gh("api", "--paginate", f"repos/{REPO}/actions/caches?per_page=100",
             "--jq", ".actions_caches[]")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def pr_state(n: int) -> str:
    return gh("api", f"repos/{REPO}/pulls/{n}", "--jq", ".state").strip()


def prefix(key: str) -> str:
    """The kind of entry a key is: entries of one kind supersede each other."""
    if "#" in key:
        return key.rsplit("#", 1)[0]
    if key.startswith("vhil-fw-"):
        return key.rsplit("-", 1)[0] + "-"
    return key


def _time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def plan(entries: list[dict], states: dict[int, str], now: dt.datetime,
         only_pr: int | None = None) -> list[tuple[dict, str]]:
    """(entry, reason) for every entry to delete."""
    out = []
    for e in entries:
        m = PR_REF.fullmatch(e["ref"])
        if m and (only_pr is None or int(m.group(1)) == only_pr) and states.get(int(m.group(1))) == "closed":
            out.append((e, f"PR #{m.group(1)} closed"))
    if only_pr is not None:
        return out
    dev = [e for e in entries if e["ref"] == DEV]
    newest: dict[str, dict] = {}
    for e in dev:
        if e["key"].startswith("buildkit-blob-"):
            continue
        p = prefix(e["key"])
        if p not in newest or _time(e["created_at"]) > _time(newest[p]["created_at"]):
            newest[p] = e
    for e in dev:
        if e["key"].startswith("buildkit-blob-"):
            idle = now - _time(e["last_accessed_at"])
            if idle > dt.timedelta(days=BLOB_IDLE_DAYS):
                out.append((e, f"dev layer blob unread for {idle.days} days"))
        elif newest[prefix(e["key"])] is not e:
            out.append((e, "dev, superseded by a newer entry"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pr", type=int, help="only this pull request's entries (it closed)")
    ap.add_argument("--dry-run", action="store_true", help="list, delete nothing")
    ap.add_argument("--summary", help="append a Markdown report (GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)
    entries = caches()
    prs = sorted({int(m.group(1)) for e in entries if (m := PR_REF.fullmatch(e["ref"]))})
    states = {n: pr_state(n) for n in prs if args.pr is None or n == args.pr}
    doomed = plan(entries, states, dt.datetime.now(dt.timezone.utc), args.pr)
    total = sum(e["size_in_bytes"] for e in entries)
    freed = sum(e["size_in_bytes"] for e, _ in doomed)
    lines = [f"{e['size_in_bytes'] / 1e6:9.1f} MB  {e['ref']:24} {e['key']}  ({why})"
             for e, why in sorted(doomed, key=lambda d: (d[0]["ref"], d[0]["key"]))]
    head = (f"cache: {len(entries)} entries, {total / 1e9:.2f} GB; "
            f"{'would delete' if args.dry_run else 'deleting'} {len(doomed)}, {freed / 1e9:.2f} GB")
    print(head)
    print("\n".join(lines))
    failed = 0
    if not args.dry_run:
        for e, _ in doomed:
            try:
                gh("api", "-X", "DELETE", f"repos/{REPO}/actions/caches/{e['id']}")
            except subprocess.CalledProcessError as err:
                # Gone already (evicted, another clean-up): not an error.
                if "404" not in (err.stderr or ""):
                    failed += 1
                    print(f"failed to delete {e['id']} {e['key']}: {err.stderr.strip()}",
                          file=sys.stderr)
    if args.summary:
        with open(args.summary, "a") as f:
            f.write(f"### Actions cache clean-up\n\n{head}\n\n```\n" + "\n".join(lines) + "\n```\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
