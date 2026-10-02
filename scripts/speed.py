"""How fast this host emulates a system: virtual seconds per wall second.

    python scripts/speed.py systems/ecu.yaml ecu=ECU08.elf [--mips 100 528] [--seconds 5]

Above 1.0x the wall-clock bench (IFS_HIL's suites) can keep real time; below
it, wall-clock tests see stretched periods. Native tests (vhil.sim) are
unaffected either way, only slower.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vhil.sim import Sim  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("system")
    ap.add_argument("firmware", nargs="+", metavar="BOARD=ELF")
    ap.add_argument("--mips", type=int, nargs="*", default=[],
                    help="core speeds to try (default: the platform's)")
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()
    firmware = dict(f.split("=", 1) for f in args.firmware)

    for mips in args.mips or [None]:
        with Sim(args.system, firmware) as sim:
            if mips is not None:
                for board in sim.system.boards:
                    sim.monitor(f"cpu PerformanceInMips {mips}", board=board)
            sim.run_for(ms=500)   # past boot
            t0 = time.monotonic()
            sim.run_for(ms=args.seconds * 1000)
            wall = time.monotonic() - t0
            label = f"{mips} MIPS" if mips is not None else "platform MIPS"
            print(f"{label}: {args.seconds:g} s virtual in {wall:.2f} s wall "
                  f"= {args.seconds / wall:.2f}x real time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
