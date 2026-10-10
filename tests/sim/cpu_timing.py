"""CPU timing references (docs/cpu-timing.md, #246): one CPU-bound window
of each firmware that a logic analyzer sees on the chip as well, a pin held
by a busy-wait loop. Shared by test_ams_cpu_timing.py and
test_ecu_cpu_timing.py.

The emulator charges each instruction 1 / mips us, so a window's width in
virtual time is its executed instructions over the board's rate; GPIO edges
carry the instruction count exactly (models/renode/VhilProbe.cs), where their
time is only as fine as the sync quantum.

`instructions` pins the window's work in the firmware at the catalogue ref:
if it moves, the busy-wait changed, and its rate bound (the firmware's
catalogue entry, `cpu`) and the chip's measurement must be redone. `chip_us`
is the window measured on the chip (#246); None until it is.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from vhil.sim import Edge

# The chip's width within this of instructions / mips once it is measured:
# Renode has one rate for all code, and the window is one loop.
CHIP_TOLERANCE = 0.10
# The window's executed instructions within this of the pinned count.
INSTRUCTION_TOLERANCE = 0.03


@dataclass(frozen=True)
class Window:
    port: str
    pin: int
    level: bool                  # the pin's level during the window
    instructions: int            # its executed instructions, the minimum over a run
    chip_us: Optional[float]     # measured on the chip (#246), or None


def widths(edges: list[Edge], level: bool) -> list[int]:
    """Executed instructions of every pulse at `level`, edge to edge."""
    return [b.instructions - a.instructions for a, b in zip(edges, edges[1:])
            if a.level == level and b.level != level]


def mips(sim, board: str) -> int:
    return int(sim.monitor("cpu PerformanceInMips", board=board).strip().split()[-1], 0)
