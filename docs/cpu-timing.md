# CPU timing: how fast the emulated core runs firmware code

Renode charges every instruction the same virtual time: 1 / `PerformanceInMips`
us. Timer-driven behaviour (the FreeRTOS tick, TIM23, peripheral timing) does
not depend on that rate. Everything the firmware times by running code does:

- busy-wait loops, such as the AMS's LTC6811 wake pulse or the ECU's bit-banged
  nRF24;
- HAL timeouts that spin;
- how long an ISR or a task step takes, and so how much CPU a task uses and
  how late the lower-priority tasks run.

If the rate is wrong, all of that is wrong in virtual time.

Tracked in #246.

## Not the same as emulation speed (#154)

There are two different speeds, and they are easy to mix up:

- **This document: virtual-time fidelity.** How much *virtual* time a piece of
  firmware code takes. It is set by the rate above and affects native tests
  (`vhil.sim`), which run entirely in virtual time.
- **#154 and `scripts/speed.py`: wall-clock speed.** How many virtual seconds
  the host emulates per wall second. It matters only to IFS_HIL's wall-clock
  suites on the virtual bench.

The two interact in one way only: a lower rate means fewer instructions per
virtual second, so the host emulates faster. The wall-clock bench sets every
core to `WALL_CLOCK_MIPS` (100, `vhil/bench.py`) so that it can keep real
time. That gives up fidelity to the rates below on purpose.

## Where the rate is set

| Where | Value | Used by |
|---|---|---|
| `catalog/firmware/<fw>.yaml` `cpu.mips` | AMS 264, ECU 231 | Native tests and generated scripts (`vhil/system.py` renders `cpu PerformanceInMips` per board) |
| `platforms/cpus/stm32h733.repl` `PerformanceInMips` | 528 | Any image the catalogue gives no rate (one instruction per cycle at SYSCLK) |
| `vhil/bench.py` `WALL_CLOCK_MIPS` | 100 | The wall-clock bench only, to keep real time |
| `scripts/speed.py --mips` | any | Host speed measurement only |

The rate belongs to the firmware, not the platform. It is decided by what the
firmware sets up: the clock tree, the caches, and where code and data live.

## What the chip runs (firmware dev, 2026-10-09)

- **Clocks.** SYSCLK is 528 MHz: HSE 24 MHz / PLLM 2 * PLLN 44 / PLLP 1. AXI
  (HCLK) is 264 MHz, and the flash runs `FLASH_LATENCY_3` at VOS0 (AMS and ECU
  `Core/Src/main.c` `SystemClock_Config`). 550 MHz is the part's maximum; these
  firmwares don't use it.
- **Caches.** The I-cache and D-cache are **off** in the AMS, the ECU and the
  CAN bootloader. None of them calls `SCB_EnableICache` or `SCB_EnableDCache`,
  and `AMS.ioc` and `ECU.ioc` have no `CORTEX_M7` cache keys.
- **Code** runs from embedded flash (`0x08020000`) over the 64-bit AXI bus.
  ITCM is unused.
- **Data.**
  - AMS: `.data`, `.bss` and the stack are in DTCM, with no wait states
    (`STM32H733XG_FLASH.ld`).
  - ECU: `.data`, `.bss` and the stack are in RAM_D1, which is AXI SRAM and
    uncached here (`STM32H733ZGTX_FLASH.ld`). The ECU image is also built
    without optimisation (its code is frame-pointer, load/store heavy).
- **Flash reads** (RM0468 Rev 3 §4.3.8, p. 159-161, and Table 16). There is one
  256-bit read buffer and a 3-deep command queue, and the queue runs one read at
  a time. Accesses that fall within the buffered flash word are served from the
  buffer; any other word needs a new read. At 3 WS a read takes 4 flash clocks
  at the AXI's 264 MHz, which is 8 CPU cycles.

With no I-cache, every instruction fetch goes to that buffer. A loop whose
body spans two flash words reads both words on every iteration, which costs at
least 16 CPU cycles per iteration, however short the body is. ST's headline
figures (2778 CoreMark at 550 MHz, about 1 instruction per cycle or better)
are measured with the L1 cache on, so they don't apply to these firmwares. I
found no published cache-off figure for the H72x/H73x.

## The rate: a datasheet bound until the chip is measured

Each firmware has a reference window: a CPU-bound stretch of code that holds
a pin, so a logic analyzer can time it on the chip too.

| Firmware | Window | Loop | Instructions in the window (emulator) | Bound |
|---|---|---|---|---|
| AMS | LTC6820 CS (PB9) low for the wake pulse, `delay_us(20)` (`ltc6820.cpp` `Bus::wakeup`), 10 pulses at boot | 8 instructions over 2 flash words, 3000 iterations | 24130 | 528 * 8/16 = **264 MIPS** |
| ECU | nRF24 SCK (PA5) high half-period, `NRF24_BitBangDelay` (`nrf24.c:359-365`) | 7 instructions over 2 flash words, 400 iterations | 2875 | 528 * 7/16 = **231 MIPS** |

How the bound is worked out:

1. Disassemble the loop (`arm-none-eabi-objdump -d -C`) to count its
   instructions and the 32-byte flash words its taken path touches.
2. Each word costs at least 8 CPU cycles per iteration, so the cycles per
   iteration are at least 8 times the number of words.
3. The rate can be no more than 528 times instructions over cycles.

The bound is an upper bound. It counts only the flash reads, not the AXI
interconnect latency or the ECU's stack loads and stores to uncached AXI
SRAM. So the chip runs these loops at that rate or slower, and 528 (one
instruction per cycle) made them at least twice as fast as on the chip.

The catalogue uses the bound until the chip's own number replaces it:
**rate = instructions in the window / the window measured on the chip (us)**.

### Measuring the chip (no firmware change, invariant 1)

Any MainLite running the same image will do: on the car, or bench-01's. CPU
timing depends only on the silicon, the clock tree and the image, none of
which the bench changes (invariant 9 concerns peripherals and bench artefacts,
which these windows don't touch).

**Logic analyzer, at 10 MS/s or more.**

- AMS: probe PB9 from power-on. Record the ten CS-low pulses of the boot wake
  train, which are about 50 us apart.
- ECU: probe PA5 while TelemetryTask sends, every 200 ms. Record the SCK high
  half-periods.
- Report the minimum and the median over the run, and the firmware commit. The
  minimum filters out preemption.

**SWD, if no analyzer is available.** Use openocd on bench-01:

1. Set DEMCR.TRCENA and DWT_CTRL.CYCCNTENA.
2. Break at the loop's entry and exit addresses, taken from the ELF.
3. Read DWT_CYCCNT at each break, about 20 times, and take the minimum.
4. The rate is 528 times the window's instructions over the cycles.

To record a measurement:

1. Put it in the test as the window's `chip_us`.
2. Set `cpu.mips` to the window's instructions divided by `chip_us`.
3. Note the firmware commit and the instrument in the firmware entry.

The skipped `..._is_as_long_as_on_the_chip` test then holds the rate to the
measurement.

## What pins it

`tests/sim/test_ams_cpu_timing.py` and `tests/sim/test_ecu_cpu_timing.py`
(shared helpers in `tests/sim/cpu_timing.py`) check four things:

- **The board's core runs at its firmware's catalogue rate.**
- **The window still costs the pinned instructions, within 3 %.** If the
  busy-wait or its build changes, so do the bound and the chip's number, and
  this test fails and says to redo them.
- **Virtual time between the window's edges is the instructions over the
  rate, within one sync quantum.** This checks that Renode charges what the
  rate says.
- **The window matches the chip's width within 10 %**, once the chip is
  measured. Until then this test is skipped and points at #246.

GPIO edges carry the CPU's executed-instruction count at the write
(`models/renode/VhilProbe.cs`, `Edge.instructions`). It is exact, whereas an
edge's time is only as fine as the sync quantum (500 us in these systems).
That is what makes a 20 us window measurable in the emulator.

## Limitations

- **One rate per core.** Renode 1.17 charges every instruction the same. It
  can't cost a flash fetch differently from a TCM fetch, a load from a store,
  or a branch from straight-line code. The reference windows are busy-wait
  loops, so the rate is right for busy-waits, which is where these firmwares
  time things by running code. Branch-light, straight-line code (a Kalman
  step) can run faster on the chip than the loop rate implies, because
  sequential fetches are served from the read buffer. Expect a 10 % tolerance
  on the windows and less fidelity elsewhere.
- **The bootloader** runs on the same core at its application's rate.
- **A firmware that turns the caches on**, or moves code to ITCM, changes its
  rate a lot. Its catalogue entry must then be measured again.
- **The uDV** has no catalogue firmware yet, so it gets the platform's 528.
