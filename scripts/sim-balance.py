"""Split tests/sim's files over CI shards by how long each takes (#260).

    python scripts/sim-balance.py <shards> <k> [--lanes 2]   # the files of shard k (1-based)
    python scripts/sim-balance.py <shards> --table           # every shard's files and estimate

Each shard runs its files `lanes` at a time (scripts/sim-shard.sh), so a
shard takes about the longer of its lanes. Files go, longest first, to the
lane that ends soonest (longest-processing-time first), lanes grouped
`lanes` per shard. Durations are wall seconds per file from a CI run
(tests/sim/durations.json: the per-file table sim-shard.sh writes to each
shard's job summary); a file it doesn't list counts as the median. The split
only moves files between shards; every file runs either way.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DURATIONS = REPO / "tests" / "sim" / "durations.json"


def split(files: list[str], durations: dict[str, float], shards: int, lanes: int) -> list[list[str]]:
    known = [durations[f] for f in files if f in durations]
    default = statistics.median(known) if known else 60.0
    cost = {f: durations.get(f, default) for f in files}
    ends = [0.0] * (shards * lanes)
    out: list[list[str]] = [[] for _ in range(shards)]
    # Longest first; ties by name, so the split is the same on every runner.
    for f in sorted(files, key=lambda f: (-cost[f], f)):
        lane = min(range(len(ends)), key=lambda i: (ends[i], i))
        ends[lane] += cost[f]
        out[lane // lanes].append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("shards", type=int)
    ap.add_argument("k", type=int, nargs="?")
    ap.add_argument("--lanes", type=int, default=2)
    ap.add_argument("--table", action="store_true")
    args = ap.parse_args()
    files = sorted(p.relative_to(REPO).as_posix() for p in (REPO / "tests" / "sim").glob("test_*.py"))
    durations = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    shards = split(files, durations, args.shards, args.lanes)
    if args.table:
        for i, fs in enumerate(shards, 1):
            est = sum(durations.get(f, 0) for f in fs) / args.lanes
            print(f"shard {i}: ~{est:.0f} s: {' '.join(Path(f).stem for f in fs)}")
        return 0
    if args.k is None or not 1 <= args.k <= args.shards:
        ap.error("k: 1..shards")
    print("\n".join(shards[args.k - 1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
