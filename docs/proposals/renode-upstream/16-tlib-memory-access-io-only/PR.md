Allow reporting only IO accesses to the memory access hook

### Related issue

renode/renode#ISSUE_E. The Renode side (`TranslationCPU`, which uses this
from the tlib submodule) and its tests are separate pull requests to
renode/renode-infrastructure and renode/renode: PR_17 and PR_18.

### Description

New mode of an existing export; 0 and 1 behave as before.

While the memory access hook is enabled
(`tlib_on_memory_access_event_enabled(1)`), translated code doesn't use the
TLB fast path (`tcg_context_use_tlb(0)`): every load and store goes
through the softmmu helpers, and each reports itself to
`tlib_on_memory_access`, a call into the emulator. A user that only cares
about peripheral (IO) accesses (a trace of a driver's MMIO, a detector of
polling loops, a profiler of peripheral traffic) pays that for every RAM
access as well, which is nearly all of them, and slows the CPU down by
several times.

This adds a mode, `MEMORY_ACCESS_EVENT_IO_ONLY` (2), and names the values
in a new `enum memory_access_event` in `cpu-defs.h`:

- `MEMORY_ACCESS_EVENT_DISABLED` (0) and `MEMORY_ACCESS_EVENT_ALL` (1): as
  before; any other value but 2 is 1, as `!!value` made it;
- `MEMORY_ACCESS_EVENT_IO_ONLY` (2): IO accesses are reported, and so are
  slow ones (unaligned, page-spanning: `slow_ld`/`slow_st`, which an
  unaligned IO access also takes); a load or store that hits RAM within a
  page is not. Translated code keeps the TLB fast path, as with the hook
  off, since what it serves inline (RAM hits) is what this mode doesn't
  report.

In `softmmu_template.h` only the two "access in the same page" branches
(load and store) check for `MEMORY_ACCESS_EVENT_ALL` instead of non-zero;
the IO and slow branches are unchanged. `tlib_on_memory_access_event_enabled`
stores the mode and keeps the TLB unless it is
`MEMORY_ACCESS_EVENT_ALL`. The operation passed to the hook is unchanged
(`MEMORY_IO_READ`/`MEMORY_IO_WRITE` for IO, `MEMORY_READ`/`MEMORY_WRITE`
for the slow path), so a hook can still tell them apart.

What is reported in the IO-only mode does not depend on the host: with the
TLB fast path (x86 hosts) a RAM hit never leaves the translated code, and
without it (hosts whose TCG backend has no inline TLB look-up, e.g.
aarch64, where every access calls the helpers) the helper does not report
it.

### Usage example

From Renode (PR_17): `cpu.SetHookAtMemoryAccess(hook, true)` calls `hook`
for the CPU's peripheral accesses only.

Measured with `tooling/bench/memhook-*.resc` in our measurements (a
Cortex-M4 at 100 MIPS copying a word within RAM 64 times, then reading a
peripheral, in a loop; a C# hook counting its calls; 0.25 s of virtual
time; median of 5, interleaved, Linux arm64 in Docker, 4 cores): without a
hook 0.37 s of host time; with the hook for every access 2.08 s (5.6x
slower); with it limited to IO accesses 0.39 s, as fast as without a hook
(96,525 calls, one per peripheral read, instead of 12.45 million). On this arm64 host the TCG
backend has no inline TLB look-up, so the gain is the calls into the
emulator; on x86 the TLB fast path comes back as well.

### Additional information

Not addressed here, but noticed: enabling the hook for every access after
code has been translated leaves the blocks translated with the TLB fast
path in the cache (nothing flushes it when `use_tlb` changes), so on a host
with inline TLB look-ups their RAM hits are not reported until those blocks
are retranslated. The IO-only mode doesn't depend on it (it keeps the TLB
either way), and switching from every access to IO only is exact.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master d0c70ee3 with renode-infrastructure master 48cee122
and this on tlib master 0dd9a30c (with PR_17 and PR_18):

- headless portable build (`./build.sh -t --no-gui`): succeeded
- `memory-access-hook.robot` (PR_18): 5 passed (on master 1 passes and
  the 4 new cases fail)
- the Robot suites that set a memory access hook or exercise CPU hooks
  (`execution-tracing`, `stm32f4-erase`, `hooks`,
  `opcodes-execution-hooks`, `memory-access`, `external-mmu`), the CAN and
  Ethernet suites and `nucleo_h753zi`, `STM32F4_Discovery`: all pass, as on
  master
- `clang-format` 19.1.0 with tlib's `.clang-format`: no change
- byte-identical results: on our STM32H733 systems (one, two and three
  boards, 10 s of virtual time) with a detector that arms an IO-only
  memory access hook around polling loops, every CAN frame's timing in ns,
  every GPIO edge with the instruction count at its write, and each CPU's
  executed instructions, PC, register state and RAM digest are the same as
  on master
