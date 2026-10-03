"""Fail when firmware touches hardware the virtual bench does not model.

Renode answers an access to an unmodelled register or a tagged region with a
log warning, not an error: a write is dropped and a read returns 0. A firmware
path that depends on one can pass a test for the wrong reason. This scans a
Renode log for those warnings and reports any that configs/peripherals.yaml
does not explain.

    python -m vhil.peripheral_guard vhil-renode.log [--allow configs/peripherals.yaml]
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ALLOW = REPO / "configs" / "peripherals.yaml"

# With more than one machine, Renode prefixes the source with the machine:
# "] ams/sdmmc:" rather than "] sdmmc:". Without the optional prefix the guard
# saw nothing at all on a multi-board system.
_SOURCE = r"\] (?:([\w.-]+)/)?"
_UNHANDLED = re.compile(
    _SOURCE + r"([\w.]+): Unhandled (read|write) (?:from|to) offset (0x[0-9A-Fa-f]+)")
_NONEXISTENT = re.compile(
    _SOURCE + r"sysbus: .*?(?:\(tag: '([^']+)'\) )?(?:Read|Write)\w* (?:from|to) "
    r"non existing peripheral at (0x[0-9A-Fa-f]+)")


def scan(log_text: str) -> Counter:
    """Count distinct unmodelled accesses, keyed by (kind, name, number,
    machine); machine is None on a one-machine system."""
    found: Counter = Counter()
    for line in log_text.splitlines():
        m = _UNHANDLED.search(line)
        if m:
            found[("peripheral", m.group(2), int(m.group(4), 16), m.group(1))] += 1
            continue
        m = _NONEXISTENT.search(line)
        if m:
            machine, tag, addr = m.group(1), m.group(2), int(m.group(3), 16)
            found[("tag", tag, None, machine) if tag else ("address", None, addr, machine)] += 1
    return found


def _allowed(key, rules) -> bool:
    kind, name, num, machine = key
    for r in rules:
        if "machine" in r and r["machine"] != machine:
            continue
        if kind == "peripheral" and "peripheral" in r:
            if fnmatch.fnmatch(name, r["peripheral"]) and \
                    ("offsets" not in r or num in r["offsets"]):
                return True
        elif kind == "tag" and r.get("tag") == name:
            return True
        elif kind == "address" and r.get("address") == num:
            return True
    return False


def unexplained(log_text: str, rules: list) -> list[tuple[tuple, int]]:
    return sorted(((k, n) for k, n in scan(log_text).items() if not _allowed(k, rules)),
                  key=lambda kn: (str(kn[0][3]), kn[0][0], str(kn[0][1]), kn[0][2] or 0))


def describe(key) -> str:
    kind, name, num, machine = key
    where = f"{machine}: " if machine else ""
    if kind == "peripheral":
        return f"{where}{name} register offset 0x{num:X}"
    if kind == "tag":
        return f"{where}tagged region '{name}'"
    return f"{where}unmapped address 0x{num:08X}"


def load_rules(path: Path = DEFAULT_ALLOW) -> list:
    return yaml.safe_load(Path(path).read_text()) or []


def report(findings) -> str:
    lines = ["Firmware touched hardware the virtual bench does not model:"]
    lines += [f"  {describe(k)}  ({n}x)" for k, n in findings]
    lines.append("Model it, or explain it in configs/peripherals.yaml, before trusting "
                 "a pass that may depend on it.")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("log", type=Path)
    p.add_argument("--allow", type=Path, default=DEFAULT_ALLOW)
    args = p.parse_args(argv)
    findings = unexplained(args.log.read_text(errors="replace"), load_rules(args.allow))
    if findings:
        print(report(findings))
        return 1
    print("peripheral guard: every unmodelled access is explained")
    return 0


if __name__ == "__main__":
    sys.exit(main())
