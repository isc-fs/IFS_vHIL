"""The last blocks a CPU ran, read back from VhilTrace (models/renode/VhilTrace.cs)
and made readable: symbolised against the board's ELF images and collapsed
into a call-ish trace.

    0x0802159E 7     ->  NRF24_CsnDelay (203 blocks)
    0x0802159E 7         [NRF24_BitBangTransfer -> HAL_GPIO_WritePin ->
    0x080215F2 3          NRF24_BitBangTransfer -> NRF24_BitBangDelay] x 2 (503 blocks)
    ...

A firmware that spins shows up as one repeated group instead of thousands of
lines. Host-only and pure: vhil/snapshot.py feeds it the ring text.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from vhil import elf


@dataclass(frozen=True)
class Block:
    pc: int
    count: int     # instructions in the translation block


def parse_ring(text: str) -> list[Block]:
    """VhilTrace's `Ring` output: "0xPC count" per line, oldest first."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("0x") and parts[1].isdigit():
            out.append(Block(int(parts[0], 16), int(parts[1])))
    return out


def symbolizer(images: Iterable[Path | str]) -> Callable[[int], str]:
    """addr -> "name+0x12" (or "0x0800ABCD" when no image has a symbol there),
    over every image of a board (bootloader and app)."""
    images = [Path(p) for p in images]
    cache: dict[int, str] = {}

    def name(address: int) -> str:
        if address not in cache:
            label = None
            for image in images:
                try:
                    hit = elf.symbol_at(image, address)
                except (OSError, ValueError):
                    hit = None
                if hit:
                    label = hit[0] if hit[1] == 0 else f"{hit[0]}+0x{hit[1]:X}"
                    break
            cache[address] = label or f"0x{address:08X}"
        return cache[address]
    return name


def function_of(label: str) -> str:
    return label.split("+", 1)[0]


@dataclass(frozen=True)
class Step:
    """One line of a collapsed trace: `pattern` (function names, in order)
    ran `repeats` times in a row, covering `blocks` blocks."""
    pattern: tuple[str, ...]
    repeats: int
    blocks: int

    def __str__(self) -> str:
        if len(self.pattern) == 1 and self.repeats == 1:
            return f"{self.pattern[0]} ({self.blocks} block{'s' if self.blocks != 1 else ''})"
        body = " -> ".join(self.pattern)
        if len(self.pattern) > 1:
            body = f"[{body}]"
        return f"{body} x {self.repeats} ({self.blocks} blocks)"


def collapse(functions: Sequence[str], max_period: int = 6) -> list[Step]:
    """Run-length the function sequence (consecutive blocks in one function
    become one entry), then fold repeated groups of up to `max_period`
    entries: A B A B A B C -> [A -> B] x 3, C."""
    runs: list[tuple[str, int]] = []
    for f in functions:
        if runs and runs[-1][0] == f:
            runs[-1] = (f, runs[-1][1] + 1)
        else:
            runs.append((f, 1))
    names = [r[0] for r in runs]
    steps: list[Step] = []
    i = 0
    while i < len(runs):
        best_p, best_n = 1, 1
        for p in range(1, max_period + 1):
            if i + 2 * p > len(runs):
                break
            n = 1
            while names[i + n * p:i + (n + 1) * p] == names[i:i + p]:
                n += 1
            # Prefer the group that swallows the most entries; on a tie the
            # shorter period (A A A is "A x 3", not "[A -> A]").
            if n > 1 and n * p > best_n * best_p:
                best_p, best_n = p, n
        span = runs[i:i + best_p * best_n]
        steps.append(Step(tuple(names[i:i + best_p]), best_n, sum(c for _, c in span)))
        i += best_p * best_n
    return steps


def format_ring(blocks: Sequence[Block], name: Callable[[int], str]) -> list[str]:
    """Full listing, one block per line: pc, instruction count, symbol."""
    return [f"0x{b.pc:08X} {b.count:4d}  {name(b.pc)}" for b in blocks]


def collapsed(blocks: Sequence[Block], name: Callable[[int], str],
              tail: Optional[int] = None) -> list[str]:
    """The collapsed trace as text lines, oldest first; `tail` keeps the
    last lines only (the ones nearest the failure)."""
    lines = [str(s) for s in collapse([function_of(name(b.pc)) for b in blocks])]
    if tail is not None and len(lines) > tail:
        lines = [f"... {len(lines) - tail} earlier steps"] + lines[-tail:]
    return lines
