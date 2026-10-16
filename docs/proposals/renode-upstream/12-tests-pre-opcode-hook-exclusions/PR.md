tests: Test excluding a pre-opcode execution hook at an address

### Related issue

renode/renode#ISSUE_C. Tests for renode/renode-infrastructure#PR_11 (with
antmicro/tlib#PR_10): this needs `src/Infrastructure` to contain PR_11.

### Description

Four test cases in `tests/unit-tests/opcodes-execution-hooks.robot`, on the
file's Cortex-M33 machine, with three `nop`s at 0x0 and a pre-opcode
execution hook on `nop` (0xBF00) that records the PC of each call (two new
keywords set it up and run the three instructions from 0x0, checking the
recorded PCs):

- **Should Not Call Pre Opcode Execution Hook At Excluded Address**: the hook
  is excluded at 0x2 before anything runs (`ExcludePreOpcodeExecutionHookAt`
  returns `True`, and `False` for the same address again); it is then called
  at 0x0 and 0x4 only.
- **Should Stop Calling Pre Opcode Execution Hook In Translated Code After
  Exclusion**: a first run calls it at 0x0, 0x2 and 0x4; excluded at 0x2, the
  already translated block is invalidated and the next run calls it at 0x0
  and 0x4.
- **Should Call Pre Opcode Execution Hook Again After Clearing Exclusions**:
  excluded at 0x2, then `ClearPreOpcodeExecutionHookExclusions`: called at
  all three again.
- **Should Fail To Exclude An Unknown Pre Opcode Execution Hook**: a hook
  that was never added is refused with an error.

The hook is a Python function, passed to both calls (IronPython converts it
to an equal delegate each time, and hooks are identified by delegate
equality).

### Usage example

```
renode-test tests/unit-tests/opcodes-execution-hooks.robot
```

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 + this PR, renode-infrastructure master
3e4301cf + PR_11, tlib master 0dd9a30c + antmicro/tlib#PR_10:
6 passed, 0 failed (the file's two existing cases and the four new
  ones). Without PR_11 the four new cases fail (no such method), the two
existing ones pass.
