"""Per-package table from tooling/bench.sh's JSON lines, one row per package:
for each benchmark the median host seconds (and its change in speed against
the baseline), the fastest run, and the allocation per virtual second.
    python3 summarize_perpr.py results.jsonl [baseline-package]
Speed change = baseline median / package median - 1 (positive is faster)."""
import json, statistics, sys
from collections import defaultdict
VIRTUAL = 0.25  # every loop and hook benchmark measures 0.25 s of virtual time
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
rows = [r for r in rows if not r.get("contended")]
base = sys.argv[2] if len(sys.argv) > 2 else rows[0]["package"]
d = defaultdict(list)
for r in rows:
    d[(r["bench"], r["package"])].append(r)
benches = list(dict.fromkeys(r["bench"] for r in rows))
pkgs = list(dict.fromkeys(r["package"] for r in rows))
print("| package | " + " | ".join(f"{b}: median (speed), min, alloc GB/vs" for b in benches) + " |")
print("|---|" + "---|" * len(benches))
for p in pkgs:
    cells = []
    for b in benches:
        runs = d[(b, p)]
        if not runs:
            cells.append("-"); continue
        med = statistics.median(x["wall"] for x in runs)
        mn = min(x["wall"] for x in runs)
        alloc = statistics.median(x["alloc"] for x in runs) / VIRTUAL / 1e9
        bm = statistics.median(x["wall"] for x in d[(b, base)])
        rel = "" if p == base else f" ({100 * (bm / med - 1):+.0f} %)"
        cells.append(f"{med:.2f} s{rel}, {mn:.2f}, {alloc:.1f}")
    print(f"| {p} | " + " | ".join(cells) + " |")
print(f"\nn = {', '.join(sorted(set(str(len(v)) for v in d.values())))} per cell")
