"""Firmware coverage of a native test run: which functions and source lines of
each ELF image executed.

    pytest tests/sim --vhil-coverage cov/        (tests/sim/conftest.py)
    python -m vhil.coverage cov/ [more/ ...] -o cov/   (re-report, merge shards)

Each Sim started with a coverage directory (vhil/sim.py) has Renode log every
translation block it translates, with the disassembly of each instruction
(`cpu LogTranslatedBlocks`), to `<dir>/raw/<stem>-<board>.tblog`, next to a
JSON sidecar naming the board's images. A block is translated when the CPU
first reaches it, so the instruction addresses in the log are the code that
ran. Two limits follow: there are no hit counts (lcov gets 1 for every hit
line), and the tail of a block cut short by an exception counts as run.

Addresses map to functions through the ELF symbol table (vhil/elf.py) and to
source lines through the DWARF line table, read with the Arm toolchain's
objdump and addr2line (no Python ELF/DWARF dependency). An image without line
info (the CAN bootloader's Release build) gets function coverage only.

Outputs, per image: `<image>.info` (lcov tracefile, files with line info),
`<image>.functions.tsv` (every function: hit, name, address, size, file,
line), and `summary.txt` / `summary.md` for the run: functions and lines hit
per source file.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from vhil import elf

OBJDUMP = os.environ.get("VHIL_OBJDUMP", "arm-none-eabi-objdump")
ADDR2LINE = os.environ.get("VHIL_ADDR2LINE", "arm-none-eabi-addr2line")

_INSN = re.compile(r"^0x([0-9a-fA-F]+):\s")
_ROW = re.compile(r"^(\S+)\s+(\d+|-)\s+(0x[0-9a-fA-F]+)")


# -- what ran ------------------------------------------------------------------

def parse_tblog(text: str) -> set[int]:
    """Instruction addresses in a `cpu LogTranslatedBlocks` log:
    "0x0803a220:   f8dfd038   ldr.w sp, [pc, #56]" lines."""
    out = set()
    for line in text.splitlines():
        m = _INSN.match(line)
        if m:
            out.add(int(m.group(1), 16))
    return out


def collect(raw_dirs: Iterable[Path]) -> dict[Path, set[int]]:
    """{image: executed instruction addresses} over every log in raw_dirs,
    each address credited to the image of its board whose code holds it."""
    executed: dict[Path, set[int]] = defaultdict(set)
    for raw in raw_dirs:
        for meta_file in sorted(Path(raw).glob("*.json")):
            meta = json.loads(meta_file.read_text())
            log = meta_file.parent / meta["log"]
            if not log.is_file():
                continue
            images = [Path(p) for p in meta["images"] if Path(p).is_file()]
            ranges = [(img, elf.exec_ranges(img)) for img in images]
            for address in parse_tblog(log.read_text(errors="replace")):
                for img, spans in ranges:
                    if any(lo <= address < hi for lo, hi in spans):
                        executed[img].add(address)
                        break
            for img in images:
                executed.setdefault(img, set())
    return dict(executed)


# -- source lines --------------------------------------------------------------

def parse_decodedline(text: str) -> list[list[tuple[int, Optional[int]]]]:
    """`objdump --dwarf=decodedline` -> sequences of (address, line); the last
    entry of a sequence has line None (its end address)."""
    sequences, current = [], []
    for raw in text.splitlines():
        m = _ROW.match(raw.strip())
        if not m or m.group(1) == "File":
            continue
        address = int(m.group(3), 16)
        if m.group(2) == "-":
            current.append((address, None))
            sequences.append(current)
            current = []
        else:
            current.append((address, int(m.group(2))))
    if current:
        sequences.append(current)
    return sequences


def parse_addr2line(text: str) -> list[Optional[tuple[str, int]]]:
    """addr2line output, one "path:line" per address -> (path, line) or None."""
    out = []
    for raw in text.splitlines():
        line = re.sub(r"\s*\(discriminator \d+\)$", "", raw.strip())
        path, _, num = line.rpartition(":")
        out.append((path, int(num)) if path and path != "??" and num.isdigit() and int(num) else None)
    return out


@dataclass
class LineTable:
    """Address ranges of an image's source lines: [start, end) -> (path, line)."""
    starts: list[int] = field(default_factory=list)
    rows: list[tuple[int, str, int]] = field(default_factory=list)   # (end, path, line)

    @classmethod
    def build(cls, sequences, resolve: Callable[[list[int]], list[Optional[tuple[str, int]]]]):
        """resolve: addresses -> (path, line) each (addr2line), for the file
        of each row: decodedline names files inconsistently (a CU's own file
        by its bare name, headers by full path)."""
        spans = []
        for seq in sequences:
            for (a, line), (b, _) in zip(seq, seq[1:]):
                if line and b > a:
                    spans.append((a, b, line))
        spans.sort()
        where = resolve([a for a, _, _ in spans]) if spans else []
        table = cls()
        for (a, b, line), loc in zip(spans, where):
            if loc is None:
                continue
            table.starts.append(a)
            table.rows.append((b, loc[0], line))
        return table

    def line_of(self, address: int) -> Optional[tuple[str, int]]:
        i = bisect.bisect_right(self.starts, address) - 1
        if i >= 0 and address < self.rows[i][0]:
            return self.rows[i][1], self.rows[i][2]
        return None

    def lines(self) -> dict[str, set[int]]:
        out: dict[str, set[int]] = defaultdict(set)
        for _, path, line in self.rows:
            out[path].add(line)
        return out

    def __bool__(self) -> bool:
        return bool(self.rows)


def line_table(image: Path) -> LineTable:
    """The image's DWARF line table; empty without line info or toolchain."""
    if not shutil.which(OBJDUMP) or not shutil.which(ADDR2LINE):
        return LineTable()
    decoded = subprocess.run([OBJDUMP, "--dwarf=decodedline", str(image)], check=True,
                             capture_output=True, text=True).stdout

    def resolve(addresses: list[int]):
        res = subprocess.run([ADDR2LINE, "-e", str(image)], check=True, capture_output=True,
                             text=True, input="\n".join(f"0x{a:x}" for a in addresses) + "\n")
        return parse_addr2line(res.stdout)
    return LineTable.build(parse_decodedline(decoded), resolve)


# -- per image -----------------------------------------------------------------

@dataclass
class Function:
    name: str
    start: int
    size: int
    hit: bool
    path: Optional[str] = None
    line: Optional[int] = None


@dataclass
class ImageCoverage:
    image: Path
    functions: list[Function]
    lines_total: dict[str, set[int]]
    lines_hit: dict[str, set[int]]


def image_coverage(image: Path, executed: set[int], functions: list[elf.Symbol],
                   table: LineTable) -> ImageCoverage:
    hits = sorted(executed)
    funcs = []
    for f in functions:
        if f.size == 0:
            continue
        i = bisect.bisect_left(hits, f.start)
        hit = i < len(hits) and hits[i] < f.end
        loc = table.line_of(f.start) if table else None
        funcs.append(Function(f.name, f.start, f.size, hit, *(loc or (None, None))))
    total = table.lines() if table else {}
    lines_hit: dict[str, set[int]] = defaultdict(set)
    if table:
        for a in hits:
            loc = table.line_of(a)
            if loc:
                lines_hit[loc[0]].add(loc[1])
    return ImageCoverage(image, funcs, dict(total), dict(lines_hit))


def lcov(cov: ImageCoverage, test_name: str = "") -> str:
    """lcov tracefile of the files with line info. Hit counts are 0/1."""
    out = []
    by_file: dict[str, list[Function]] = defaultdict(list)
    for f in cov.functions:
        if f.path:
            by_file[f.path].append(f)
    for path in sorted(set(cov.lines_total) | set(by_file)):
        out += [f"TN:{test_name}", f"SF:{path}"]
        fs = sorted(by_file.get(path, []), key=lambda f: (f.line or 0, f.name))
        out += [f"FN:{f.line},{f.name}" for f in fs]
        out += [f"FNDA:{int(f.hit)},{f.name}" for f in fs]
        out += [f"FNF:{len(fs)}", f"FNH:{sum(f.hit for f in fs)}"]
        total, hit = cov.lines_total.get(path, set()), cov.lines_hit.get(path, set())
        out += [f"DA:{n},{int(n in hit)}" for n in sorted(total)]
        out += [f"LF:{len(total)}", f"LH:{len(total & hit)}", "end_of_record"]
    return "\n".join(out) + "\n"


def functions_tsv(cov: ImageCoverage) -> str:
    rows = ["hit\tfunction\taddress\tsize\tfile\tline"]
    for f in sorted(cov.functions, key=lambda f: f.start):
        rows.append(f"{int(f.hit)}\t{f.name}\t0x{f.start:08X}\t{f.size}\t{f.path or ''}\t{f.line or ''}")
    return "\n".join(rows) + "\n"


def _common_dir(paths: Iterable[str]) -> str:
    """The firmware's source root: the common directory of the files in the
    largest top-level tree (/vhil/fw/ams@main/...), so toolchain headers
    (/opt/arm-gnu/...) don't pull it up to "/"."""
    paths = [p for p in paths if p.startswith("/")]
    if not paths:
        return ""
    trees: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        trees["/".join(p.split("/")[:3])].append(p)
    biggest = max(trees.values(), key=len)
    return os.path.commonpath([os.path.dirname(p) for p in biggest])


def _relative(path: str, root: str) -> str:
    if root and path.startswith(root.rstrip("/") + "/"):
        return os.path.relpath(path, root)
    return path


@dataclass
class Row:
    module: str
    funcs_hit: int
    funcs: int
    lines_hit: int
    lines: int


def summary_rows(cov: ImageCoverage) -> list[Row]:
    """Per source file (relative to the image's source root); functions with
    no line info grouped as "(no line info)"."""
    root = _common_dir(set(cov.lines_total) | {f.path for f in cov.functions if f.path})
    groups: dict[str, list] = defaultdict(lambda: [0, 0, 0, 0])
    for f in cov.functions:
        key = _relative(f.path, root) if f.path else "(no line info)"
        g = groups[key]
        g[0] += f.hit
        g[1] += 1
    for path, total in cov.lines_total.items():
        key = _relative(path, root)
        g = groups[key]
        g[2] += len(total & cov.lines_hit.get(path, set()))
        g[3] += len(total)
    return [Row(k, *v) for k, v in sorted(groups.items())]


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:5.1f}%" if b else "    -"


def summary_text(covs: list[ImageCoverage], markdown: bool = False, files: bool = True) -> str:
    """Per image totals and, with `files`, a row per source file."""
    out = []
    for cov in covs:
        rows = summary_rows(cov)
        fh, ft = sum(r.funcs_hit for r in rows), sum(r.funcs for r in rows)
        lh, lt = sum(r.lines_hit for r in rows), sum(r.lines for r in rows)
        title = (f"{cov.image.name}: functions {fh}/{ft} ({_pct(fh, ft).strip()}), "
                 f"lines {lh}/{lt} ({_pct(lh, lt).strip()})")
        if not files:
            out.append(title)
            continue
        if markdown:
            out += [f"### {title}", "", "| file | functions | | lines | |", "|---|---:|---:|---:|---:|"]
            out += [f"| {r.module} | {r.funcs_hit}/{r.funcs} | {_pct(r.funcs_hit, r.funcs).strip()} "
                    f"| {r.lines_hit}/{r.lines} | {_pct(r.lines_hit, r.lines).strip()} |" for r in rows]
        else:
            width = max([len(r.module) for r in rows] + [4])
            out += [title, f"  {'file':<{width}s}  {'functions':>11s}        {'lines':>11s}"]
            out += [f"  {r.module:<{width}s}  {r.funcs_hit:>5d}/{r.funcs:<5d} {_pct(r.funcs_hit, r.funcs)}"
                    f"  {r.lines_hit:>5d}/{r.lines:<5d} {_pct(r.lines_hit, r.lines)}" for r in rows]
        out.append("")
    return "\n".join(out) + ("\n" if out and not files else "")


def report(raw_dirs: Iterable[Path], out_dir: Path, test_name: str = "") -> list[ImageCoverage]:
    """Write every image's lcov, function list and the summary to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    covs = []
    for image, executed in sorted(collect(raw_dirs).items()):
        cov = image_coverage(image, executed, elf.functions(image), line_table(image))
        stem = image.stem
        (out_dir / f"{stem}.info").write_text(lcov(cov, test_name))
        (out_dir / f"{stem}.functions.tsv").write_text(functions_tsv(cov))
        covs.append(cov)
    (out_dir / "summary.txt").write_text(summary_text(covs))
    (out_dir / "summary.md").write_text(summary_text(covs, markdown=True))
    return covs


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vhil.coverage", description=__doc__.split("\n\n")[0])
    ap.add_argument("dirs", nargs="+", type=Path, help="coverage directories (their raw/ logs)")
    ap.add_argument("-o", "--out", type=Path, help="report directory (default: the first dir)")
    ap.add_argument("--name", default="", help="lcov test name")
    args = ap.parse_args(argv)
    raws = [d / "raw" if (d / "raw").is_dir() else d for d in args.dirs]
    covs = report(raws, args.out or args.dirs[0], args.name)
    print(summary_text(covs), end="")
    return 0 if covs else 1


if __name__ == "__main__":
    sys.exit(main())
