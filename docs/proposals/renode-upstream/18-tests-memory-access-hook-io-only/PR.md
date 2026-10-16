tests: Test a memory access hook limited to IO accesses

### Related issue

renode/renode#ISSUE_E. Tests for renode/renode-infrastructure#PR_17 (which
needs antmicro/tlib#PR_16): merge once `src/Infrastructure` contains both.

### Description

Tests only. `tests/unit-tests/memory-access-hook.robot` (new, listed in
`tests/tests.yaml` after `memory-access.robot`): a Cortex-M4 runs a
program with a load and a store in RAM and a load and a store in the NVIC
(`SYST_CSR`, `SYST_RVR`), stepping over it, with a Python memory access
hook that records each data access as `<operation> <address>`:

- `SetHookAtMemoryAccess(hook)`: the four accesses (pins the existing
  behaviour; passes on master);
- `SetHookAtMemoryAccess(hook, False)`: the same;
- `SetHookAtMemoryAccess(hook, True)`: the two NVIC accesses only;
- an every-access hook switched to IO only after the program ran (its
  blocks already translated): the two NVIC accesses only;
- an IO-only hook removed (`SetHookAtMemoryAccess(None)`): no access.

### Usage example

```
renode-test tests/unit-tests/memory-access-hook.robot
```

### Additional information

On renode master d0c70ee3 the first case passes and the other four fail
(no such overload); with renode/renode-infrastructure#PR_17 and
antmicro/tlib#PR_16 in the submodules, all five pass (Docker, Linux
arm64). With renode/renode-infrastructure#PR_17 on tlib master (without
antmicro/tlib#PR_16) the first two pass and the three IO-only cases fail:
an older tlib treats the mode as "every access".
