"""Analyse `perf script -F comm,ip,sym` output.
  stacks.py FILE incl PAT...            inclusive share (of all samples) of frames matching each regex
  stacks.py FILE drill PAT [N]          for samples containing PAT (outermost match), the callee frame below it
  stacks.py FILE self [N] [THREADPAT]   leaf frames
"""
import re, sys
from collections import Counter

def short(s):
    s = re.sub(r"\[(OptimizedTier1OSR|OptimizedTier1|Optimized|QuickJitted|InstrumentedTier|Tier0)\]", "", s)
    s = re.sub(r"(class |valuetype )?\[[\w.\-]+\] ?", "", s)
    s = re.sub(r"^instance ", "", s)
    s = re.sub(r"\(.*\)$", "()", s)
    s = re.sub(r"^\S+ (?=\S*::)", "", s)
    return s[:150]

def samples(path):
    comm, frames = None, []
    for line in open(path, errors="replace"):
        if not line.strip():
            if comm is not None:
                yield comm, frames
            comm, frames = None, []
        elif not line.startswith("\t"):
            comm, frames = line.strip(), []
        else:
            parts = line.strip().split(" ", 1)
            frames.append(short(parts[1]) if len(parts) > 1 else "?")
    if comm is not None:
        yield comm, frames

path, mode, args = sys.argv[1], sys.argv[2], sys.argv[3:]
data = list(samples(path))
total = len(data)
threads = Counter(re.sub(r"\d+", "N", c) if c.startswith("Slave") else c for c, _ in data)
if mode == "incl":
    for pat in args:
        r = re.compile(pat)
        n = sum(1 for _, f in data if any(r.search(x) for x in f))
        by = Counter(c for c, f in data if any(r.search(x) for x in f))
        print(f"{100*n/total:6.2f}%  {pat}   " + ", ".join(f"{k}:{100*v/total:.1f}" for k, v in by.most_common(5)))
elif mode == "drill":
    r = re.compile(args[0]); top = int(args[1]) if len(args) > 1 else 30
    c = Counter(); n = 0
    for _, f in data:
        idx = [i for i, x in enumerate(f) if r.search(x)]
        if not idx: continue
        n += 1
        i = max(idx)              # outermost (frames are leaf-first)
        c[f[i-1] if i > 0 else "<self>"] += 1
    print(f"{args[0]}: {100*n/total:.2f}% of all samples")
    for k, v in c.most_common(top):
        print(f"  {100*v/total:6.2f}%  {k}")
elif mode == "self":
    top = int(args[0]) if args else 40
    tp = re.compile(args[1]) if len(args) > 1 else None
    c = Counter(f[0] if f else "?" for comm, f in data if not tp or tp.search(comm))
    for k, v in c.most_common(top):
        print(f"  {100*v/total:6.2f}%  {k}")
print(f"[{total} samples] " + ", ".join(f"{k}:{100*v/total:.1f}%" for k, v in threads.most_common()), file=sys.stderr)
