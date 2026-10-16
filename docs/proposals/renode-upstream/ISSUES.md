# Issues to open in renode/renode first

Renode's CONTRIBUTING.md asks for an issue even when you send the fix
yourself ("Please create an issue even if you plan to fix it"), and its
commit subjects carry the issue number (`[#1234] Tag: Description`; GitHub
issues land as `[GH#941]`, e.g. renode-infrastructure@[GH#941] NVIC). Open
these five in https://github.com/renode/renode/issues/new/choose, then put
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
- Renode master 9b5195e6 (renode-infrastructure 3e4301cf)

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
| `BaseClockSource` (every timer reload/read, every `Machine.ScheduleAction`, every update) | linear search comparing delegates, with a closure; two `ThreadLocal<bool>` read several times; a `List` per update; each enabled entry's tick arithmetic in `Fraction`, a GCD or LCM per operation |
| `STM32_Timer` (every update event) | the four compare channels' timers re-applied, unused ones included: eight clock entry exchanges per period |
| `SystemBus` (every access) | a `Tuple`, a closure and a `DisposableWrapper` to set the access context; a locked-range check (two locks, up to three dictionary look-ups, `Any` with a closure) although nothing is locked; a lock and dictionary look-ups for the initiator's peripheral collection; a closure for the tag search |
| `PeripheralRegister` (every register read/write) | a `List` per access, `Distinct()` (a `HashSet`), LINQ over the tags; unhandled-write messages formatted before the logger drops them |
| `TranslationCPU` | a `new object()` per bus access (pause guard); a LINQ iterator per execution chunk (hooks); noisy log strings formatted before being dropped (opcode hooks, scheduled actions) |
| `Packet` (every value decoded or encoded: CAN frames and filter elements, DMA and Ethernet descriptors, USB transfers) | `GetCustomAttributes` for every attribute read of every field (offset, width, alignment, padding, order, byte order): several reflection look-ups, each allocating, per field per value |

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
9b5195e6 (arm64 Linux, 4 cores) that loop allocates 656 bytes per bus read,
a register read 786 bytes and a register write 1.36 kB; with the PRs below,
92, 127 and 280 bytes, and the three loops run 2.76x, 2.24x and 1.91x as
fast. An STM32 timer with a 1 MHz update event and no channel in use
(STM32F4's TIM2) runs 2.64x as fast.

### Additional information

We carry these changes as a patch queue on 1.17.0 in a virtual
hardware-in-the-loop bench for Formula Student ECUs (STM32H733); they made it
13-58 % faster with byte-identical results (bus timelines, GPIO edges with
instruction counts, CPU state and RAM).

### Do you plan to address this issue and file a PR?

Yes: eight small renode-infrastructure PRs (the clock entry index, the
system bus, the register framework, `TranslationCPU`, `STM32_Timer`,
`Packet`), independent of one another except that the clock entry index
builds on issue A's fix.

---

## Issue C (template: Feature/support request)

**Title:** `Opcode execution hooks: let a hook stop being called at an address`

### Description

A pre-opcode execution hook (`TranslationCPU.AddPreOpcodeExecutionHook(mask,
value, action)`) is called before every executed instruction its mask
matches, wherever it is, each time it executes: a call from the translated
code into C#. Many hooks only have work to do at some of those addresses,
and learn which after their first call there:

- coverage or inventory hooks, which need only the first execution at each
  address (which `wfi`/`svc`/`bkpt`/`dmb` sites run, where an instruction
  class occurs);
- idiom detectors, which look for a code pattern at the instructions their
  mask matches (a busy-wait or delay loop around a compare, a magic
  instruction sequence for semihosting-like services) and find it at a few.

Today such a hook can only stay installed everywhere (hooks cannot be
removed, and removing one would remove it everywhere): every other execution
of a matching instruction still pays the call, which dominates when the
instruction is common (a compare, a load).

### Usage example

```python
cpu = self.Machine["sysbus.cpu"]
def on_nop(pc, opcode):
    self.Machine.InfoLog("first nop at 0x%x" % pc)
    cpu.ExcludePreOpcodeExecutionHookAt(on_nop, pc)   # proposed
cpu.AddPreOpcodeExecutionHook(0xFFFFFFFF, 0xBF00, on_nop)
```

Proposed API: `bool ExcludePreOpcodeExecutionHookAt(action, pc)` (blocks
translated afterwards don't call that hook at that address; the blocks
holding it are queued for invalidation) and
`ClearPreOpcodeExecutionHookExclusions()`, over two tlib exports consulted
at translation time, so translated code and hooks without exclusions are
unchanged. A hook on a loop of 15 `nop`s at 100 MIPS: 0.68 s of host time
for 0.25 s of virtual time with the hook called every time, 0.022 s with it
excluded after its first call at each address (0.023 s without a hook).

### Additional information

We use it in a virtual hardware-in-the-loop bench (STM32H733 firmware):
a hook on `CMP.W` recognises delay loops to skip them in virtual time, and
has nothing to do at the other `CMP.W`s. Excluding those after their first
call took its calls from 258,777 to 472 in 8 s of virtual time on a board
without delay loops.

### Do you plan to address this issue and file a PR?

Yes: antmicro/tlib "Allow excluding a pre-opcode execution hook at an
address", renode/renode-infrastructure "TranslationCPU: Allow excluding a
pre-opcode execution hook at an address" (which needs the tlib change in the
submodule) and renode/renode "tests: Test excluding a pre-opcode execution
hook at an address". If you would rather have a different shape (e.g. a
return value from the hook meaning "not here again", or post-opcode hooks
too), we are happy to adapt.

---

## Issue D (template: Feature/support request)

**Title:** `Time framework: a slave time source costs an extra thread hand-off per quantum`

### Description

A machine created with a time source of its own
(`new Machine(createLocalTimeSource: true)`) runs on a `SlaveTimeSource`
attached to the emulation's master time source. The slave hands every grant
on to its sinks (the machine's CPU threads) from a dispatcher thread of its
own, so each quantum goes master thread → slave's thread → CPU threads and
the reports come back the same way: two thread hand-offs per quantum per
machine where a machine on the master time source has one, and 2N + 1
threads for N machines, which spin while they wait for each other.

Machines from `mach create` are not affected (they share the master time
source since renode-infrastructure 63702bc7, "[#32007] Machine: Use
MasterTimeSource by default"). It matters to whoever gives machines a time source of
their own, typically so that each machine's timers, sync points and
delayed actions (`ExecuteInSyncedState`) follow that machine's own time
rather than the slowest CPU's, which keeps multi-machine runs repeatable.

The cost is the time framework's, not the machines' work:

- Renode's two-node BLE demo (`scripts/multi-node/nrf52840-ble-zephyr.resc`:
  Zephyr's heart-rate central and peripheral, 10 us quanta, both nodes
  idle most of the time), 5 s of virtual time, with its `mach create`s
  replaced by machines with time sources of their own: 8.01 s of host time
  and 6.1 CPU seconds per virtual second, against 2.87 s and 2.1 on the
  master time source;
- two Cortex-M4s spinning, 10 us quanta, 0.5 s: 1.31 s and 9.8 CPU s/s,
  against 0.68 s and 5.1;
- in a three-machine STM32H7 emulation of ours (host `perf` by thread,
  x86), the slaves' dispatcher threads were a quarter of all samples.

(Master 9b5195e6, Linux arm64 in Docker, 4 cores, median of 7. The scripts
create each machine with
`EmulationManager.Instance.CurrentEmulation.AddMachine(Machine(True), name)`
from the monitor's Python, then `mach set` it.)

### Usage example

```python
from Antmicro.Renode.Core import EmulationManager, Machine
m = Machine(True)                             # a time source of its own
m.LocalTimeSource.RunsOnMasterThread = True   # proposed
EmulationManager.Instance.CurrentEmulation.AddMachine(m, "m0")
```

Proposed: `SlaveTimeSource.RunsOnMasterThread`, off by default. When set
(before the slave is attached), the master begins the slave's iteration on
its own thread when it grants the slave time and ends it when it waits for
it, so the slave's sinks run beside the master's other sinks with no
dispatcher thread in between: one hand-off per quantum per machine and
N + 1 threads. Each machine keeps its own time source (quantum, sync
points, sync hooks, delayed actions, pause); the difference is that the
slaves' sync phases run on the master's thread one after another instead
of concurrently on their own threads.

### Additional information

We use machines with time sources of their own in a virtual
hardware-in-the-loop bench (two and three STM32H733 boards in lock-step,
CAN between them) and carry this change as a patch on 1.17.0: steady state
20-23 % faster with two and three boards and 29-36 % less CPU (x86, 4
vCPUs), and byte-identical results (bus timelines, GPIO edges with
instruction counts, CPU state and RAM). With the change, the BLE demo above takes 3.83 s and
2.8 CPU s/s, the spinning pair 1.00 s and 7.6.

While testing it we found that a precise pause (`Wait For Line On Uart ...
pauseEmulation=true`, `tests/unit-tests/precise-pause.robot`) never returns
when the machine has a time source of its own, on master already; a
separate problem, reported here in case it is not known.

### Do you plan to address this issue and file a PR?

Yes: renode/renode-infrastructure "SlaveTimeSource: Optionally run on the
master's thread" (three commits: `InnerExecute` split into a begin and an
end half, `RequestTimeInterval(noWait)`, the mode). We made it opt-in to
keep the default untouched; if you would rather have it on by default for
machines with a time source of their own, or set through `Machine`, we are
happy to change it.

---

## Issue E (template: Feature/support request)

**Title:** `Memory access hook: let a hook see only peripheral (IO) accesses`

### Description

`TranslationCPU.SetHookAtMemoryAccess(hook)` calls `hook` for every load,
store and instruction fetch, and while it is set translated code doesn't use
the TLB fast path (`tlib_on_memory_access_event_enabled(1)` turns it off), so
every RAM access goes through tlib's softmmu helpers and then into C#. Many
users of the hook only care about peripheral accesses, which tlib already
tells apart (`MemoryOperation.MemoryIORead`/`MemoryIOWrite`):

- a trace of a driver's MMIO (which registers firmware touches, in which
  order) while the rest of the code runs normally;
- the profiler's peripheral entries (`EnableProfiling` logs `MemoryIORead`
  and `MemoryIOWrite` as `PeripheralEntry`);
- a detector of polling loops, which needs the reads of the polled register.

Today such a hook pays for every RAM access too, which is nearly all of them:
on a Cortex-M4 at 100 MIPS copying words within RAM and reading a peripheral
every 64 copies, setting a hook that counts its calls makes the run
5.6 times slower (0.37 s of host time for 0.25 s of virtual time without a
hook, 2.08 s with it; master d0c70ee3, Linux arm64 in Docker).

### Usage example

```python
cpu = self.Machine["sysbus.cpu"]
def on_access(pc, operation, virtualAddress, physicalAddress, width, value):
    self.Machine.InfoLog("%s 0x%x at pc 0x%x" % (operation, physicalAddress, pc))
cpu.SetHookAtMemoryAccess(on_access, True)   # proposed: IO accesses only
```

Proposed: a mode of tlib's memory access event,
`tlib_on_memory_access_event_enabled(2)` (`MEMORY_ACCESS_EVENT_IO_ONLY`):
IO accesses and slow (unaligned, page-spanning) ones are reported, a load or
store that hits RAM within a page is not, and translated code keeps the TLB
fast path; 0 and 1 unchanged. In Renode,
`SetHookAtMemoryAccess(hook, ioAccessesOnly)` beside the existing
`SetHookAtMemoryAccess(hook)`. With it, the loop above runs as fast as
without a hook.

### Additional information

We use it in a virtual hardware-in-the-loop bench (STM32H733 firmware): a
detector arms a memory access hook around polling loops to fast-forward
them in virtual time, and only needs their MMIO reads. We carry the tlib
side as a patch on 1.17.0 (and write tlib's flag directly, as Renode has no
API for it); on Apple-silicon hosts, whose TCG backend calls the softmmu
helpers for every access, it made our boards boot 10-54 % faster, with
byte-identical results.

Noticed on the way, not addressed by the proposal: enabling the hook for
every access after code was translated leaves the blocks translated with the
TLB fast path in the cache (nothing flushes it when tlib stops using the
TLB), so on x86 hosts their RAM hits are not reported until they are
retranslated.

### Do you plan to address this issue and file a PR?

Yes: antmicro/tlib "Allow reporting only IO accesses to the memory access
hook", renode/renode-infrastructure "TranslationCPU: Allow a memory access
hook for IO accesses only" (which needs the tlib change in the submodule)
and renode/renode "tests: Test a memory access hook limited to IO
accesses". If you would rather have another shape (a separate hook for
peripheral accesses, or the mode on `ICPUWithMemoryAccessHooks`), we are
happy to adapt.
