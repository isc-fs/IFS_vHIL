"""Median per package and benchmark from tooling/bench.sh's JSON lines.
    python3 summarize_bench.py results.jsonl [baseline-package]
Speed is virtual time per host second relative to the baseline (higher is
better); allocation is bytes per virtual second; CPU (when bench.sh recorded
it) is the process's CPU seconds per virtual second, all threads."""
import json, statistics, sys
from collections import defaultdict
VIRTUAL = {"bus-trivial": 0.25, "register-read": 0.25, "register-write": 0.25, "stm32-timer": 0.25,
           "hook-none": 0.25, "hook-always": 0.25, "hook-exclude": 0.25,
           "demo-stm32f4_discovery": 10.0, "demo-nucleo_h753zi_hubris": 60.0, "demo-nrf52840": 60.0,
           "multi-spin-shared": 0.5, "multi-spin-slaves": 0.5, "multi-spin-inline": 0.5,
           "multi-ble-shared": 5.0, "multi-ble-slaves": 5.0, "multi-ble-inline": 5.0}
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
# Runs that ended with another container running may have shared the CPUs
dropped = [r for r in rows if r.get("contended")]
rows = [r for r in rows if not r.get("contended")]
if dropped:
    print(f"(dropped {len(dropped)} contended runs)", file=sys.stderr)
base = sys.argv[2] if len(sys.argv) > 2 else rows[0]["package"]
d = defaultdict(list)
for r in rows:
    d[(r["bench"], r["package"])].append(r)
benches = list(dict.fromkeys(r["bench"] for r in rows))
pkgs = list(dict.fromkeys(r["package"] for r in rows))
print("| benchmark | " + " | ".join(f"{p} wall s" for p in pkgs) + " | " + " | ".join(f"{p} alloc MB/vs" for p in pkgs) + " |")
print("|---|" + "---|" * (2 * len(pkgs)))
for b in benches:
    v = VIRTUAL.get(b, 10.0)
    walls = {p: statistics.median(x["wall"] for x in d[(b, p)]) for p in pkgs if d[(b, p)]}
    allocs = {p: statistics.median(x["alloc"] for x in d[(b, p)]) / v / 1e6 for p in pkgs if d[(b, p)]}
    cells = []
    for p in pkgs:
        if p not in walls: cells.append("-"); continue
        rel = "" if p == base or base not in walls else f" ({100 * (walls[base] / walls[p] - 1):+.0f} % speed)"
        cells.append(f"{walls[p]:.2f}{rel} n={len(d[(b, p)])}")
    acells = []
    for p in pkgs:
        if p not in allocs: acells.append("-"); continue
        rel = "" if p == base or base not in allocs or allocs[base] == 0 else f" ({100 * (allocs[p] / allocs[base] - 1):+.0f} %)"
        acells.append(f"{allocs[p]:.0f}{rel}")
    print(f"| {b} | " + " | ".join(cells) + " | " + " | ".join(acells) + " |")
if any(r.get("cpu") is not None for r in rows):
    print()
    print("| benchmark | " + " | ".join(f"{p} CPU s/vs" for p in pkgs) + " |")
    print("|---|" + "---|" * len(pkgs))
    for b in benches:
        v = VIRTUAL.get(b, 10.0)
        cells = []
        for p in pkgs:
            xs = [x for x in d[(b, p)] if x.get("cpu") is not None]
            if not xs: cells.append("-"); continue
            cells.append(f"{statistics.median(x['cpu'] for x in xs) / v:.2f}")
        print(f"| {b} | " + " | ".join(cells) + " |")
