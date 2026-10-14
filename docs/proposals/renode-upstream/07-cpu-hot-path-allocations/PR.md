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

Host time: alone, within this host's noise on the bus loops (±20 % run
to run there). On a loop with a pre-opcode execution hook on every `nop` (a
Cortex-M4 at 100 MIPS, 0.25 s of virtual time) the hook handler's noisy
message was 13.7 GB of allocation per virtual second; with the series (of
which only this PR touches that path) 0.05 GB, and the loop runs 3.7x as
fast (2.49 → 0.68 s).

With the whole series (renode/renode#ISSUE_B, plus renode/renode#ISSUE_C's
hook exclusion; master vs master + every PR, median of 5, interleaved): the
bus-performance loop runs 2.76x as fast, a register read 2.24x, a register
write 1.91x, an STM32 timer's update path 2.64x; our STM32H733 systems (one
and two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 15-62 %
faster in steady state.

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master 3e4301cf:

- headless portable build (`./build.sh -t --no-gui`): succeeded with this PR
  alone and with the whole series
- the whole series together: the NUnit projects of `tests/tests.yaml`
  (`UnitTests`, `PeripheralsTests`, `MonitorTests`, `RenodeTests`) 1,119
  passed, 18 skipped, 0 failed (master: 1,095 / 18 / 0; the difference is
  the 24 tests the series adds), and 47 Robot files (among them `hooks`,
  `opcodes-execution-hooks`, `opcodes-counting`, `cpu-pause-halt`,
  `pause-without-sleep` and `log-tests`): 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails
  on master too)
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
