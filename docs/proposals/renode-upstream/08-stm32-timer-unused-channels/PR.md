STM32_Timer: Leave unused compare channels alone on update events

### Related issue

Part of renode/renode#ISSUE_B.

### Description

Performance improvement in `STM32_Timer`, no behaviour change.

The update event (`LimitReached`, every period of the counter) calls
`UpdateTimer()` on all four capture/compare channels, which writes the
channel timer's `Enabled` and `Direction`: two clock entry exchanges (each a
new `ClockEntry`, an entry search and a request for another update pass) and
two CPU return requests, per channel, per period. On a typical STM32
firmware most channels are unused: an HAL timebase or a periodic tick timer
(e.g. TIM6/TIM7, or TIM2 at 1 kHz) runs with none of its channels enabled,
and pays eight exchanges every millisecond for nothing.

For a channel with neither its interrupt (`CCxIE`) nor its output (`CCxE`)
enabled, `UpdateTimer()` writes `Enabled = false` and the parent's
direction. If the channel's timer is already disabled and already counts in
the parent's direction, both writes put back the entry that is there. The
event runs inside the clock source's update (it is the timer's clock entry
handler), where an exchange only replaces the entry and asks for another
pass, and the auto-reload write just before the loop (`Limit =
autoReloadValue`) has already asked for that pass and for the CPU to
return. So `UpdateTimerOnUpdateEvent()` skips `UpdateTimer()` for such a
channel; every other channel is updated as before.

The direction check matters: in centre-aligned modes the parent's direction
changes at the limit, and the channels must follow it. A channel's timer can
only be enabled with its interrupt or output enabled (`UpdateTimer()`,
`Reset()` and a `CCRx` write of 0 are the only writers), so the `Enabled`
check is a safeguard.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository.

`tooling/bench/stm32-timer.resc` in our measurements (STM32F4's TIM2 at
10 MHz with a 1 MHz update event and no channel in use, a Cortex-M4 at 100
MIPS spinning; about 250,000 update events in 0.25 s of virtual time),
master vs master + this PR, median of 5, interleaved, Linux arm64 in Docker,
4 cores: 3.26 s → 2.60 s of host time (1.26x as fast; fastest runs 3.17 →
2.31 s), allocation 9.0 → 6.5 GB per virtual second (-28 %). Within the
whole series, leaving this PR out makes the same workload 1.34x slower
(1.29 → 1.73 s, median of 7).

On our STM32H733 firmware (FreeRTOS, the HAL timebase on TIM23 at 1 kHz with
no channel enabled), a host profile (`perf`, steady state) put
`STM32_Timer`'s update event at 2.1 % and 2.8 % of all samples on two
boards; with this change, 1.3 % and 1.9 %.

With the whole series (renode/renode#ISSUE_B, plus renode/renode#ISSUE_C's
hook exclusion; master vs master + every PR, median of 5, interleaved): the
bus-performance loop runs 2.76x as fast, a register read 2.24x, a register
write 1.91x, an STM32 timer's update path 2.64x; our STM32H733 systems (one
and two machines, FreeRTOS firmware with HAL timers, CAN, SPI) 15-62 %
faster in steady state.

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master 9b5195e6 with renode-infrastructure master
3e4301cf:

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
- of those, every STM32 platform suite (`STM32F4_Discovery`, `STM32F7`,
  `STM32F072b`, `STM32F103`, `STM32L072`, `STM32W108`, `nucleo_f429zi`,
  `nucleo_f767zi`, `nucleo_h753zi`, `nucleo_wba52cg`, `ramn`, which all use
  `STM32_Timer`): the same results as on master (`ramn`'s two "ECUA Should
  Display" cases fail on both)
