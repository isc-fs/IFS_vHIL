SystemBus: Set the access context without allocating

### Related issue

Part of renode/renode#ISSUE_B.

### Description

Performance improvement in `SystemBus`'s access path, no behaviour change.

Every bus access with an initiator sets it as the thread's context for the
access (`SetLocalContext`, from the generated accessors) and puts the
previous context back when the returned `IDisposable` is disposed. That
allocates a `Tuple<IPeripheral, ulong?>`, a closure capturing the previous
context and a `DisposableWrapper` per access.

`ThreadLocalContext` now keeps a per-thread stack of contexts whose entries
are allocated once per nesting depth and reused. `Initialize` pushes the
initiator and its state and returns the entry; disposing the entry sets the
stack's depth back to what it was before the push. That gives back the
previous context, as before, including when the disposables are disposed
out of order or twice (restoring a depth, like restoring a snapshot, is
idempotent). `InUse`, `Initiator` and `InitiatorState` read the top entry;
`Initialize(null, ...)` still returns the shared no-op disposable and leaves
the context as it is.

One difference only a misuse could see: an entry restores the stack of the
thread that pushed it, where the old closure set the context of whichever
thread disposed it. Every caller (the generated accessors, `ReadBytes`/
`WriteBytes`, `BaseARMv8`'s system-register access) disposes in a `using` on
the same thread.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository;
the numbers below come from the scripts in renode/renode#ISSUE_B.

Measured on master (Linux arm64 in Docker, 4 cores), with the loops in
renode/renode#ISSUE_B (.NET's allocation counter, deterministic to 1 %):

| per access | master | this PR |
|---|---|---|
| bus read (`TrivialPeripheral`) | 658 B | 402 B |
| register read (`STM32_GPIOPort` IDR) | 786 B | 530 B |
| register write (`STM32_GPIOPort` OSPEEDR) | 1,360 B | 1,102 B |

With the whole series (renode/renode#ISSUE_B; master vs master + the seven
PRs, median of 5, interleaved): the bus-performance loop runs 2.38x as fast,
a register read 1.69x, a register write 2.23x; our STM32H733 systems (one and
two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 13-53 % faster in
steady state.

### Additional information

Test added: `SystemBusTests.ShouldRestoreTheTransactionInitiatorAfterNestedContexts`
(nested contexts, a null context, back to none); it passes on master too.

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 59f8f5d0 with renode-infrastructure master 5474222a:

- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,099
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the four tests the series adds), and 38 Robot suites (among them `sysbus`,
  `sysbus-data-race`, `per-core-registration`, `bus-isolation` and
  `arm-cortex-m-trustzone`, which read the initiator): 330 passed, 1
  skipped, 0 failed, as on master
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
