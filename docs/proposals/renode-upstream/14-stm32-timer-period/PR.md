[FIX] STM32_Timer: Count ARR + 1 values per update period

### Related issue

None filed: the bug and its fix are small enough to describe here. One Robot expectation in renode/renode encodes the old period and changes with it (below).

### Description

Bug fix in `STM32_Timer`: the update event comes one count early.

On the chip the counter counts from 0 up to the auto-reload value and then
overflows to 0, or counts down from ARR to 0 and then underflows to ARR:
ARR + 1 values per period. The update period is therefore
(ARR + 1) * (PSC + 1) timer clocks. RM0468 (STM32H72x/73x) 44.3.2 "Counter
modes" gives the counting, and 44.4.14 gives the prescaler,
CK_CNT = fCK_PSC / (PSC + 1). RM0090 18.3.2 says the same for the F4.
STM32Cube's HAL timebase follows it: `Period = 1000000 / 1000 - 1` at a
1 MHz counter clock.

`STM32_Timer` uses ARR itself as its clock entry's limit (`Limit =
autoReloadValue`), so the update event comes every ARR counts. Every
period derived from a timer runs fast by 1 / (ARR + 1):

- a HAL timebase with ARR = 999 ticks every 999 us, so `HAL_GetTick()`
  gains 0.1 %;
- a 400 Hz sampling timer with ARR = 2499 fires every 2.499 ms;
- `TIMx_CNT` never reads ARR;
- PWM mode 1 has a period of ARR counts, not ARR + 1.

We found it on an STM32H733 firmware whose IMU frames came every 19.992 ms
instead of 20 ms and drifted off their grid.

The fix sets the entry's limit to ARR + 1 everywhere it is set: the
constructor, `Reset`, an ARR write without preload, and the update event's
preload transfer. Counting up, the entry's value is still the counter, so
CNT reads 0..ARR. Counting down, a descending entry fires when its value
reaches 0 and reloads its limit, so its value is CNT + 1. CNT reads and
writes, UG's reload to ARR and a DIR change go through a private `Counter`
property that maps the two. A DIR write that keeps the direction does what
it did before. Centre-aligned counting, which `STM32_Timer` does not model,
is unchanged.

The public `Limit` (from `LimitTimer`) now reads ARR + 1. It is the clock
entry's limit, not a register.

### Usage example

`STM32_TimerTests` (PeripheralsTests) has four tests. They drive the timer at
1 MHz through its registers and advance the clock source by microseconds.
All four fail on master and pass with this change:

- `ShouldRaiseUpdateEventEveryArrPlusOneCounts`: with ARR = 99, after 99 us
  CNT = 99 and UIF is clear. After 100 us CNT = 0 and UIF is set, and the
  same holds for the next three periods. On master: CNT = 0 and UIF is set
  at 99 us.
- `ShouldDivideTheCounterClockByPrescalerPlusOne`: with PSC = 1 and
  ARR = 49, the period is 100 us.
- `ShouldUnderflowAfterZeroWhenCountingDown`: UG loads ARR, CNT is 0 at
  99 us and UIF is clear; the underflow back to ARR comes at 100 us. On
  master it comes at 99 us.
- `ShouldKeepAPwmPeriodOfArrPlusOneCounts`: PWM mode 1 with CCR1 = 25 and
  ARR = 99. The output is high for 25 us of every 100 us.

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`,
Linux arm64), renode master 8c5a2440 with renode-infrastructure master
3e4301cf:

- headless portable build (`./build.sh --no-gui`): succeeded
- `PeripheralsTests`: 117 passed, 5 skipped (master: 113 passed, 4
  failed, 5 skipped; the 4 failures are this PR's new tests)
- `UnitTests`: 621 passed, 6 skipped, the same as master
- the Robot suites of every STM32 platform that uses `STM32_Timer`, plus the
  timer tests (`STM32F4_Discovery`, including its up- and down-counting
  update event tests, `STM32F4_Tock`, `STM32F7`, `STM32F072b`, `STM32F103`,
  `STM32L072`, `STM32W108`, `nucleo_f429zi`, `nucleo_f767zi`,
  `nucleo_h753zi`, `nucleo_wba52cg`, `timer-reconfiguration`,
  `virtual-time`): all pass, as on master. One expectation changes:
  `nucleo_wba52cg`'s "Timer Should Support Input Capture" encoded the old
  period. The PWM it measures now has the chip's period, so IC2 reads
  50000 and 33333 instead of 49999 and 33332, and the second duty cycle,
  computed by the firmware as IC1 * 100 / IC2 in integers, reads 49 %
  instead of 50 %. That change is a renode/renode commit, ready to land
  with the submodule bump: as a PR of its own, or for you to fold in.
- `dotnet format whitespace --verify-no-changes` on the changed files:
  passed

On our STM32H733 boards the fix moves timing only. Every CAN frame and GPIO
edge is the same, in the same order. The HAL tick is now 1000 us, and the
IMU frame is on a 20 ms grid within 1 us, where it used to drift 722 us
over 91 frames.
