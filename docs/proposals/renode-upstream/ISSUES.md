# Issues to open in renode/renode first

Renode's CONTRIBUTING.md asks for an issue even when you send the fix
yourself ("Please create an issue even if you plan to fix it"), and its
commit subjects carry the issue number (`[#1234] Tag: Description`; GitHub
issues land as `[GH#941]`, e.g. renode-infrastructure@[GH#941] NVIC). Open
these two in https://github.com/renode/renode/issues/new/choose, then put
the numbers in the PRs (README.md, step 3).

---

## Issue A (template: Bug report)

**Title:** `ClockSource: TryRemoveClockEntry and GetClockEntry use an index that a handler may have moved`

### Description

`BaseClockSource.TryRemoveClockEntry` finds the entry's index, then calls
`UpdateLimits()`, which runs the handlers of the clock entries that are due,
and then removes the entry at the index it found before. `GetClockEntry`
does the same with the entry's own handler (and with the handlers its
`UpdateLimits()` runs when unaccounted time triggers a full update): it
writes the entry back at the index found before the handler ran. A handler
that adds or removes a clock entry (e.g. one that cancels another timer, or
a `Machine.ScheduleAction` callback) moves the entry in the list, so:

- `TryRemoveClockEntry` removes another entry, or throws
  `ArgumentOutOfRangeException` when the index is now past the end;
- `GetClockEntry` overwrites another entry with this one (that entry is lost
  and this one is in the list twice).

### Expected behaviour

The entry that was asked for is removed / updated, and no other.

### How to reproduce?

Two NUnit tests (in the PR, `TimeTests`) fail on master: an entry with a zero
period is reached on every update, so its handler runs inside
`TryRemoveClockEntry`'s `UpdateLimits()` and inside `GetClockEntry`; the
handler removes an entry placed before the one being removed / read.

```
ShouldRemoveTheGivenEntryWhenAHandlerRemovesAnEntryBeforeIt
  entries [A, Z, X, W]; Z's handler removes A; TryRemoveClockEntry(X)
  expected [Z, W], master leaves [Z, X] (W was removed)
ShouldKeepOtherEntriesWhenAHandlerRunByGetClockEntryRemovesAnEntry
  entries [A, Z, W]; Z's handler removes A; GetClockEntry(Z)
  expected [Z, W], master leaves [Z, Z] (W was overwritten)
```

### Environment

- Linux (Docker, .NET 8 SDK), arm64
- Renode master 59f8f5d0 (renode-infrastructure 5474222a)

### Additional information

Found while reading the clock source for a performance change (issue B):
indexing entries by handler needs the index to be right after handlers run.

### Do you plan to address this issue and file a PR?

Yes: renode-infrastructure PR "[FIX] ClockSource: Look the entry up again
after running handlers".

---

## Issue B (template: Feature/support request)

**Title:** `Host performance: allocations, locks and linear searches on every bus access and timer event`

### Description

Profiling Renode on Cortex-M systems (STM32H7 firmware, one machine and two
in lock-step) shows the host spending a sizeable share of its time
outside the translated code, on bookkeeping done for every bus access and
every timer event, none of which changes what the emulation computes:

| Where | Cost, per event |
|---|---|
| `BaseClockSource` (every timer reload/read, every `Machine.ScheduleAction`) | linear search comparing delegates, with a closure; two `ThreadLocal<bool>` read several times; a `List` per update |
| `SystemBus` (every access) | a `Tuple`, a closure and a `DisposableWrapper` to set the access context; a locked-range check (two locks, up to three dictionary look-ups, `Any` with a closure) although nothing is locked; a lock and dictionary look-ups for the initiator's peripheral collection; a closure for the tag search |
| `PeripheralRegister` (every register read/write) | a `List` per access, `Distinct()` (a `HashSet`), LINQ over the tags; unhandled-write messages formatted before the logger drops them |
| `TranslationCPU` | a `new object()` per bus access (pause guard); a LINQ iterator per execution chunk (hooks); noisy log strings formatted before being dropped (opcode hooks, scheduled actions) |

On a two-machine STM32H7 workload (host `perf`, steady state): clock-entry
look-ups were 5.5 % of all samples, allocation 6.3 %, garbage collection
4.8 %. A register write on a Cortex-M4 allocates about 1.3 kB.

### Usage example

Any platform: the changes are internal, behaviour-preserving (same results,
same order of side effects, same log output) and need no configuration.

To measure: `tests/unit-tests/bus-performance.robot`'s loop for a fixed
virtual time, reporting host time and .NET's allocation counter (`renode
--disable-gui --console --plain bus.resc`):

```
i @tests/unit-tests/TrivialPeripheral.cs
mach create
machine LoadPlatformDescriptionFromString """
cpu: CPU.CortexM @ sysbus
    cpuType: "cortex-m4"
    nvic: nvic
nvic: IRQControllers.NVIC @ sysbus 0xE000E000
rom: Memory.MappedMemory @ sysbus 0x0
    size: 0x40000
trivial: Mocks.TrivialPeripheral @ sysbus 0x10000000
"""
cpu PerformanceInMips 100
cpu AssembleBlock 0 """
ldr r0, =0x10000000
loop:
ldr r2, [r0]
b loop
"""
cpu PC 0
emulation SetGlobalAdvanceImmediately true
emulation RunFor "0.05"
python "import time; from System import GC; a0 = GC.GetTotalAllocatedBytes(False); t0 = time.time()"
emulation RunFor "0.25"
python "print('wall=%.2f s alloc=%d B' % (time.time() - t0, GC.GetTotalAllocatedBytes(False) - a0))"
quit
```

Replacing the peripheral with `GPIOPort.STM32_GPIOPort @ sysbus <0x40020000, +0x400>`
and the loop's load with a read of `0x40020010` (IDR) or a store toggling
`0x40020008` (OSPEEDR) measures the register framework. On master
59f8f5d0 (arm64 Linux, 4 cores) that loop allocates 656 bytes per bus read,
a register read 786 bytes and a register write 1.36 kB; with the PRs below,
92, 127 and 280 bytes, and the three loops run 2.38x, 1.69x and 2.23x as
fast.

### Additional information

We carry these changes as a patch queue on 1.17.0 in a virtual
hardware-in-the-loop bench for Formula Student ECUs (STM32H733); they made it
13-58 % faster with byte-identical results (bus timelines, GPIO edges with
instruction counts, CPU state and RAM).

### Do you plan to address this issue and file a PR?

Yes: six small renode-infrastructure PRs, independent of one another except
that the clock source one builds on issue A's fix.
