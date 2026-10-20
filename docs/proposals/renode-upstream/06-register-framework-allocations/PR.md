PeripheralRegister: Avoid allocations on every register access

### Related issue

Part of renode/renode#ISSUE_B.

### Description

Performance improvement in the register framework, no behaviour change. Two
commits:

**PeripheralRegister: Allocate the changed fields list only when needed.**
Every `ReadInner` and `WriteInner` allocates a `List<RegisterField>` for the
fields it changed, runs `Distinct()` on it (a `HashSet` and an iterator) and,
on writes, filters the tags with LINQ (`InvalidTagValues`). Most reads change
no field and a write rarely changes more than one. The list is now created
when the first field changes (`AddChangedField`), `Distinct()` only runs with
two or more fields (it keeps each field's first occurrence in order, so the
change handlers run in the same order, each reading `UnderlyingValue` at its
call as before), the "any field changed" checks become null checks, and the
tags are filtered with a plain loop.

**PeripheralRegister: Build unhandled write messages only when logged.** A
write to bits no field handles builds a warning and a noisy message with
LINQ and string formatting (`TagLogger`, whose comment already says it was
"extracted as a method to allow future lazy evaluation"), which the logger
then drops unless its level lets them through (always for the noisy one at
the default level). Each is now built only when `Logger.MinimumLogLevel` is
at or below its level, the same check `Logger.LogAs` makes before logging
anything, as `PL330_DMA` already does. The output is the same.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository;
the numbers below come from the scripts in renode/renode#ISSUE_B.

Measured on master (Linux arm64 in Docker, 4 cores), with the loops in
renode/renode#ISSUE_B (.NET's allocation counter, deterministic to 1 %):

| per access | master | this PR |
|---|---|---|
| register read (`STM32_GPIOPort` IDR) | 786 B | 693 B |
| register write (`STM32_GPIOPort` OSPEEDR, one field changes) | 1,360 B | 845 B |

Host time, this PR alone against master (median of 5, interleaved, 4
cores; this host's bus loops vary by up to ±20 % run to run): register write
1.25x as fast (6.07 → 4.85 s for 0.25 s of virtual time; fastest runs 5.50
→ 4.15 s); register read 1.11x, within the noise.

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
  the 24 tests the series adds), and 47 Robot files (among them
  `register-read-caching`, `peripheral-hooks`, `log-tests` and the STM32
  peripheral and platform suites): 427 passed, 5 skipped, 2
  failed (master: 422 / 5 / 3; every failure is in `ramn.robot` and fails
  on master too)
- `dotnet format whitespace --verify-no-changes` on the changed files: passed
- byte-identical results: on our STM32H733 systems (one board, and two in
  lock-step, CAN between them, 3.5 s of virtual time), every CAN frame's
  timing in ns, every GPIO edge with the instruction count at its write, and
  each CPU's executed instructions, PC, register state and RAM digest are
  the same with the series as on master
