"""Median real-time factor per package/system/path/phase from vhil-ab.sh's
speed.py JSON files (<results>/<label>/<package>-<repeat>.json).
    python3 summarize_vhil.py <results>/<label> [baseline-package]"""
import glob, json, os, re, statistics, sys
from collections import defaultdict
folder = sys.argv[1]; base = sys.argv[2] if len(sys.argv) > 2 else "master"
d = defaultdict(list)
for f in glob.glob(os.path.join(folder, "*.json")):
    p = re.sub(r"-\d+$", "", os.path.basename(f)[:-5])
    for r in json.load(open(f))["results"]:
        for ph in ("boot", "steady"):
            d[(r["system"], r["path"], ph, p)].append(r[ph])
pkgs = sorted({k[3] for k in d}, key=lambda p: (p != base, p))
print("| system | path | phase | " + " | ".join(pkgs) + " |")
print("|---|---|---|" + "---|" * len(pkgs))
for k in sorted({k[:3] for k in d}):
    b = statistics.median(d[k + (base,)]) if d.get(k + (base,)) else None
    cells = []
    for p in pkgs:
        xs = d.get(k + (p,))
        if not xs:
            cells.append("-"); continue
        m = statistics.median(xs)
        cells.append(f"{m:.2f}x" + (f" ({100 * (m / b - 1):+.0f} %)" if b and p != base else "") + f" n={len(xs)}")
    print(f"| {k[0]} | {k[1]} | {k[2]} | " + " | ".join(cells) + " |")
