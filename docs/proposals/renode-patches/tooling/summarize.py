"""Median real-time factor per variant/system/path/phase from results/<label>/*.json."""
import json, sys, statistics, glob, os, re
from collections import defaultdict
label = sys.argv[1]; base = sys.argv[2] if len(sys.argv) > 2 else "stock"
d = defaultdict(list)
for f in glob.glob(f"{os.path.dirname(__file__)}/../results/{label}/*.json"):
    v = re.sub(r"-\d+$", "", os.path.basename(f)[:-5])
    for r in json.load(open(f))["results"]:
        for ph in ("boot", "steady"):
            d[(r["system"], r["path"], ph, v)].append(r[ph])
variants = sorted({k[3] for k in d}, key=lambda v: (v != base, v))
keys = sorted({k[:3] for k in d})
print("| system | path | phase | " + " | ".join(variants) + " |")
print("|---|---|---|" + "---|" * len(variants))
for k in keys:
    b = statistics.median(d[k + (base,)]) if d.get(k + (base,)) else None
    cells = []
    for v in variants:
        xs = d.get(k + (v,))
        if not xs: cells.append("-"); continue
        m = statistics.median(xs)
        rel = f" ({100*(m/b-1):+.0f}%)" if b and v != base else ""
        cells.append(f"{m:.2f}x{rel} n={len(xs)}")
    print(f"| {k[0]} | {k[1]} | {k[2]} | " + " | ".join(cells) + " |")
