Allow excluding a pre-opcode execution hook at an address

### Related issue

renode/renode#ISSUE_C. The Renode side (`TranslationCPU`, which needs this
in the tlib submodule) and its tests are separate pull requests to
renode/renode-infrastructure and renode/renode: PR_11 and PR_12.

### Description

A pre-opcode execution hook (`tlib_install_pre_opcode_execution_hook`) is
called before every executed instruction its mask matches, wherever that
instruction is. A hook that only has work to do at some of those addresses
still costs a call into the emulator (`tlib_handle_pre_opcode_execution_hook`,
i.e. a native-to-managed transition) at every other one, on every execution.
Examples: a hook that needs only the first execution at each address
(coverage of an instruction class, or a table of where some instruction
occurs), or one that recognises a code idiom (a busy-wait loop, a magic
instruction sequence) that occurs at a few of the places its mask matches.
Such a hook knows after its first call at an address that it has nothing
more to do there, but can only remove itself everywhere.

This adds two exports:

- `tlib_exclude_pre_opcode_execution_hook(pc, hook_id)`: blocks translated
  from now on don't call hook `hook_id` at `pc`; other hooks at `pc`, and the
  hook elsewhere, are unaffected. Returns 1 if the pair was added, 0 if it
  was already there, the id is out of range or the set is full. Blocks
  already translated keep calling it until they are invalidated: the caller
  invalidates them (the Renode side does it before the CPU's next execution
  chunk).
- `tlib_clear_pre_opcode_execution_hook_exclusions()`: empties the set;
  blocks translated from now on call every hook wherever its mask matches.

`generate_pre_opcode_execution_hook` checks the set for each hook whose mask
matches, at translation time only: translated code is unchanged, and an
excluded address simply gets no call. The set is an open-addressed table of
(pc, hook id) pairs, 4,096 slots kept at most three quarters full (3,072
exclusions), per tlib instance like the other translation state (one per
CPU); while it is empty, translation does one extra compare per matching
instruction. A full set refuses further exclusions (the hook is then still
called there) rather than growing.

What is not covered: post-opcode execution hooks (the same could be done
for them; no use for it so far), removing one exclusion (clear all, then
exclude again), and snapshots (the set is not part of `CPUState`; it is
rebuilt by whoever excluded the addresses, like the hooks themselves, which
the Renode side does not serialize either).

### Usage example

Through the Renode side (PR_11):

```python
cpu = self.Machine["sysbus.cpu"]
seen = set()
def first_nop(pc, opcode):
    seen.add(pc)
    cpu.ExcludePreOpcodeExecutionHookAt(first_nop, pc)   # nothing more to do here
cpu.AddPreOpcodeExecutionHook(0xFFFFFFFF, 0xBF00, first_nop)   # every Thumb `nop`
```

Measured with a Cortex-M4 at 100 MIPS running a loop of 15 `nop`s and a
branch, a hook on `nop` (in C#), median of 5, Linux arm64 in Docker: without a hook 0.023 s of
host time for 0.25 s of virtual time; with the hook called at every `nop`
(about 23 million calls) 0.68 s; with the hook excluding itself at each
address after its first call, 0.022 s, as fast as without a hook. (Measured
with the whole series; on master the hook called every time takes 2.49 s,
as its handler also formats a noisy log message per call, which PR 07 of
renode/renode#ISSUE_B removes.)

On our STM32H733 firmware, a hook on `CMP.W` that recognises delay loops (to
skip them) was called 258,777 times in 8 s of virtual time on a board with
no delay loop; with each non-loop `CMP.W` excluded after its first call, 472
times (its share of host samples from 1.0 % to below 0.02 %).

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), on tlib master 0dd9a30c (renode master 9b5195e6,
renode-infrastructure master 3e4301cf, with the submodule moved to this
change):

- headless portable build (`./build.sh -t --no-gui`), every architecture:
  succeeded
- `clang-format` 19.1.0 (`.pre-commit-config.yaml`'s) with the repository's
  `.clang-format`: no changes to the four files
- the Robot tests of PR_12 (`opcodes-execution-hooks.robot`: an excluded
  address gets no call while the others still do, also when its block was
  already translated; a second exclusion returns false; clearing gives the
  address its hook back; an unknown hook is refused) and the existing ones pass:
  6 passed, 0 failed (the file's two existing cases and the four new
  ones)
- the full test set of the series (renode/renode#ISSUE_B), with this change
  in: NUnit 1,119 passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0),
  47 Robot files 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails on master too)
