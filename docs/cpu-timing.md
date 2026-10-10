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
  the host emulates per wall second. It matters only to IFS_HIL's suites on
  the virtual bench, and there only to how long they take: they run on the
  bench's time (below).

The two interact in one way only: a lower rate means fewer instructions per
virtual second, so the host emulates faster. The wall-clock bench sets every
core to `WALL_CLOCK_MIPS` (100, `vhil/bench.py`) to keep close to real
time. That gives up fidelity to the rates below on purpose.

The host does not keep real time anyway. On a 4-vCPU CI runner the two-board
`ecu-ams` bench runs at 0.1-0.4x while both bootloaders poll and at 0.4-0.9x
after, and an 8-core M-series host at 0.4-0.9x (#243). IFS_HIL's tests time
the bench with `time.sleep`, `time.monotonic` and CAN frame timestamps, so the
vHIL's pytest plugin puts them on the emulation's virtual time
(`vhil/benchclock.py`, published by `models/renode/VhilClock.cs`): a slow host
makes a suite take longer, not miss a 6 s deadline that covered 0.6 s of the
firmware's boot. Each test's report gives the bench's speed during it, and a
stopped emulation fails the test that waits on it (`BenchStalled`) instead of
timing it out. `--vhil-host-clock` restores the host's clock.

## Where the rate is set

| Where | Value | Used by |
|---|---|---|
| `catalog/firmware/<fw>.yaml` `cpu.mips` | AMS 528, ECU 231 | Native tests and generated scripts (`vhil/system.py` renders `cpu PerformanceInMips` per board) |
| `platforms/cpus/stm32h733.repl` `PerformanceInMips` | 528 | Any image the catalogue gives no rate (one instruction per cycle at SYSCLK) |
| `vhil/bench.py` `WALL_CLOCK_MIPS` | 100 | The wall-clock bench only, to keep real time |
| `scripts/speed.py --mips` | any | Host speed measurement only |

The rate belongs to the firmware, not the platform. It is decided by what the
firmware sets up: the clock tree, the caches, and where code and data live.

## The cycle counter

The DWT cycle counter, CYCCNT (0xE0001004), counts core clock cycles (ARMv7-M
ARM DDI0403E C1.8). In the emulator it counts **SYSCLK cycles of virtual
time**: 528 per virtual us (`dwt` in `platforms/cpus/stm32h733.repl`). Renode's
DWT syncs the CPU's time on every CYCCNT read, so a read is exact to the
instruction. Since each instruction costs 1 / `cpu.mips` us, it adds
528 / `cpu.mips` cycles: 2 at 264 MIPS, 1 at 528.

So a wait timed on CYCCNT lasts what the firmware asks in virtual time,
whatever the rate, as it does on the chip whatever the caches and wait states
do. Only the code around the wait follows the rate. The rate still decides
how many polls the wait takes, but no pin shows that.

- Every MainLite image has CYCCNT running: the CAN bootloader enables it
  (DEMCR.TRCENA, DWT_CTRL.CYCCNTENA; `stm32-can-bootloader` `main.c:489-491`)
  to time its sector erases, and the app inherits it.
- The bootloader writes no DWT_LAR unlock, and on the car's H733 the counter
  runs anyway: bench-01 measured 1822 ms erases with it
  (stm32-can-bootloader#138). So the emulator has no lock. A firmware's
  unlock write is dropped and explained in `configs/peripherals.yaml`.
- Renode's DWT counts while CYCCNTENA is set, regardless of DEMCR.TRCENA.
  Every image sets both.
- 528 MHz is fixed, not derived from the RCC: every image runs SYSCLK at
  528 MHz from the bootloader on. Code that reads CYCCNT before
  `SystemClock_Config` (HSI 64 MHz on the chip) would see it count 8x fast.
- `stm32h7.repl` clocked it at 250 MHz, so CYCCNT-timed waits ran 2.1x long
  (the AMS's isoSPI wake after IFS08-CE-AMS#637: 42.7 us / 1056 us for a
  20 us / 500 us request). `tests/sim/test_cortex_m7_core.py` pins the rate.

## The caches

Renode has no cache. Every fetch and load sees memory, and every instruction
costs the same. Enabling a cache or invalidating it is therefore modelled as
the architected no-op for what code observes
(`models/renode/VhilCaches.cs`):

- CCR.IC and CCR.DC read back as written and clear on every reset (ARMv7-M
  ARM B3.2.8; ST PM0253 4.3.7). tlib dropped them, so CMSIS
  `SCB_EnableICache` (`cachel1_armv7.h:57-70`) saw the cache still off.
- ICIALLU (0xE000EF50) is accepted and counted (ARMv7-M ARM B2.2.7).

What a cache does to speed has to be the firmware's `cpu.mips`, which is why
a firmware that turns its caches on needs its rate redone.
`SCB_EnableDCache` would also read CSSELR and CCSIDR, which Renode leaves
tagged. The guard would fail that run until they are modelled, and no
MainLite firmware enables the D-cache.

## What the chip runs (firmware dev, 2026-10-10)

- **Clocks.** SYSCLK is 528 MHz: HSE 24 MHz / PLLM 2 * PLLN 44 / PLLP 1. AXI
  (HCLK) is 264 MHz, and the flash runs `FLASH_LATENCY_3` at VOS0 (AMS and ECU
  `Core/Src/main.c` `SystemClock_Config`). 550 MHz is the part's maximum; these
  firmwares don't use it.
- **Caches.**
  - AMS: the I-cache is **on** since IFS08-CE-AMS#637 (`SCB_EnableICache` in
    `main()` before `HAL_Init`, `AMS.ioc` `CORTEX_M7.CPU_ICache=Enabled`). The
    D-cache stays off: the AMS's DMA buffers are in AXI SRAM and it has no
    non-cacheable MPU region (its `docs/ARCHITECTURE.md`, "Caches").
  - ECU and CAN bootloader: both caches **off**. Neither calls
    `SCB_EnableICache` or `SCB_EnableDCache`, and `ECU.ioc` has no
    `CORTEX_M7` cache keys.
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

With no I-cache (the ECU, the bootloader), every instruction fetch goes to
that buffer. A loop whose
body spans two flash words reads both words on every iteration, which costs at
least 16 CPU cycles per iteration, however short the body is. ST's headline
figures (2778 CoreMark at 550 MHz, about 1 instruction per cycle or better)
are measured with the L1 cache on, so they don't apply to the cache-off
firmwares. I found no published cache-off figure for the H72x/H73x.

## The rate: a datasheet bound until the chip is measured

Each firmware has a reference window: a CPU-bound stretch of code that holds
a pin, so a logic analyzer can time it on the chip too.

| Firmware | Window | Loop | Instructions in the window (emulator) | Bound |
|---|---|---|---|---|
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

Up to AMS dev 2026-10-09 the AMS had such a window too: PB9 low for the wake
pulse, a `delay_us(20)` NOP loop of 8 instructions over 2 flash words, 24130
instructions, bound 528 * 8/16 = 264 MIPS.

### The AMS: I-cache on, no window left

IFS08-CE-AMS#637 changed two things:

- `delay_us` is timed on CYCCNT, so the wake pulse lasts 20 us whatever the
  rate ("The cycle counter" above). It was the AMS's only pass-counting delay
  (the PR's own audit). Every other pin edge brackets peripheral-timed work,
  which Renode doesn't time the way the chip does: SPI transfers to the
  LTC6820, relay and SDC writes.
- With the I-cache on, a loop that fits in the 32 KB cache no longer pays the
  flash read buffer. The 264 bound is gone.

So no pin times the AMS's CPU rate any more, and the rate is pinned on the
pipeline instead:

- **Upper bound, 1056 MIPS.** On I-cache hits the Cortex-M7 fetches 64 bits a
  cycle and its in-order superscalar pipeline issues up to two instructions a
  cycle (Arm Cortex-M7 Processor Technical Reference Manual, DDI 0489,
  "About the processor" and "Prefetch Unit"): 2 * 528.
- **Rate used, 528 MIPS: one instruction a cycle.** Dual issue is limited to
  certain pairs, a mispredicted branch costs the pipeline's refill, and the
  AMS's code is branchy control code with loads from peripherals over AXI and
  AHB, which stall for many cycles. Its data and stack are in DTCM, so its
  own loads and stores don't stall. Half the dual-issue peak is the
  documented middle until the chip is measured. Expect the chip within a
  factor of 2 either way, closer to 528 for most code.

The AMS's PB9 wake train is still worth timing on the chip: 20 us low and
500 us high per IC is the firmware's intent (LTC6811 "Waking a Daisy Chain,
Method 2"), and it checks the cycle-counter model against the silicon. It
doesn't measure the rate.

### Measuring the chip (no firmware change, invariant 1)

Any MainLite running the same image will do: on the car, or bench-01's. CPU
timing depends only on the silicon, the clock tree and the image, none of
which the bench changes (invariant 9 concerns peripherals and bench artefacts,
which these windows don't touch).

**Logic analyzer, at 10 MS/s or more.**

- AMS (since IFS08-CE-AMS#637): probe PB9 from power-on. Record the ten
  CS-low pulses of the boot wake train and the nine CS-high gaps between them.
  Expect 20 us low and 500 us high. That checks the cycle counter, not the
  rate.
- ECU: probe PA5 while TelemetryTask sends, every 200 ms. Record the SCK high
  half-periods.
- Report the minimum and the median over the run, and the firmware commit. The
  minimum filters out preemption.

**SWD, if no analyzer is available.** Use openocd on bench-01:

1. Set DEMCR.TRCENA and DWT_CTRL.CYCCNTENA.
2. Break at the loop's entry and exit addresses, taken from the ELF.
3. Read DWT_CYCCNT at each break, about 20 times, and take the minimum.
4. The rate is 528 times the window's instructions over the cycles.

For the AMS, which has no pin window, SWD is the way to measure its rate. Use
a CPU-bound function such as `ams::crc::update`, the SD log's nibble-table
CRC over a block. Count its instructions for the same call in the emulator,
from the CPU's executed-instruction count at its entry and at its return, then
apply step 4.

To record a measurement:

1. Put it in the test as the window's `chip_us`.
2. Set `cpu.mips` to the window's instructions divided by `chip_us`.
3. Note the firmware commit and the instrument in the firmware entry.

The skipped `..._is_as_long_as_on_the_chip` test then holds the rate to the
measurement. For the AMS's wake train, put the chip's widths in
`CHIP_WAKE_US` instead: the test then holds the cycle counter to them.

## What pins it

`tests/sim/test_ecu_cpu_timing.py` (shared helpers in
`tests/sim/cpu_timing.py`) checks four things:

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

`tests/sim/test_ams_cpu_timing.py` checks that the AMS's core runs at its
catalogue rate, and that its wake train is 20 us low and 500 us high per IC
in virtual time, to the instruction. Once isc-fs/IFS_HIL#154 reports the
chip's widths, it also holds the train to them.
`tests/sim/test_cortex_m7_core.py` holds CYCCNT at 528 cycles per virtual us.

## Limitations

- **One rate per core.** Renode 1.17 charges every instruction the same. It
  can't cost a flash fetch differently from a TCM fetch, a load from a store,
  or a branch from straight-line code. The reference windows are busy-wait
  loops, so the rate is right for busy-waits, which is where these firmwares
  time things by running code. Branch-light, straight-line code (a Kalman
  step) can run faster on the chip than the loop rate implies, because
  sequential fetches are served from the read buffer. Expect a 10 % tolerance
  on the windows and less fidelity elsewhere.
- **The bootloader** runs on the same core at its application's rate. The
  bootloader keeps its caches off, so behind the AMS it runs about twice as
  fast as on the chip. It times nothing by running code: its waits use the
  HAL tick and CYCCNT.
- **A firmware that turns the caches on**, or moves code to ITCM, changes its
  rate a lot. Its catalogue entry must then be redone, as the AMS's was for
  IFS08-CE-AMS#637.
- **The uDV** has no catalogue firmware yet, so it gets the platform's 528.
