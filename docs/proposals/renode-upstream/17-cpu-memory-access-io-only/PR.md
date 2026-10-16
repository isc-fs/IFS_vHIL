TranslationCPU: Allow a memory access hook for IO accesses only

### Related issue

renode/renode#ISSUE_E. Needs antmicro/tlib#PR_16 (the tlib side) in the
tlib submodule: open this PR once the submodule contains it, or with a
commit moving the submodule to it (`tlib: Bump`, see below).

### Description

New overload, no change for existing users.

`SetHookAtMemoryAccess(hook)` calls `hook` for every load and store (and
instruction fetch) of the CPU, and while it is set translated code doesn't
use the TLB fast path: every access goes through tlib's softmmu helpers and
then into C#. A hook that only watches peripherals (MMIO) still pays both
for every RAM access. This adds:

- `void SetHookAtMemoryAccess(MemoryAccessHook hook, bool ioAccessesOnly)`:
  with `ioAccessesOnly` true, `hook` is called for IO (peripheral)
  accesses, `MemoryOperation.MemoryIORead`/`MemoryIOWrite`, and for slow
  (unaligned, page-spanning) accesses, and not for loads and stores that
  hit RAM within a page; translated code keeps the TLB fast path
  (antmicro/tlib#PR_16's `MEMORY_ACCESS_EVENT_IO_ONLY`). With false it is
  `SetHookAtMemoryAccess(hook)`, which now calls it.

The mode is kept beside the hook (`memoryAccessHookIoAccessesOnly`), so
`RestoreState` re-enables the event in the same mode after
deserialization (it re-sent 1 unconditionally). `ICPUWithMemoryAccessHooks`
is unchanged. On a tlib without antmicro/tlib#PR_16, mode 2 is treated as 1
(`!!value`): the hook is called for every access, as before.

### Usage example

```python
cpu = self.Machine["sysbus.cpu"]
def on_access(pc, operation, virtualAddress, physicalAddress, width, value):
    self.Machine.InfoLog("%s 0x%x at pc 0x%x" % (operation, physicalAddress, pc))
cpu.SetHookAtMemoryAccess(on_access, True)
```

logs the CPU's peripheral accesses, while the code runs from RAM at full
speed.

Measured with `tooling/bench/memhook-*.resc` in our measurements (a
Cortex-M4 at 100 MIPS copying a word within RAM 64 times, then reading a
peripheral, in a loop; a C# hook counting its calls, set from a peripheral
model; 0.25 s of virtual time; median of 5, interleaved, Linux arm64 in
Docker, 4 cores): without a hook 0.37 s of host time; `(hook)` 2.08 s
(5.6x slower); `(hook, true)` 0.39 s, as fast as without a hook.

Our use: on STM32H733 firmware a detector arms a memory access hook around
polling loops (it needs only their MMIO reads, to fast-forward them in
virtual time) for a few iterations at a time; today it sets the hook, then
turns the event off and writes tlib's flag directly while armed, to keep
the TLB fast path. With IO accesses only (antmicro/tlib#PR_16), on this
arm64 host, our boards' boots are 17-43 % faster where the detector arms
(two of the boards, and the third and the three-board system on our
lock-step path), one board's steady state 20-29 %, with byte-identical
results; this API would let it drop the direct write.

### Additional information

The commit that moves the tlib submodule is not part of this PR's patch:
with antmicro/tlib#PR_16 merged it is the usual submodule bump; before
that, point the submodule at the tlib PR's branch in a fork to build.

Tests: `tests/unit-tests/memory-access-hook.robot` in renode/renode
(PR_18): a hook set with `SetHookAtMemoryAccess(hook)` and with `(hook,
false)` sees a program's RAM load and store and its two NVIC accesses;
with `(hook, true)` only the NVIC accesses, also after switching an
every-access hook to IO only over blocks already translated; removing the
hook stops the calls.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master d0c70ee3 with renode-infrastructure master 48cee122
and tlib master 0dd9a30c + antmicro/tlib#PR_16:

- `memory-access-hook.robot` with PR_18's tests: 5 passed (on master the
  first, which pins the existing behaviour, passes and the 4 new ones fail:
  no such overload; with this PR on tlib master, without
  antmicro/tlib#PR_16, the 3 IO-only cases fail: the hook gets every access)
- headless portable build (`./build.sh -t --no-gui`): succeeded
- the Robot suites that set a memory access hook or exercise CPU hooks
  (`execution-tracing`, `stm32f4-erase`, `hooks`,
  `opcodes-execution-hooks`, `memory-access`, `external-mmu`) and the CAN
  and Ethernet suites, `nucleo_h753zi`, `STM32F4_Discovery`: 146 passed, as
  on master
- `dotnet format whitespace --verify-no-changes` on the changed file:
  passed
