"""What each CPU of a Sim was doing when a test failed (tests/sim/conftest.py).

Per board: virtual time, PC/LR/SP with their symbols, the core registers,
the active exception and the SCB fault registers, the FreeRTOS view read
from RAM (the running task, and every task's state, priority and stack
high-water mark) and, when the Sim runs a VhilTrace (`--vhil-trace`), the
blocks that led up to the failure, symbolised and collapsed (vhil/trace.py).

Everything here only reads: registers, RAM (never a peripheral through a
stray pointer: an address is read only inside the H733's SRAMs), and the
trace ring. A step that fails is recorded in the snapshot as an error and
the rest still runs; `take` itself never raises.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from vhil import elf, trace

# RAM of the STM32H733 (RM0468 table 7, Memory map): DTCM, AXI SRAM,
# SRAM1/2, SRAM4. Pointers outside these are not followed.
RAM = ((0x20000000, 0x20020000), (0x24000000, 0x24050000),
       (0x30000000, 0x30008000), (0x38000000, 0x38004000))

# SCB fault status (Armv7-M ARM B3.2.2): CFSR, HFSR, MMFAR, BFAR.
SCB_FAULTS = (("CFSR", 0xE000ED28), ("HFSR", 0xE000ED2C),
              ("MMFAR", 0xE000ED34), ("BFAR", 0xE000ED38))

# Core registers worth printing; Renode also lists D0-D31/S0-S31.
CORE = ("R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11", "R12",
        "SP", "LR", "PC", "CPSR", "Control", "BasePri", "PRIMASK", "FAULTMASK",
        "OtherSP", "VecBase")

EXCEPTIONS = {0: "thread mode", 1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage",
              5: "BusFault", 6: "UsageFault", 11: "SVCall", 12: "DebugMonitor",
              14: "PendSV", 15: "SysTick"}

# FreeRTOS (tasks.c, list.h; no MPU wrappers, no list integrity bytes):
# TCB_t starts pxTopOfStack, xStateListItem (ListItem_t, 20 bytes),
# xEventListItem, uxPriority, pxStack, pcTaskName. xStateListItem.pvOwner
# points back at the TCB, which is how the layout is checked before use.
TCB_STATE_OWNER, TCB_PRIORITY, TCB_STACK, TCB_NAME = 16, 44, 48, 52
TASK_NAME_MAX = 24
STACK_FILL = 0xA5            # tskSTACK_FILL_BYTE
TASK_LISTS = (("pxReadyTasksLists", "ready"), ("xPendingReadyList", "ready"),
              ("xDelayedTaskList1", "blocked"), ("xDelayedTaskList2", "blocked"),
              ("xSuspendedTaskList", "suspended"), ("xTasksWaitingTermination", "deleted"))
LIST_SIZE = 20


def in_ram(address: int, size: int = 1) -> bool:
    return any(lo <= address and address + size <= hi for lo, hi in RAM)


def parse_registers(text: str) -> dict[str, int]:
    """`cpu GetRegistersValues` table -> {name: value}; "SP / R13" -> "SP"."""
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[2].startswith("0x"):
            out[cells[0].split(" / ")[0]] = int(cells[2], 16)
    return out


def parse_bytes(text: str) -> bytes:
    """`sysbus ReadBytes` output ("[ 0x0A, 0x00, ... ]") -> bytes."""
    return bytes(int(t, 16) for t in re.findall(r"0x([0-9A-Fa-f]{1,2})\b", text))


def exception_name(ipsr: int) -> str:
    if ipsr >= 16:
        return f"IRQ{ipsr - 16}"
    return EXCEPTIONS.get(ipsr, f"exception {ipsr}")


def parse_time_us(info: str) -> Optional[int]:
    m = re.search(r"Elapsed Virtual Time: (\d+):(\d+):([\d.]+)", info)
    if not m:
        return None
    h, mnt, s = m.groups()
    return round((int(h) * 3600 + int(mnt) * 60 + float(s)) * 1_000_000)


@dataclass
class Task:
    tcb: int
    name: str
    state: str
    priority: Optional[int] = None
    stack_free: Optional[int] = None      # bytes never used (high-water mark)


@dataclass
class BoardSnapshot:
    board: str
    images: list[Path]
    time_us: Optional[int] = None
    registers: dict[str, int] = field(default_factory=dict)
    faults: dict[str, int] = field(default_factory=dict)
    current_task: Optional[str] = None
    tasks: list[Task] = field(default_factory=list)
    ring: list[trace.Block] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def _name(self) -> Callable[[int], str]:
        return trace.symbolizer(self.images)

    def summary(self) -> str:
        """One or two lines for the pytest report."""
        name = self._name()
        r = self.registers
        t = f"{self.time_us / 1e6:.6f} s" if self.time_us is not None else "? s"
        parts = [f"{self.board} @ {t}:"]
        if "PC" in r:
            parts.append(f"PC 0x{r['PC']:08X} {name(r['PC'])}")
        if "LR" in r:
            parts.append(f"LR {name(r['LR'] & ~1)}")
        if "CPSR" in r:
            parts.append(f"in {exception_name(r['CPSR'] & 0x1FF)}")
        if self.current_task:
            parts.append(f"task '{self.current_task}'")
        line = " ".join(parts)
        if any(self.faults.get(k) for k in ("CFSR", "HFSR")):
            line += f"\n  fault: CFSR=0x{self.faults.get('CFSR', 0):08X} HFSR=0x{self.faults.get('HFSR', 0):08X}"
        if self.ring:
            last = trace.collapsed(self.ring, name, tail=3)
            line += "\n  last blocks: " + " | ".join(last)
        if self.errors:
            line += f"\n  ({len(self.errors)} snapshot step(s) failed, see the full file)"
        return line

    def text(self, full_ring: bool = True) -> str:
        """Everything collected; `full_ring` adds the trace block by block."""
        name = self._name()
        out = [f"== {self.board}", "images: " + ", ".join(str(p) for p in self.images)]
        out.append(f"virtual time: {self.time_us} us" if self.time_us is not None else "virtual time: ?")
        r = self.registers
        for key in ("PC", "LR", "SP"):
            if key in r:
                v = r[key] & ~1 if key == "LR" else r[key]
                out.append(f"{key:3s} 0x{r[key]:08X}  {name(v)}")
        if "CPSR" in r:
            out.append(f"xPSR 0x{r['CPSR']:08X}  {exception_name(r['CPSR'] & 0x1FF)}")
        if r:
            out.append("registers:")
            keys = [k for k in CORE if k in r]
            for i in range(0, len(keys), 4):
                out.append("  " + "  ".join(f"{k:>9s}=0x{r[k]:08X}" for k in keys[i:i + 4]))
        if self.faults:
            out.append("SCB: " + "  ".join(f"{k}=0x{v:08X}" for k, v in self.faults.items()))
        if self.tasks or self.current_task:
            out.append(f"FreeRTOS: running '{self.current_task}'")
            for t in self.tasks:
                free = f"{t.stack_free} B free" if t.stack_free is not None else "stack ?"
                prio = f"prio {t.priority}" if t.priority is not None else ""
                out.append(f"  {t.name:<{TASK_NAME_MAX}s} {t.state:<9s} {prio:<8s} {free}  (TCB 0x{t.tcb:08X})")
        if self.ring:
            out.append(f"last {len(self.ring)} blocks, collapsed (oldest first):")
            out += ["  " + s for s in trace.collapsed(self.ring, name)]
            if full_ring:
                out.append("last blocks, one per line (pc, instructions, symbol):")
                out += ["  " + s for s in trace.format_ring(self.ring, name)]
        if self.errors:
            out.append("snapshot errors:")
            out += ["  " + e for e in self.errors]
        return "\n".join(out) + "\n"


class _Reader:
    """Monitor reads of one board, bounded to RAM."""

    def __init__(self, monitor: Callable[[str], str]):
        self.monitor = monitor

    def bytes(self, address: int, size: int) -> bytes:
        if not in_ram(address, size):
            raise ValueError(f"0x{address:08X}+{size} is outside RAM")
        data = parse_bytes(self.monitor(f"sysbus ReadBytes 0x{address:X} {size}"))
        if len(data) != size:
            raise ValueError(f"ReadBytes 0x{address:X} {size}: got {len(data)} bytes")
        return data

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.bytes(address, 4))[0]


def freertos(read: _Reader, image: Path) -> tuple[Optional[str], list[Task]]:
    """(running task's name, every task) from the kernel's lists in RAM.
    Raises LookupError if the image has no FreeRTOS, ValueError if the TCB
    layout isn't the one this reader knows."""
    syms = elf.symbols(image)
    if "pxCurrentTCB" not in syms:
        raise LookupError("no FreeRTOS in this image")
    current = read.u32(syms["pxCurrentTCB"][0])
    tasks: dict[int, Task] = {}

    def task(tcb: int, state: str) -> Task:
        head = read.bytes(tcb, TCB_NAME + TASK_NAME_MAX)
        if struct.unpack_from("<I", head, TCB_STATE_OWNER)[0] != tcb:
            raise ValueError(f"TCB 0x{tcb:08X}: xStateListItem.pvOwner does not point back "
                             "(unknown TCB layout)")
        raw = head[TCB_NAME:]
        name = raw.split(b"\0", 1)[0].decode("ascii", "replace")
        prio, stack = struct.unpack_from("<II", head, TCB_PRIORITY)
        return Task(tcb, name, state, prio, _stack_free(read, stack))

    if current:
        tasks[current] = task(current, "running")
    for symbol, state in TASK_LISTS:
        if symbol not in syms:
            continue
        base, size = syms[symbol]
        for off in range(0, max(size, LIST_SIZE), LIST_SIZE):
            lst = base + off
            n = read.u32(lst)
            if n == 0:
                continue
            item = read.u32(lst + 12)                  # xListEnd.pxNext
            for _ in range(min(n, 64)):
                owner = read.u32(item + 12)            # ListItem_t.pvOwner
                if owner and owner not in tasks:
                    tasks[owner] = task(owner, state)
                item = read.u32(item + 4)              # pxNext
    running = tasks[current].name if current in tasks else None
    return running, sorted(tasks.values(), key=lambda t: (t.state != "running", t.name))


def _stack_free(read: _Reader, stack: int, limit: int = 32768) -> Optional[int]:
    """Bytes of the fill pattern left at the low end of a task's stack."""
    if not in_ram(stack, 4):
        return None
    free = 0
    while free < limit:
        chunk = read.bytes(stack + free, 256) if in_ram(stack + free, 256) else b""
        if not chunk:
            break
        for b in chunk:
            if b != STACK_FILL:
                return free
            free += 1
    return free


def take_all(sim, timeout_s: float = 30.0) -> list[BoardSnapshot]:
    """Snapshot every board of a running Sim. Never raises.

    Monitor commands get `timeout_s` instead of the Sim's long default: a
    snapshot only reads, so a command that takes longer means Renode is stuck,
    and the failure being reported must not wait minutes for it. The machine
    the monitor had selected is selected again afterwards, so a test that
    goes on with the same Sim sees the monitor as it left it."""
    boards = list(sim.system.boards)
    proc = getattr(sim, "_proc", None)
    if proc is not None and proc.poll() is not None:
        return [BoardSnapshot(b, sim.images_of(b), errors=[f"Renode exited ({proc.returncode})"])
                for b in boards]
    mon = getattr(sim, "_monitor", None)
    sock = getattr(mon, "_sock", None)
    old_timeout = sock.gettimeout() if sock is not None else None
    prev_mach = getattr(sim, "_mach", None)
    try:
        if sock is not None:
            sock.settimeout(timeout_s)
        return [take(sim, b) for b in boards]
    finally:
        try:
            if sock is not None:
                sock.settimeout(old_timeout)
            if prev_mach is not None and sim._mach != prev_mach:
                mon.execute(f'mach set "{prev_mach}"')
                sim._mach = prev_mach
        except Exception:
            pass


def take(sim, board: str, ring_blocks: Optional[int] = None) -> BoardSnapshot:
    """Snapshot one board of a running Sim. Never raises."""
    images = sim.images_of(board)
    snap = BoardSnapshot(board, images)

    def step(what, fn):
        try:
            return fn()
        except Exception as e:   # noqa: BLE001 - recorded, never raised
            snap.errors.append(f"{what}: {type(e).__name__}: {e}")
            return None

    mon = lambda cmd: sim.monitor(cmd, board=board)
    snap.time_us = step("time", lambda: parse_time_us(sim.monitor("emulation GetTimeSourceInfo")))
    snap.registers = step("registers", lambda: parse_registers(mon("cpu GetRegistersValues"))) or {}
    faults = {}
    for key, address in SCB_FAULTS:
        v = step(key, lambda a=address: int(mon(f"sysbus ReadDoubleWord 0x{a:X}").strip(), 16))
        if v is not None:
            faults[key] = v
    snap.faults = faults
    read = _Reader(mon)
    app = sim.firmware.get(board)
    if app is not None:
        result = step("FreeRTOS", lambda: freertos(read, app))
        if result:
            snap.current_task, snap.tasks = result
    if sim.trace_blocks:
        last = ring_blocks or 0
        snap.ring = step("trace", lambda: trace.parse_ring(
            sim.monitor(f"vhil_trace_{board} Ring {last}"))) or []
    return snap
