TranslationCPU, Machine: Avoid allocations and noisy log formatting on hot paths

### Related issue

Part of renode/renode#ISSUE_B.

### Description

Performance improvement, no behaviour change. Two small commits:

**TranslationCPU: Avoid allocations on every bus access and execution
chunk.** `CpuThreadPauseGuard.Initialize`, called for every bus access,
stores a `new object()` in a `ThreadLocal<object>` of which only being
non-null is ever checked; it now stores one shared object.
`HookDescriptorBase.ActivateNewHooks`, called before every execution chunk,
filtered the hooks with LINQ (`Where` and a closure); it now loops over
`hooks.Values` in the same order.

**Machine, TranslationCPU: Format noisy messages only when they are logged.**
`Machine.ScheduleAction`'s clock entry handler boxes two `TimeInterval`s for
a noisy message on every scheduled action, and the pre/post opcode execution
hook handlers format an interpolated string for every hooked instruction,
before `Logger.LogAs` drops the message at any level above Noisy. Both now
check `Logger.MinimumLogLevel <= LogLevel.Noisy` first, as `PL330_DMA` does.
The output is the same.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository;
the numbers below come from the scripts in renode/renode#ISSUE_B.

Measured on master (Linux arm64 in Docker, 4 cores), with the loops in
renode/renode#ISSUE_B (.NET's allocation counter, deterministic to 1 %): 27
bytes less per bus access (the pause guard's object; 658 to 631 B per read),
and on Renode's demos 4-13 % less allocation (`stm32f4_discovery`,
`nucleo_h753zi_hubris`, `nrf52840`). The scheduled-action and opcode-hook
messages cost most where they are used: our firmware's fast-forward hooks a
compare instruction in polling loops, and our CAN model wakes through
`ScheduleAction`.

With the whole series (renode/renode#ISSUE_B; master vs master + the seven
PRs, median of 5, interleaved): the bus-performance loop runs 2.38x as fast,
a register read 1.69x, a register write 2.23x; our STM32H733 systems (one and
two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 13-53 % faster in
steady state.

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 59f8f5d0 with renode-infrastructure master 5474222a:

- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,099
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the four tests the series adds), and 38 Robot suites (among them `hooks`,
  `opcodes-execution-hooks`, `opcodes-counting`, `cpu-pause-halt`,
  `pause-without-sleep` and `log-tests`): 330 passed, 1 skipped, 0 failed,
  as on master
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
