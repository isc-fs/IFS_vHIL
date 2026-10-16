TranslationCPU: Allow excluding a pre-opcode execution hook at an address

### Related issue

renode/renode#ISSUE_C. Needs antmicro/tlib#PR_10 (the tlib side) in the
tlib submodule: open this PR once the submodule contains it, or with a
commit moving the submodule to it (`tlib: Bump`, see below).

### Description

New API, no change for existing users.

A pre-opcode execution hook (`AddPreOpcodeExecutionHook`) is called before
every executed instruction its mask matches. A hook that only has work to
do at some of those addresses (the first execution at each address, or a
code idiom found at a few of them) pays a call from the translated code into
C# everywhere else. This adds:

- `bool ExcludePreOpcodeExecutionHookAt(Action<ulong, ulong> action, ulong pc)`:
  the hook `action` (as passed to `AddPreOpcodeExecutionHook`) is no longer
  called at `pc` in blocks translated from now on
  (`tlib_exclude_pre_opcode_execution_hook`), and the blocks already holding
  `pc` are invalidated before the next execution chunk, on the CPU thread
  (`OrderTranslationBlocksInvalidation(pc, pc + 4, delayInvalidation: true)`,
  then `InvalidateTranslationBlocks` queued once with
  `InvokeInCpuThreadSafely`). So the hook can exclude itself, which is the
  common case, without the block it runs in being invalidated under it;
  until the chunk ends that block still calls it. No return is requested, so
  the execution's timing is not changed. Returns `false` if the address was
  already excluded or tlib's table is full (3,072 exclusions per CPU), in
  which case the hook is still called there; throws `RecoverableException`
  for a hook that was never added.
- `void ClearPreOpcodeExecutionHookExclusions()`: every address gets its
  hooks back; the translation cache is cleared before the next execution
  chunk (`pendingTranslationCacheClearing`, as
  `RequestTranslationCacheClearing` does, without its return request).

Both are meant for the CPU thread (a hook, a CPU-thread action) or a paused
machine, as tlib's translation reads the exclusions.

The hook is identified as `AddPreOpcodeExecutionHook` stores it, by
delegate equality: pass the same delegate, or from Python the same function
(IronPython converts it to an equal delegate each time).

### Usage example

```python
cpu = self.Machine["sysbus.cpu"]
def on_nop(pc, opcode):
    self.Machine.InfoLog("first nop at 0x%x" % pc)
    cpu.ExcludePreOpcodeExecutionHookAt(on_nop, pc)
cpu.AddPreOpcodeExecutionHook(0xFFFFFFFF, 0xBF00, on_nop)
```

logs each Thumb `nop` the first time it runs (and the few more times it runs
until the current execution chunk ends), however often it runs afterwards.

Measured with a Cortex-M4 at 100 MIPS running a loop of 15 `nop`s and a
branch for 0.25 s of virtual time, a C# hook on `nop` that counts its calls
(`tooling/bench/hook-*.resc` in our measurements), median of 5, Linux arm64
in Docker, 4 cores: without a hook 0.023 s of
host time for 0.25 s of virtual time; with the hook called at every `nop`
(about 23 million calls) 0.68 s; with the hook excluding itself at each
address after its first call, 0.022 s, as fast as without a hook. (Measured
with the whole series; on master the hook called every time takes 2.49 s,
as its handler also formats a noisy log message per call, which PR 07 of
renode/renode#ISSUE_B removes.)

Our use: on STM32H733 firmware, a hook on `CMP.W` recognises delay loops (to
skip them in virtual time) and has nothing to do at any other `CMP.W`; with
those excluded after their first call, it was called 472 times instead of
258,777 in 8 s of virtual time on a board with no delay loop, and its share
of host samples went from 1.0 % to below 0.02 %.

### Additional information

The commit that moves the tlib submodule is not part of this PR's patch:
with antmicro/tlib#PR_10 merged it is the usual submodule bump; before that,
point the submodule at the tlib PR's branch in a fork to build.

Tests: `tests/unit-tests/opcodes-execution-hooks.robot` in renode/renode
(PR_12): an excluded address gets no call while the others still do, also
when the block holding it was already translated; a second exclusion of the
same address returns `false`; clearing gives the address its hook back; an
unknown hook is refused.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master
3e4301cf and tlib master 0dd9a30c + antmicro/tlib#PR_10:

- `opcodes-execution-hooks.robot` with PR_12's tests: 6 passed, 0 failed (the file's two existing cases and the four new
  ones)
- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,119
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the 24 tests the series adds), and 47 Robot files (among them every STM32
  platform suite, `timer-reconfiguration`, `virtual-time`,
  `opcodes-execution-hooks` and `execution-tracing`): 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails
  on master too)
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
