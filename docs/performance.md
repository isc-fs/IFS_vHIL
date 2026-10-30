# Emulation speed (#154)

How many virtual seconds the host emulates per wall second: the real-time
factor. It is not the virtual-time fidelity of [`cpu-timing.md`](cpu-timing.md):
nothing a test asserts depends on it. IFS_HIL's suites run on the bench's own
time since #253 ([`vhil/benchclock.py`](../vhil/benchclock.py)), and native
tests (`vhil.sim`) run entirely in virtual time, so a slow host makes a run
take longer, not fail. The wall-clock bench paces itself to 1x at most
(`models/renode/VhilPacer.cs`); above 1x it waits, below it runs flat out.

## Measuring it

```sh
VHIL_DOCKER_CPUS=4 scripts/vhil-docker.sh speed --baseline   # results/speed/speed.json
python scripts/speed.py ecu=ECU08.elf ams=AMS.elf bootloader=CAN_BL.elf \
    [--systems ams ecu ecu-ams ecu-ams-ecu] [--paths bench sim] [--boot 3] [--steady 5] \
    [--baseline] [--repeat N] [--threads] [--profile DIR] [--setup "vhil_ff_{board} Enabled false"]
```

A speed change must not change what a run computes: the identity
fingerprint ([`fingerprint.py`](proposals/renode-patches/tooling/fingerprint.py),
in the image with the firmware from `VHIL_ECU_ELF`, `VHIL_AMS_ELF`,
`VHIL_UDV_ELF` and `VHIL_CAN_BOOTLOADER_ELF`: `python
docs/proposals/renode-patches/tooling/fingerprint.py OUT.json 10000`)
records what a lock-step run of ecu, ams, ecu-ams, udv, ecu-ams-udv and
ecu-ams-ecu observes over 10 s from power-on (every bus's timeline, GPIO
edges with instruction counts, radio payloads, each CPU's executed
instructions, PC, state and RAM digests, the HAL tick) and a SHA-256 of it
per system; a pure speed change leaves the file byte-identical. On dev
0b7449a (patch set vhil.6, firmware at the catalogue's refs, 2026-10-15):
ecu `f66bae9a`, ams `e34419b8`, ecu-ams `8afef3de`, udv `f61019b8`,
ecu-ams-udv `a329f2fb`, ecu-ams-ecu `4718c51b` (dev d371c84 too).

Since #320 (the AMS carrier's measured ADC3 transfer on the pack-current
pair, not a speed change; same patch set and firmware, 2026-10-20): ecu
`f66bae9a`, ams `f46357e7`, ecu-ams `26c157b3`, udv `f61019b8`, ecu-ams-udv
`5f05bd19`, ecu-ams-ecu `0bfcdbf3`. What moved, only in systems with the
AMS: the pack current it reads at rest, -2102 mA -> 0, so 0x135's accu
(0xFFEB -> 0), 0x4A1's current (-2102 -> 0) and the SoC its coulomb count
integrates (0x130, 54 -> 55 %) on can_acu, the ECU's relay of it on
can_dash (0x518) and in its nRF24 payloads; then, from those frames'
different bit stuffing, every later frame's timing by a few us, and the AMS's
and ECU's instruction counts, PB9 / ECU edges (by a few us, same count),
state and RAM digests. Frame counts, edge counts and the ecu and udv
fingerprints are unchanged.

Since #327 (the car's measured ADC3 noise on the same pair, seed 1, not a
speed change; same patch set, firmware AMS dev 94b59a9, ECU dev 44610a5, uDV
dev 1252022, CAN_BL v1.7.0; 2026-10-20): ecu `f66bae9a`, ams `79f9b368`,
ecu-ams `ca19a9e9`, udv `f61019b8`, ecu-ams-udv `82f40a77`, ecu-ams-ecu
`5a060988`; two runs byte-identical. What moved, only in systems with the
AMS: the pack current it reads at rest, 0 -> a few tens of mA either side,
so 0x135's accu (0 -> -2..+1 dA, 32 of its frames in ams) and 0x4A1's
current (0 -> -94..+35 mA, 15 frames) on can_acu, and the ECU's relay of it
on can_dash (0x518, 9 frames) and in its nRF24 payloads (11 of 200); then,
from those frames' bit stuffing, later frames' timing by a few us, the AMS's
and ECU's instruction counts, PB9 / ECU edge instruction counts and times,
state and RAM digests. SoC (0x130), frame counts, edge counts and the ecu and
udv fingerprints are unchanged.

Since #326 (the ECU's and uDV's datasheet-derived single-ended ADC3 noise,
seed 1, not a speed change; same patch set and firmware; 2026-10-22), the
CI guard's 5 s references (`ci/fingerprints.json`): ams unchanged
(`89b3ad31`), ecu `3d2bdfad`, ecu-ams `1d51b853`, udv `d7b56357`,
ecu-ams-udv `6084d96c`, ecu-ams-ecu `2d30a9e5`; two runs byte-identical.
What moved, in every system with an ECU or a uDV: the codes they read, so
their RAM and state digests and what they send from them (the ECU's nRF24
payloads carry the raw pedal codes, IFS08-CE-ECU `radio_snapshot.hpp:43-45`,
hence ecu's watched SPI edges 54001 -> 54009). Frame counts and the other
edge counts are unchanged.

`ecu-ams-ecu` is a benchmark, not a car system
([`scripts/speed-systems/`](../scripts/speed-systems/ecu-ams-ecu.yaml), named
in `speed.py`'s `BENCH_ONLY`, so the catalogue, the editor and the scenarios
never see it): ecu-ams plus a third MainLite running the ECU's application on
buses of its own, to measure how the cost grows from one board to two to
three before the uDV has a firmware. `--threads` splits each phase's host CPU
by thread: each board's CPU thread, the master time source's loop (on the
monitor's thread), the slave time sources of a lock-step multi-board run, and
the JIT.

[`scripts/speed.py`](../scripts/speed.py) boots each system from power-on
and measures two phases: **boot**, the first 3 s of virtual time (the
bootloaders' 2 s auto-jump window, invariant 5, then the apps' start), and
**steady**, the 5 s after it. Each runs on two paths:

- **bench**: the wall-clock bench's emulation (`vhil/bench.py`): Renode's
  shared time source (`mach create`), every core at `WALL_CLOCK_MIPS` (100),
  the bench clock and GPIO probes, without the pacer, so the factor is the
  host's headroom. Each bus the bench bridges to SocketCAN (`host_netdev`)
  gets a passive node that acknowledges in place of the bridge (#260): before,
  a board alone on such a bus retried every frame with an ACK error, a load
  the bench never has (half the ECU's bench cost was its bus model deciding
  those retries);
- **sim**: lock-step `vhil.sim` as native tests run it: the catalogue's
  `cpu.mips` (ECU 231, AMS 528), a time source per board (invariant 11), a CAN
  probe on every bus.

Each phase also records the host CPU seconds Renode's process used per
virtual second (all its threads, from `/proc`): what a run costs a host that
runs several at once (throughput), and steadier than the factor on a host
that is busy with something else.

`--baseline` runs each configuration a second time as the bench ran before
#154's speed-ups, on the same host (`VHIL_FAST_FORWARD=0`, .NET's default
tiering delay and call-count threshold). `VHIL_DOCKER_CPUS=N` limits the
container to N cores, as a CI runner (GitHub's have 4). The `emulation speed` job of
[`sim.yml`](../.github/workflows/sim.yml) runs it with `--baseline` when a run
selects it (`full-ci-all`, every night, or a `full-ci` change to the speed
paths, [`ci.md`](ci.md)), into the job summary and an artifact.

On an M-series host under Colima, run to run variation is ±20 %: the VM's
vCPUs land on performance or efficiency cores as macOS schedules them. Take
the median of a few (`--repeat`), or CI's numbers.

## Where the host's time goes

Profiled with host `perf` on Renode's CPU threads (JIT symbols from .NET's
perf map) and by sampling each board's PC on host time
(`speed.py --profile`, `models/renode/VhilPcSampler.cs`):

- **Managed code, not translated code.** On a board's CPU thread, the code
  tlib generated from the firmware is 3-6 % of the samples and tlib's helpers
  5-12 %. The rest is .NET: the CLR's transitions between tlib and C# (every
  MMIO access is a call from native code into C#, every CPU register read a
  call the other way), the peripheral models, and Renode's time framework
  (`BaseClockSource.Update` and the clock-entry list on every chunk). So code
  costs the host by its peripheral accesses and timer events, not by its
  instruction count.
- **The bootloader's polling loop, every boot.** For its 2 s window the CAN
  bootloader's main loop (`stm32-can-bootloader` `main.c`
  `Bootloader_MainLoop`) polls each of the three FDCANs' RXF0S and PSR,
  clears a `FDCAN_ProtocolStatusTypeDef` byte by byte three times, and
  reloads the IWDG: 849 instructions and 7 MMIO accesses an iteration, about
  half a billion instructions per boot at the ECU's 231 MIPS, a billion at the
  AMS's 528. That is why a boot ran at 0.1-0.5x.
- **The ECU's bit-banged nRF24.** Each half bit waits in
  `NRF24_BitBangDelay`, a 400-iteration loop on a volatile counter (2800
  instructions): a third of the ECU's instructions in steady state, plus a GPIO
  write into the C# GPIO and radio models per edge. (Host-time PC sampling
  credited the loop with half the host time, #243; skipping it showed its
  real share is far less: the GPIO writes and the time framework around it
  cost more.)
- **Two boards.** The boards meet at every 500 us sync point (on both paths),
  so each quantum takes as long as the slower board's share of it. Their
  busy stretches (the AMS's LTC6811 chain reads and SD writes, the ECU's radio
  bit-bang) rarely fall in the same quantum, so ecu-ams runs at about the sum
  of the two boards' costs, not the larger: each CPU thread is busy about half
  the time, and while it waits for the other it spins in `SemaphoreSlim.Wait`
  (`TimeHandle.RequestTimeInterval`), a quarter of its samples. The master
  time source's loop takes about an eighth of a core.
- **A fresh Renode.** Every native test starts one. .NET first JITs Renode's
  hot paths unoptimised and, by default, holds their promotion back while new
  methods keep being called: the first boot ran on tier-0 code.
- **Starting a Renode** (#260): every native test starts one, and the
  generated script compiled each C# model with its own `include`. Renode's
  `AdHocCompiler` references every loaded assembly in each compilation, so
  each file took 0.15-0.2 s and ~120 MB the process kept: 3 s and a 1.66 GB
  peak per start. The script now compiles every model, and `vhil.sim`'s
  probes, in one `include` (Renode compiles several `.cs` files into one
  assembly), and the monitor connection polls every 50 ms instead of 500:
  a start (process to ready to run) went from 5.4 to 3.4 s, and the peak RSS
  from 1.66 to 0.6 GB, which is what bounds how many runs a host holds.
  That one compilation was still 1.4 s and kept Roslyn loaded: every model
  is now one assembly compiled once per model change (`vhil/models.py`,
  below), and Renode's own assemblies are ReadyToRun.
- **On an aarch64 host** (Apple silicon) tlib's TCG backend calls its softmmu
  helpers for every guest load and store (`tcg/aarch64/tcg-target.c`
  `tcg_out_qemu_st`/`_ld`: no inline TLB look-up), where x86 has a fast path.
  Memory-heavy code costs more there than on CI.

Tried, with no measurable effect beyond the host's ±20 %: `cpu
SyncPCEveryInstructionDisabled` (tlib's per-instruction PC store), dropping
the default map's XN window (`VhilExecuteNever.cs`, #242), `DOTNET_gcServer`,
`DOTNET_gcConcurrent=0` and a 64 MB `DOTNET_GCgen0size`; worse:
`DOTNET_TieredPGO=0` (steady state) and `DOTNET_TieredCompilation=0`. The
other instrumentation (the bench clock `VhilClock.cs` once a sync point, the
probes' hooks only on the GPIO ports a test watches or drives, `VhilBusHooks`
on the RCC and NVIC, the peripheral guard, which reads Renode's log) is under
half a percent of any profile. Block chaining and the block cache are on, and
`MaximumBlockSize` is tlib's 10000 already. Serial execution of the two
boards' quanta on one thread was 10x slower on the bench path. The sync
quantum (500 us) is left alone: it decides when boards and the bus meet (what
a firmware sees of another can lag by up to one quantum, `can-bus.md`), so it
is part of what the emulation computes, not a host setting.

Idle is already cheap: both firmwares sleep in their idle task (AMS#626,
ECU#261), and a CPU in WFI makes Renode report the time to the next event at
once (`BaseCPU`, `ExecutionResult.WaitingForInterrupt`, with
`AdvanceImmediately` on both paths), at no cost per instruction.

## Busy-wait fast-forward

[`models/renode/VhilFastForward.cs`](../models/renode/VhilFastForward.cs),
one per board (`vhil_ff_<board>`, created by the platform's setup in
`catalog/platforms/stm32h733.yaml`), recognises these kinds of busy-wait and
charges their instructions instead of executing them:

- **Counter loops**: the `-O0` volatile counter delay of
  `NRF24_BitBangDelay` (`nop; ldr; adds #1; str; ldr; cmp.w #N; bcc`). A
  pre-opcode hook on CMP.W (immediate) decodes the code around it once per
  PC, exactly that shape or nothing, and checks its bytes again on every use.
  m iterations later the counter (register and memory) is i + m and the flags
  are those of the last ADDS; nothing else changed. A CMP.W that is not a
  delay loop's stays so until a reset, so tlib stops calling the hook there
  (docker/renode's patch 0005 and tlib/0001: `ExcludePreOpcodeExecutionHookAt`,
  the blocks holding it invalidated at the next sync point; every exclusion
  dropped at a reset): the hook reaches C# from the delay loops only. Over a
  3 s boot and 5 s of steady state, the AMS (no delay loops) called it
  259,000 times before and 472 after; the ECU 278,000 and 186,000 (its
  157,000 skips and the delay loops' last iterations).
- **Fixed-point polling loops**: the bootloader's main loop. Armed once a
  quantum, Renode's memory-access hook records the MMIO accesses (Thread mode)
  between two passes through the same access. Two consecutive iterations with
  the same accesses and values, every one of them to a whitelisted register
  (FDCAN CCCR, ECR, PSR, IR, RXF0S, RXF1S, TXFQS, TXBRP: they change only when
  a frame, a transmission's end or the firmware changes them; IWDG KR = 0xAAAA
  with the window off), the same instruction count and no exception or WFI,
  that leave tlib's whole architectural CPU state and every RAM byte as they
  found them, and a third pass through the first access that reads what the
  other two did, are a fixed point: the next iterations repeat them until an
  event. Comparing every RAM byte means copying and comparing all the RAM the
  board has touched (330 KB on the uDV) for each proof; an iteration whose
  every possible path writes no memory (`StoreFreeLoop`, #304: each path of
  exactly its instruction count from the boundary access back to it, decoded
  from the code, following BL into the callee; no store, no instruction
  whose successor the code does not give) leaves RAM as it was by
  construction, so only the CPU's registers are compared. Its verdict is
  kept per (PC, k) with the code it read, checked again from memory on every
  use.
- **Self-yielding polls** (#297): an iteration may pend a PendSV itself, a
  FreeRTOS `taskYIELD` in the loop, as the uDV's micro-ROS transport read
  does on every pass while no agent answers (`usb_cdc_transport.c`
  `cubemx_transport_read`, IFS09-DV-uDV#17). SCB ICSR is whitelisted for it
  (reads, and a write of PENDSVSET alone); every exception entry in the
  iteration must be the PendSV the NVIC acknowledges, as many as the
  iteration pends, ICSR must read the same at both ends (read at a boundary
  only once an attempt has seen a yielding iteration, #307), and the PendSV
  handler's MMIO accesses join the iteration's trace. CPU state and RAM, the
  handler's stack frames included, are compared as for any iteration.
- **Time-bounded polls** (#260): a poll on a register computed from the time
  when read, whose model knows until when it stands (`VhilStableUntil`): ADC3,
  up to its next result, so `HAL_ADC_PollForConversion` (the AMS's
  `read_leg_q4`, 80 us a call). The read joins the whitelist above and a skip
  ends before that time, every skipped read before it with a margin for
  Renode's instruction-to-time rounding (time advances in steps of
  `mips / gcd(mips, ticks per us)` instructions: 231 on the ECU, 66 on the
  AMS). The model's `VhilOnRead` cue arms the detector when the poll starts;
  it asks the model first whether anything is under way, before any call
  into tlib (#306), since most ADC3 reads come while it is idle (the uDV
  configures, starts and stops it around each of its six single conversions
  a millisecond), and ADC3 syncs the CPU's time only while a conversion runs
  (#299).
- **Cycle-counter waits**: a wait on DWT CYCCNT
  (`while ((DWT->CYCCNT - start) < cycles) {}`, the AMS's isoSPI wake,
  `ltc6820.cpp` `delay_us`: `ldr; subs; cmp; bcc`). CYCCNT is Renode's DWT
  timer on the machine's clock, so each read returns a non-decreasing
  function of the time, and every read before the count reaches
  `start + cycles` sees the loop go on. A pre-opcode hook on BCC (excluded at
  every BCC that is not such a loop's, as the CMP.W one) charges, at the BCC,
  the iterations whose reads all come before that time, from the count at the
  time synced there (at least what the last read saw) and with the
  time-bounded polls' margin. The registers the skipped iterations would
  have written last (the read value, the difference, the flags) are
  overwritten by the next iteration before anything reads them.
- **Tick waits** (#301): `HAL_Delay`'s `while ((HAL_GetTick() - tickstart) <
  wait) {}` as GCC emits it at -Og/-O2 (BL to a `HAL_GetTick` that loads the
  tick from RAM, SUBS, CMP, BCC; the uDV's BMI088 and I2C bring-up, 140 ms of
  its boot), decoded at its BCC through the CYCCNT waits' hook. The loop makes
  no peripheral access (the polling detector needs one) and writes no
  memory, only registers it recomputes from registers it never writes and
  the tick, which only an interrupt changes. Two arrivals at the BCC one
  iteration apart (seven instructions, no exception, the same chunk) with
  those registers equal and the branch taken are a fixed point; the
  iterations up to the chunk's end less two are charged. Never on a page a
  lazily delivering peripheral owns (`VhilLazy.IoPages`).

Exact by construction:

- Renode runs a CPU in chunks that end at the nearest clock entry or the end
  of the quantum; events fire, interrupts are raised and sync-point actions
  (frames from the bus model) run between chunks, and tlib stops exactly at a
  chunk's instruction count. A skip never crosses the end of its chunk, so
  every event fires at the instruction it would have.
- The skipped instructions go into tlib's own executed-instruction counters,
  so virtual time, `cpu ExecutedInstructions` (GPIO edge counts, the radio's
  clock), CYCCNT and the chunk's end are what they would have been. tlib keeps
  those counters in its CPUState; their offsets are found at run time by
  value and confirmed before the first skip, and a Renode whose layout
  differs turns the fast-forward off and logs why.
- A polling skip leaves at least one real iteration before the chunk ends, so
  a repeated time-dependent write (the IWDG reload) happens last at the same
  instruction.
- Not accelerated: anything else, including a poll on a register whose value
  is computed from the time when read and whose model cannot say until when
  it stands (SysTick, a timer's CNT, CYCCNT outside the wait above) or on a
  GPIO a device model drives.

## ADC3's DMA results, handed over lazily

The AMS samples its pack current through ADC3 and DMA1 at 12.5 kHz
(`current_task.cpp`), and `Stm32H7Adc3.cs` landed each result with a clock
event of its own: Renode ends a chunk of execution there (in WFI as well), runs
the time framework, the action and the DMA's transfer, and schedules the next.
That was a fifth of the AMS's host time in steady state. Between two of the
stream's flags (half and full transfer) a result changes only what someone
must look at to see: ADC3's registers, the stream's (NDTR, the flags) and the
RAM it writes. So, when ADC3's own interrupts can't be raised by a result and
the stream takes every request (`TryPlanAhead`), results are handed over when
something looks: ADC3's registers catch up as before, the DMA's
(`Stm32H7Dma.cs`) and the DMAMUX's catch up first (`VhilLazy.cs`), and the
pages the stream will write go through the bus (`SetPageAccessViaIo`) with
hooks that catch up before a read or write lands. The result that sets the
stream's next flag keeps its clock event, so its interrupt is taken at the
same instruction. Results that come due together are handed over in one
go (`DeliverInBulk`, `TakeMany`): ADC3's registers and the stream's counters
as the last of them leaves them, and the items written to RAM at once,
instead of a DMA request through the DMAMUX, a bus read of DR and a bus
write per result. Lock-step runs only (`vhil.sim` and `speed.py`'s sim path
launch Renode with `VHIL_LOCKSTEP=1`): there a monitor command lands only
while every machine is paused; on the wall-clock bench the broker's commands
land while the CPU runs, and the bench keeps an event per result.

[`tests/sim/test_lazy_dma.py`](../tests/sim/test_lazy_dma.py) runs ams and
ecu-ams to steady state both ways (`VHIL_LAZY_DMA=0`: an event per result)
and compares the bus timelines, GPIO edges by instruction count, ADC3's and
DMA1's counts, executed instructions and every register and RAM byte:
identical. Not an edge's `t_us`: the GPIO probe stamps the time the CPU last
reported, which moves with where Renode ends a chunk (`VhilProbe.cs`).

[`tests/sim/test_fast_forward.py`](../tests/sim/test_fast_forward.py) runs
ecu, ams, ecu-ams, udv and ecu-ams-udv from power-on into steady state with
and without it and compares every bus's timeline (offer, start, end,
outcome, in ns), GPIO edges
with the executed-instruction count at each write, the radio's payloads, and
at the end each CPU's executed instructions, its registers and every RAM byte
(a SHA-256): identical, and the run with it must have skipped every kind (the
AMS's ADC3 polls included; the uDV's yielding poll, tick waits and
store-free ADC3 polls).

`VHIL_FAST_FORWARD=0` in Renode's environment turns it off for a run, hooks
and all; `vhil_ff_<board> Enabled false` (or `Counters`, `Polls`) at the
monitor, and `vhil_ff_<board> Stats` says what it skipped and what that cost.
On the wall-clock bench, a monitor command that lands inside a skip (a
broker's DAC or TCA write, timed by the host) takes effect at the skip's end,
at most one chunk later in virtual time: still host-timed, as before.

## Results

Real-time factor, before (`--baseline`) → after, each pair measured on the
same host (2026-10-11): CI is the `emulation speed` job on a GitHub runner (4
vCPU, x86-64, one run); M-series is an 8-core Apple M-series Mac through
Colima, limited to 4 cores (median of 2) and to 2 (one run), `VHIL_DOCKER_CPUS`.

| System | Path | Boot 0-3 s: CI | M-series 4 / 2 cores | Steady +5 s: CI | M-series 4 / 2 cores |
|---|---|---|---|---|---|
| ams | bench | 0.48 → **0.98** | 0.58 → 1.24 / 0.58 → 1.32 | 1.26 → **1.42** | 1.68 → 1.91 / 1.76 → 2.02 |
| ams | sim | 0.13 → **0.80** | 0.14 → 1.01 / 0.18 → 1.04 | 1.52 → **1.62** | 1.84 → 2.04 / 2.14 → 2.04 |
| ecu | bench | 0.38 → **1.05** | 0.53 → 1.17 / 0.60 → 1.39 | 1.26 → **1.29** | 1.26 → 1.50 / 1.28 → 1.70 |
| ecu | sim | 0.26 → **1.19** | 0.35 → 1.29 / 0.36 → 1.26 | 2.13 → **2.72** | 1.63 → 2.46 / 1.69 → 2.45 |
| ecu-ams | bench | 0.34 → **0.72** | 0.32 → 0.91 / 0.42 → 0.80 | 0.62 → **0.69** | 0.57 → 0.93 / 0.73 → 0.74 |
| ecu-ams | sim | 0.12 → **0.59** | 0.14 → 0.79 / 0.14 → 0.75 | 0.85 → **0.89** | 0.95 → 1.14 / 0.97 → 1.21 |

- **Boot**: 2-6x faster, the bootloaders' window fast-forwarded. A single
  board now boots at about real time on CI; two boards at 0.6-0.7x.
- **Steady state**: 1.0-1.3x. The ECU's nRF24 delays are skipped; what the
  firmwares do between their busy-waits is executed as before.
- Native tests on CI: the 15 shards of `sim.yml` took 151 min in total
  (longest 18 min) against 294 min (longest 41 min) for the same tests on dev
  the same day (#261 against #259).
- IFS_HIL's AMS suite on CI (bench path): 855 s against 909-1003 s for the
  same 76 cases on dev; most of a suite is paced to real time, and only its
  boots and sub-real-time stretches get shorter.

### Renode's own hot paths (#260)

Since #271 the image and CI run Renode 1.17.0 built from source with three
patches to renode-infrastructure ([`docker/renode/`](../docker/renode/README.md);
[the study](proposals/renode-fork-study.md)): clock entries found by index,
bus accesses without allocations, and the register framework's allocations
and unused log formatting gone. What a run computes is the same, byte for
byte. Same GitHub runner (4 vCPU, x86-64), the release package and the
patched build interleaved, median of 3 (2026-10-13, dev 6363b2e, fast-forward
on):

| System | Path | Boot 0-3 s: release | patched | | Steady +5 s: release | patched | |
|---|---|---|---|---|---|---|---|
| ams | bench | 1.76x | 1.91x | +8 % | 3.14x | **5.84x** | +86 % |
| ams | sim | 1.50x | 1.83x | +22 % | 4.19x | **7.11x** | +70 % |
| ecu | bench | 1.85x | 2.11x | +14 % | 2.85x | **4.45x** | +56 % |
| ecu | sim | 1.96x | 2.37x | +21 % | 5.91x | **8.36x** | +41 % |
| ecu-ams | bench | 1.36x | 1.56x | +15 % | 1.64x | **2.49x** | +52 % |
| ecu-ams | sim | 1.12x | 1.35x | +21 % | 2.08x | **2.96x** | +42 % |

On x86 the steady state gains more than the study measured on Apple silicon
(+13-58 %); the boot, which the bootloaders' fast-forwarded polling
dominates, less. Runners differ by up to 2x between jobs, so compare a
build with another only on the same runner (as here), not across the
`emulation speed` job's runs. The 15 native-test shards took 125 min of
pytest in total (longest 15 min), against 148 min (18 min) for the same
540 tests on dev's last full run (#265), on other runners.

### The vHIL's models, start-up, throughput and CI (#260)

Dev before #260's model changes (3ab9041) → after them (#263-#270, #272,
#273, #275), both on the patched image above, the M-series host through
Colima, `VHIL_DOCKER_CPUS=4`, interleaved, median of 3 (2026-10-14). The host
was shared with other jobs, so the factor moves ±15 % between runs; the
host CPU seconds Renode's process spends per virtual second (`speed.py`'s
CPU column, #266) is the steadier measure. Steady state, 8 s after a 3 s boot:

| System | Path | Steady factor | CPU s per virtual s |
|---|---|---|---|
| ams | sim | 3.19x → **4.74x** | 0.423 → **0.269** |
| ecu | sim | 3.76x → **3.91x** | 0.336 → **0.329** |
| ecu-ams | sim | 1.67x → **1.73x** | 1.211 → **1.107** |
| ams | bench | 2.59x → **2.75x** | 0.528 → **0.456** |
| ecu | bench | 2.28x → **4.23x** | 0.510 → **0.300** |
| ecu-ams | bench | 1.16x → **1.73x** | 1.205 → **0.830** |

- **AMS**: ADC3's DMA results handed over lazily (#268) and in bulk (#275)
  in lock-step runs, its `HAL_ADC_PollForConversion` fast-forwarded (#263),
  SPI1's and the FDCAN CCU's per-access warnings gone (#265).
- **Bench path**: `speed.py` acknowledges the buses the bench bridges, as the
  bench's SocketCAN bridge does (#266); before, the ECU's lone buses retried
  every frame (the bus model's "eager wakes" of #260 were those retries).
  The bench itself keeps ADC3's event per result.
- **ecu-ams** gains least: its two boards meet every 500 us, and the
  lock-step hand-off, not either board's work, is most of its time (below).
- **Boot (0-3 s)**: unchanged within noise: the bootloaders' window was
  already fast-forwarded; what is left is the new process's JIT and the
  apps' own start.
- **Starting a Renode** (#269): 5.4 → 3.4 s; peak RSS 1.66 → 0.60 GB.

Throughput: scenario runs (`ecu-ams/car-arms-through-the-ecu`) an hour, K at
once in one container, before → after. The VM has 8 GB shared with other
jobs; before, Renodes were OOM-killed first.

| Host | K=1 | K=4 | K=8 |
|---|---|---|---|
| 4 cores | 163 → 171 | 408 → 474 | 280, 2 OOM → **590** |
| 8 cores | 158 → 169 | 552 → 636 | 752, 1 OOM → **919** |

A run keeps one to two cores busy (its CPU threads and Renode's time
source), so a host saturates at about one to two runs per core, and at 0.6 GB
a run memory no longer limits it. Tried with no effect beyond noise:
`DOTNET_EnableWriteXorExecute=0`, and (earlier, #154) `DOTNET_gcServer`,
`DOTNET_GCgen0size`; worse: `DOTNET_TieredPGO=0`, `DOTNET_TieredCompilation=0`.
A Renode is not kept across tests: each test starts from a real power-on
through the CAN bootloader's window, and virtual time, flash and the bus
models' records would carry over.

CI (`sim.yml`): 15 shards running their files one at a time (156-165
runner-minutes, the longest 17-19 min) → 8 shards running two files at a
time (`scripts/sim-shard.sh`, #272), split by each file's measured time
(`scripts/sim-balance.py`, `tests/sim/durations.json`, #273): 86
runner-minutes, the longest 12.3 min. Refresh `durations.json` from the
per-file tables in the shards' job summaries when suites change much.

### Start-up: the models' assembly and ReadyToRun

Every Renode the vHIL starts (`vhil.renode.launch`) loads every C# model in
`models/renode/` at start, in one `include`, from Renode's compiler cache
([`vhil/models.py`](../vhil/models.py)): the cache is content-addressed
(Renode files an assembly under the SHA-256 of its sources' bytes), so the
assembly is compiled once per change to a model, by Renode itself, and every
later start loads it without Roslyn. Generated scripts then include no model
(`render_renode(preloaded=True)`); renode-test's smoke suites still compile
theirs. Locally (`VHIL_MODELS` unset or `auto`) the first start after a model
changes compiles it; CI (`.github/actions/setup-renode`, after the checkout)
and the web app's workers (at start, from their workspace) build it with
`python -m vhil.models build` and run with `VHIL_MODELS=prebuilt`, where a
start whose sources' hash is not built fails, naming the hash and what is
built, instead of compiling. The Docker wrapper keeps the cache in its
volume. Renode's own assemblies are compiled ReadyToRun at the image build
(`docker/renode/build.sh`): native code in the package, still re-JIT by
tiering when hot. What a run computes is unchanged (the identity
fingerprint, 10 s of ecu, ams and ecu-ams, is byte-identical to dev's).

Dev → the models' assembly → plus ReadyToRun, on the patched image, the
M-series host through Colima, `VHIL_DOCKER_CPUS=4`, interleaved, median of 3
(2026-10-14). Start: process launch to the system loaded (`Sim.start`); to
the app: through the bootloader's window to the app running; bare: a Renode
with its monitor up and nothing loaded.

| | Start | To the app | Peak RSS | Bare RSS |
|---|---|---|---|---|
| ecu | 4.02 → 2.28 → **1.93 s** | 6.58 → 4.19 → **3.89 s** | 570 → 360 → 382 MB | |
| ams | 3.90 → 2.20 → **1.86 s** | 6.04 → 4.17 → **3.81 s** | 573 → 361 → 383 MB | |
| ecu-ams | 4.14 → 2.40 → **2.12 s** | 7.25 → 5.46 → **5.17 s** | 621 → 406 → 427 MB | |
| bare | 1.94 → 1.05 → **0.83 s** | | | 427 → **280** → 302 MB |

ReadyToRun takes another 0.3 s off a start for 20 MB more RSS (its native
code is mapped) and 74 MB more package; steady state is unchanged and the
boot's host CPU is up to 10 % higher (the boot runs ReadyToRun code before
tiering promotes it), yet runs an hour are as many or more. Scenario runs
(`ecu-ams/car-arms-through-the-ecu`) an hour, K at once in one container,
dev → the models' assembly → plus ReadyToRun (one 240 s window each, so ±1
run):

| Host | K=1 | K=4 | K=8 |
|---|---|---|---|
| 4 cores | 165 → 180 → **195** | 480 → 600 → **600** | 570 → 675 → **720** |
| 8 cores | 180 → 180 → **195** | 615 → 720 → **720** | 960 → 1080 → **1185** |

### Several quanta at once: studied, not done (#260)

The lock-step hand-off costs the same every quantum, so the question was
whether a board could be granted several quanta at once when provably
nothing could cross between boards in them, without changing any result and
without changing the 500 us quantum. A sync point s can be passed over
exactly only if nothing that happens at s would have been different, which
needs all of:

- **no board runs code before the grant ends.** A board running code may
  write a frame, and the bus model delivers what is offered before a sync
  point at that sync point: on a two-board system the other board would see
  it a quantum later than it does now. Whether a running firmware transmits
  in the next quantum is not known in advance (there is no lookahead), so
  every board must be in WFI at s, and the grant must end before the
  earliest clock entry of any board (its next timer interrupt wakes it);
- **nothing pending on any bus** (the bus model acts at sync points: a
  frame offered, in flight or due for delivery) and **no co-simulation step
  due** before the grant ends;
- the sync-point actions (`SyncHook`: the fast-forward's arming, the CAN bus
  model, the bench clock) must have nothing to do at s. They are host-speed
  only today but would need checking one by one.

Measured upper bound, ignoring the bus and co-simulation conditions (a probe
on `SyncHook` reading each CPU's WFI state through
`AddHookAtWfiStateChange` and each machine's `NearestLimitIn`; lock-step,
dev, 3 s boot then 8 s steady): the sync points at which every board is in
WFI and the next clock entry of every board is more than a quantum away,
i.e. the sync points a multi-quantum grant could pass over.

| System | Boot 0-3 s: all in WFI / passable | Steady +8 s: all in WFI / passable |
|---|---|---|
| ecu | 700 / **289** of 6,001 | 1,652 / **34** of 16,000 |
| ams | 200 / **112** of 6,001 | 3,639 / **0** of 16,000 |
| ecu-ams | 24 / **18** of 6,001 | 1,103 / **0** of 16,000 |

At most 0.2 % of the steady state's hand-offs, none on the AMS or ecu-ams,
and under 5 % of the boot's. Both firmwares tick at 1 kHz (FreeRTOS's SysTick, the
HAL's TIM23) and the AMS's ADC3/DMA flags and peripheral timers add more,
so some board's next clock entry is almost always within one 500 us
quantum; a board is in WFI at only 10-23 % of the sync points, and two at
once rarely. So it is not implemented: an exact version buys nothing, and
anything looser changes when a firmware sees the other board's frames.
What could still cut the hand-off's cost is making each one cheaper (the
spin, the master's loop), not skipping them.

### This round, end to end (#260)

Dev before it (212b827, patch set 1.17.0-vhil.1) → after it (#278 the models'
assembly and ReadyToRun, #279 patch 0004, #281 patch 0005 and tlib/0001; #280
the study above). On x86: one GitHub runner (4 vCPU) building both Renode
packages from their trees and running both interleaved, median of 3
(2026-10-14, a temporary branch since deleted):

| | Start | To the app | Peak RSS |
|---|---|---|---|
| ecu | 7.63 → **3.50 s** | 10.25 → **5.53 s** | 590 → 460 MB |
| ams | 7.10 → **3.32 s** | 9.31 → **5.64 s** | 590 → 461 MB |
| ecu-ams | 7.63 → **4.01 s** | 11.34 → **7.39 s** | 584 → 511 MB |
| bare Renode | 3.14 → **1.37 s** | | 471 → 335 MB |

| System | Path | Boot 0-3 s | Steady +8 s | Steady CPU s / virtual s |
|---|---|---|---|---|
| ams | bench | 0.99x → **2.04x** | 3.55x → 3.61x | 0.415 → 0.397 |
| ams | sim | 1.34x → **1.82x** | 5.92x → 5.63x | 0.256 → 0.260 |
| ecu | bench | 1.14x → **2.25x** | 4.55x → **6.04x** | 0.405 → 0.254 |
| ecu | sim | 1.41x → **2.27x** | 4.70x → 4.88x | 0.291 → 0.287 |
| ecu-ams | bench | 0.74x → **1.27x** | 1.91x → 2.00x | 0.868 → 0.853 |
| ecu-ams | sim | 0.97x → **1.12x** | 2.32x → 2.43x | 0.965 → 0.924 |

On the M-series host (Colima, 4 cores, interleaved, median of 3) the same
comparison gave boot 1.08-1.48x → 1.17-1.84x and steady state within
-1 to +8 % (its run-to-run noise is ±10-20 %). The boot gains most: a start
no longer runs Roslyn beside the bootloader's window, and ReadyToRun code
runs from the first instruction. Steady state moves little: patch 0004
takes about one point of the samples off the time framework and patch 0005
the AMS's one point of CMP.W hooks, near the noise of a single run.
Runs an hour, scenario `ecu-ams/car-arms-through-the-ecu` (#278): 4 cores
480 → 600 at K=4 and 570 → 720 at K=8; 8 cores 615 → 720 and 960 → 1185.

### Several boards: one, two, three (#260)

The uDV is the next MainLite to join a system, so what grows with the number
of boards was measured before it has a firmware, with `speed.py`'s
three-board benchmark `ecu-ams-ecu` (#283) and `--threads`. GitHub runner (4
vCPU, x86-64), median of 3 (2026-10-15, dev 3307002):

| Boards | System | Boot 0-3 s: sim / bench | Boot CPU s/s (sim) | Steady +8 s: sim / bench | Steady CPU s/s (sim) |
|---|---|---|---|---|---|
| 1 | ecu | 3.50x / 3.72x | 0.72 | 10.06x / 12.62x | 0.18 |
| 1 | ams | 2.63x / 3.12x | 0.81 | 12.45x / 8.16x | 0.14 |
| 2 | ecu-ams | 1.69x / 1.98x | 1.76 | 3.69x / 3.79x | 0.79 |
| 3 | ecu-ams-ecu | 1.43x / 1.73x | 2.22 | 3.09x / 2.84x | 1.08 |

That runner was a fast one: another the same night ran ecu-ams at 0.85x boot
and 1.62x steady, ecu-ams-ecu at 0.76x and 1.40x (lock-step), as the numbers
of #260 before it. Compare within a job only.

- **Where it goes.** A lock-step run of several boards (invariant 11) is the
  master time source (on the monitor's thread) granting each quantum to one
  slave time source per board, each with a dispatcher thread of its own, that
  grants it to its board's CPU thread: two hand-offs a quantum per board, and
  2N + 1 threads that spin while they wait. One board, and the wall-clock
  bench's shared time source, have one hand-off. Host `perf` by thread, steady
  state, x86: ecu-ams: the slaves' threads 29 % of Renode's samples, each CPU
  thread 27 %, the master 14 %; ecu-ams-ecu: slaves 24 %, CPUs 20-22 % each,
  master 10 %; a quarter of all samples in the kernel's scheduler
  (`sched_yield`, task switches). By thread, CPU s per virtual s (sim):
  ecu-ams CPUs 0.20 + 0.20, master 0.12, slaves 0.24; ecu-ams-ecu CPUs 0.21 +
  0.22 + 0.20, master 0.12, slaves 0.30.
- **One to two is the superlinear step.** Steady CPU goes from 0.32 (the two
  boards alone, summed) to 0.79: each CPU thread doubles, because it now waits
  for the other board at every sync point (the boards' busy stretches rarely
  fall in the same quantum, so each quantum takes the busier board's time and
  the other spins), and the slaves add 0.24. Two to three grows about
  linearly (+0.29: one more CPU thread, one more slave).
- **The boot** costs about the two boards' sum (1.76 against 0.72 + 0.81):
  the slaves add 0.25 and the waiting CPU threads 0.2, and one JIT and one
  master instead of two take about as much back. The busy-wait fast-forward
  engages exactly as with one board (the same 5,993 and 5,947 bootloader-loop
  skips per board as alone); each board's translation cache is 128 MiB
  (clamped from 512) with no flush. What a boot spends is per board: the
  bootloader's window still runs a few real iterations of its loop per chunk
  for the detector, and the AMS's isoSPI wake (`ltc6820.cpp` `delay_us`, a
  poll on DWT CYCCNT, fast-forwarded since #289) was a fifth to a
  quarter of the AMS's boot; the JIT adds 0.5 CPU s per virtual s over the
  first seconds.
- **Without the slaves' hop**, estimated by running the lock-step path on the
  shared time source (as `mach create` does; not deterministic, a measure of
  cost only): ecu-ams steady CPU 1.60 → 1.10 s per virtual s (-31 %), factor
  1.62x → 1.73x, boot 3.37 → 2.64 CPU s/s, 0.85x → 0.92x (slower runner).
- **Pinning threads to cores** (each board's CPU thread to a core of its own,
  optionally the master and the slaves to the rest; `sched_setaffinity` from
  the thread) made nothing faster: one run, ecu-ams steady 2.74x unpinned,
  2.52x pinned, 2.79x pinned with the rest apart; ecu-ams-ecu boot 1.24x,
  1.25x, 1.01x; scenario runs an hour (`ecu-ams/car-arms-through-the-ecu`)
  195 → 180 at K=1, 390 → 300 at K=4, 360 → 315 at K=8. Linux places the
  threads as well on its own, and a pinned thread cannot move off a core
  another run is using. Not adopted.
- **Every `RunFor` and monitor command** has a fixed cost on top: a scenario
  run sends 900-1,500 monitor commands (a slice's frames, sent frames and
  load per bus, edges and samples per board), a native test's `run_until` one
  `RunFor` and a time query per 10 ms step, and Renode starts every CPU's and
  every slave's thread anew at each `RunFor`. #284 took the command history
  file (rewritten after every command, and growing across runs) and the
  per-byte socket I/O out of the monitor: a bare command 0.82-0.89 → 0.33-0.37
  ms (1.86 → 0.36 ms with a 20,000-line history), the fixed cost of a
  `RunFor` 1.24-1.39 → 0.60-0.64 ms (ecu) and 1.52 → 1.05-1.10 ms (ecu-ams),
  a scenario run 13.2-13.5 → 12.3-12.7 s; the identity fingerprint is
  unchanged.


### The uDV, alone and in ecu-ams-udv

Measured once the uDV had a firmware (IFS09-DV-uDV#12), on a MacBook through
`scripts/vhil-docker.sh speed` (Colima VM, 8 vCPU arm64), median of 3
(2026-10-15). The ECU and ecu-ams, one run on the same host the same day,
for scale:

| System | Boot 0-3 s: sim / bench | Boot CPU s/s (sim) | Steady +5 s: sim / bench | Steady CPU s/s (sim) |
|---|---|---|---|---|
| ecu | 1.60x / — | 0.91 | 3.86x / — | 0.34 |
| ecu-ams | 1.06x / 1.17x | 2.17 | 1.50x / 1.43x | 1.28 |
| udv | 0.24x / 0.58x | 4.52 | 0.07x / 0.18x | 13.9 |
| ecu-ams-udv | 0.24x / 0.45x | 6.31 | 0.08x / 0.16x | 14.4 |

- **The uDV never slept** (then; fixed by IFS09-DV-uDV#15, below). Its idle
  task spun: `configUSE_IDLE_HOOK` was 0, so there was no `WFI`
  (IFS09-DV-uDV#13). A PC profile of the steady state (`speed.py
  --profile`) put 84 % of its core's time in `prvIdleTask` and
  `prvCheckTasksWaitingTermination`, which the fast-forward doesn't skip (not
  a polling loop), so the core ran every instruction: its CPU thread took
  13-22 s per virtual s. The ECU and the AMS sleep in their idle hooks
  (IFS08-CE-ECU#261, IFS08-CE-AMS#626).
- **So a system with a uDV runs at the uDV's pace.** ecu-ams-udv is the uDV's
  rate, the ECU and the AMS waiting for it at every sync point; the three
  boards' own cost (ECU 0.7, AMS 0.6 CPU s/s) is what ecu-ams-ecu measured.
- The bench path is 2-3x the sim path here: one shared time source, and every
  core at the wall-clock bench's rate instead of the catalogue's 231 MIPS.

### Several boards: the slaves on the master's thread, the CYCCNT waits (#260)

Two changes after the measurements above, each its own PR, each with the
identity fingerprint (ecu, ams, ecu-ams and ecu-ams-ecu, 10 s from power-on)
byte-identical to dev's and A/B on one GitHub runner (4 vCPU x86):

- **#288, patch 0006 (vhil.5): every board's slave time source runs on the
  master's thread** (`SlaveTimeSource.RunsOnMasterThread`, turned on by
  `VhilMachine`; `VHIL_INLINE_SLAVES=0` keeps the threads). Renode's
  `TimeSourceBase.InnerExecute` is split into a begin (grant or unblock every
  handle) and an end (wait for them, the sync phase), and the master begins a
  slave's iteration when it grants it time and ends it when it waits for it:
  one hand-off a quantum per board and N + 1 threads, each board still on a
  time source of its own (invariant 11). Renode's slave time-source unit
  tests run both ways. Lock-step: ecu-ams steady 1.63x → 2.01x (CPU 1.60 →
  1.02 s per virtual s), boot 0.87x → 0.95x; ecu-ams-ecu steady 1.36x → 1.63x
  (CPU 2.29 → 1.63), boot 0.76x → 0.84x; single boards and the bench path
  (no slaves) unchanged. A `RunFor`'s fixed cost on ecu-ams 0.97 → 0.68 ms.
  Scenario runs an hour 210 → 225 (K=1), 480 → 480 (K=4 and 8; mean run -4 to
  -6 %).
- **#289: the AMS's CYCCNT waits fast-forwarded** (above): AMS boot 2.75x →
  3.72x, ecu-ams boot 1.77x → 2.20x, ecu-ams-ecu boot 1.60x → 1.91x (a fast
  runner); scenario runs an hour 225 → 240 (K=1), 480 → 540 (K=4), 480 → 600
  (K=8).

After both, 1 → 2 → 3 boards (two runs on two slower runners, median of 3
each; before: the same class of runner, #285's numbers):

| Boards | System | Boot 0-3 s: sim / bench | Boot CPU s/s (sim) | Steady +8 s: sim / bench | Steady CPU s/s (sim) |
|---|---|---|---|---|---|
| 1 | ecu | 1.94-1.99x / 2.05-2.08x | 1.08-1.12 | 4.79x / 5.74-6.18x | 0.33 |
| 1 | ams | 1.98-2.03x / 1.85-1.89x | 1.09-1.12 | 5.36-5.66x / 3.18-3.28x | 0.27-0.29 |
| 2 | ecu-ams | 1.12-1.14x / 1.05-1.10x | 2.47-2.52 | 1.94-2.01x / 1.46-1.51x | 1.01-1.04 |
| 3 | ecu-ams-ecu | **0.98-0.99x** / 0.88-0.90x | 3.23-3.26 | **1.58-1.64x** / 1.18-1.21x | 1.62-1.67 |

On those runners three boards boot at real time in lock-step (0.76x before)
and run 1.6x in steady state (1.40x). The step from one board to two is now
the CPU threads waiting for each other alone; two to three adds one CPU thread
(+0.6 CPU s per virtual s steady, +0.75 at boot).

Tried, not adopted:

- **A CPU thread kept across `RunFor`s.** Renode starts a board's CPU thread
  at every `RunFor` and joins it at the pause. After #288 a `RunFor`'s fixed
  cost is 0.54 ms on ecu and 0.68 ms on ecu-ams: 0.14 ms per extra board, of
  which the thread is a part, about 1-3 % of a native test's 10 ms step.
  Parking the thread instead touches BaseCPU's pause, step and abort paths
  for that; not done.
- **Re-proving the bootloader loop's fixed point from the last one.** The
  detector proves the fixed point again in every chunk (two iterations and a
  RAM snapshot). Starting each chunk's check from the fixed point found in
  the last (one iteration), with the detector left armed between chunks,
  skipped as much but cost more: the detector armed is a C# call per MMIO
  access, and the check already starts in the iterations left at the end of
  the previous chunk (152,000 hooked accesses and 273 ms against 119,000 and
  138 ms over the window).

### A new identity reference: STM32 timers' ARR + 1 period (#292)

Patch 0007 (vhil.6) is a fidelity fix, not a speed-up: an STM32 timer's
update event now comes every ARR + 1 counts, as on the chip, so the identity
fingerprint changes once and the vhil.6 one is the reference for later
patches. What moved, and by how much (only timing; frames, edges and their
order are the same):
[`docker/renode/README.md`](../docker/renode/README.md#the-patches). It
adds no work per period (the same clock-entry updates).

### The hand-off's wait: studied, not shipped (#260)

Profiled with wait-time histograms in the time framework (every
`MonitorCondition.Wait`, per thread and condition; `InnerExecute`'s grant,
wait and sync phases; each CPU's execution of its grant), steady state, 5 s
after a 3 s boot, the M-series host through Colima, 4 cores. Per 500 us
quantum, means:

| | ecu sim | ams sim | ecu bench | ecu-ams sim |
|---|---|---|---|---|
| quantum, wall | 117 us | 104 us | 107 us | 373 us |
| a CPU executing its grant | 91 us | 82 us | 79 us | 145 / 136 us |
| the master's sync phase (SyncHook, delayed actions) | 3.3 us | 3.2 us | 4.2 us | 9.2 us |
| a CPU waiting for its next grant | 25 us | 21 us | 28 us | 228 / 237 us |

A single board's CPU waits 20-40 us for each grant after it reports, against
3-9 us of work at the source: the rest is two wake-ups. The waits are
bimodal: a third end within 8 us (the other side was still spinning), most
of the rest at 16-32 us (it had gone to sleep). `SemaphoreSlim.Wait` spins
about 100 us (`SpinWait`, alternating pause loops and `sched_yield`) and then
sleeps, and the master's wait (the CPU's whole quantum, 80-150 us) outlasts
it, so each sync point ends in a futex wake-up, which in Colima's VM also
wakes a halted vCPU: blocking at once (no spin at all) cost about 55 us per
wake-up on this host (ecu sim 3.80x → 2.59x). Several boards add the slave
time sources' relay: four wake-ups per board per quantum, five threads for
two boards.

Tried, as a patch to `MonitorCondition`
([`renode-patches/02-handoff-waiter.patch`](proposals/renode-patches/02-handoff-waiter.patch),
not in the queue): one waiter per thread, signalled by one interlocked
exchange (a pulse only if it sleeps), that spins (yielding) up to
`RENODE_HANDOFF_SPIN_US` while the host has a core no other runnable thread
needs (`/proc/loadavg`, sampled every 500 us and re-checked while spinning)
and up to 100 us otherwise, then sleeps. What a run computes is unchanged
(the identity fingerprint is byte-identical; Renode's time-framework tests,
`test_fast_forward.py`, `test_lazy_dma.py` and the two-board determinism test
pass). Steady state, the M-series host, interleaved, median of 3, spin 1000
us against dev (patch set vhil.3):

| System | Path | 4 cores: factor | CPU s/s | 8 cores: factor | CPU s/s |
|---|---|---|---|---|---|
| ams | bench | 3.18x → 4.76x | 0.395 → 0.406 | 3.17x → 5.31x | 0.401 → 0.373 |
| ams | sim | 4.96x → 7.00x | 0.257 → 0.277 | 3.68x → 6.46x | 0.335 → 0.301 |
| ecu | bench | 4.25x → 7.70x | 0.302 → 0.254 | 4.30x → 8.07x | 0.297 → 0.245 |
| ecu | sim | 3.81x → 4.64x | 0.335 → 0.420 | 3.77x → 6.14x | 0.339 → 0.323 |
| ecu-ams | bench | 1.50x → 2.15x | 0.948 → 1.297 | 1.69x → 2.49x | 0.846 → 1.154 |
| ecu-ams | sim | 1.77x → 2.50x | 1.095 → 1.367 | 1.50x → 2.90x | 1.336 → 1.620 |

Runs an hour there (`ecu-ams/car-arms-through-the-ecu`, one 300 s window
each, ±8 % between windows): 8 cores 180 → 180 (K=1), 528 → 576 (K=4), 864 →
1044 (K=8); 4 cores within -8 to +4 % at K=2, 4 and 8.

On x86 it is not a win. One GitHub runner (4 vCPU) building both packages
and interleaving them, median of 3 (2026-10-15, a temporary branch since
deleted): spin 1000 us, steady state ams +10-11 %, ecu +8-9 %, ecu-ams -3 to
-1 %, at 9-55 % more CPU s/s (ecu-ams +37-55 %); runs an hour 168 → 168
(K=1), 288 → 240 (K=2), 384 → 336 (K=4), 384 → 372 (K=8). Spin 300 us did
no better. Another agent's interleaved run of the same patch found ecu-ams
sim 3.31x → 3.19x at 0.86 → 1.10 CPU s/s, bench 3.35x → 3.20x at 0.65 → 0.92.
KVM wakes a sleeping thread far more cheaply than Apple's hypervisor, so the
spin buys little there and costs the cores a K=2 CI shard or a worker's
other runs need. The cheaper waiter alone (spin 100 us, as `SemaphoreSlim`
did) was within ±3 % in speed and CPU on every system and path, and runs an
hour 192/168 → 192/192 (K=1), 312/240 → 312/288 (K=2), 432/336 → 480/384
(K=4), 480/468 → 540/480 (K=8), two windows each: possibly a few percent of
throughput, not clear enough to carry a patch for. Blocking at once was
slower everywhere (also P1, above), and spinning without yielding starved
the CPU threads of two boards on 4 cores (ecu-ams 1.72x → 0.96x).

So the hand-off stays as it is. On an M-series Mac, a long spin would make a
single run 1.4-1.9x faster; what would help on x86 too is fewer hand-offs
(the slave on the master's thread, below), not a different wait.

### The uDV and ecu-ams-udv made fast (#260)

Where it started, after the uDV slept in its idle hook (IFS09-DV-uDV#15):
on a MacBook through Colima (8 vCPU), steady state udv 0.50x (sim) / 0.84x
(bench), ecu-ams-udv 0.39x / 0.63x; boot udv 0.88x, ecu-ams-udv 0.63x. The
benchmark ecu-ams-ecu ran three boards at ~1.6x on CI, so the uDV board was
the expensive one.

**Profiles** (`speed.py --threads --profile`, host `perf` with .NET's perf map
on the Mac and on GitHub's x86 runners, and the uDV's executed instructions
per 10 ms step in lock-step):

- **Its micro-ROS agent ping.** With no agent on USB (every vHIL run, and the
  car while the DVPC is down) `ros_task.c:361` pings for 100 ms of every
  600 ms, and the transport read polls the CDC RX ring with a `taskYIELD` on
  every pass (`usb_cdc_transport.c` `cubemx_transport_read`): 2.1 M
  instructions per 10 ms in those windows, against 0.05 M outside them, run
  at 5.6x slower than real time. About 45 % of the uDV's host time; the
  fixed-point detector refused it as an interrupt (the PendSV).
- **ADC3, six single conversions a millisecond** (`hardware_io.c`
  `hardware_io_read_adc_raw`, from AppTask's 1 ms loop): each reconfigures,
  starts, polls (6.8 us) and stops ADC3, ~50 register accesses. A third of
  the uDV's host time: the time-bounded fast-forward's proof per conversion
  (a 330 KB RAM snapshot copied and compared), the model syncing the time on
  every access, the cue's calls into tlib on every idle read.
- **`HAL_Delay` at boot**: the BMI088 and I2C bring-up spin 140 ms in it
  (`bmi088.c`, `i2c_utils.c`); no peripheral access, so no fast-forward;
  4-7x slower than real time on Apple silicon.
- **Its CAN reception in a system**: Renode's MCAN decodes every filter
  element of the message RAM for every frame by reflection (`Packet`, no
  attribute cache): 6 % of ecu-ams-udv's samples, the receive path 8-12 %.
- **The bench path alone**: udv.yaml bridged no bus, so the uDV was alone on
  both and retried every frame with an ACK error; each retry a bus event, the
  CPU ran in chunks of ~100 us and skips covered a few iterations.
- **The rest, per board**: FreeRTOS critical sections read ICSR (Renode's
  RETTOBASE by reflection each time); the detector armed on Apple silicon
  brought every RAM access to C#; the tiered JIT recompiled thousands of
  methods during each boot (0.76 CPU s per virtual s beside three boards).

**Changes**, each its own PR with the identity fingerprint (ecu, ams,
ecu-ams, udv, ecu-ams-udv, ecu-ams-ecu; 10 s from power-on; #298 added the
three) byte-identical to dev's, `test_fast_forward.py` (now with udv and
ecu-ams-udv) and `test_lazy_dma.py` green, and an interleaved A/B on one x86
runner (median of 3; runner classes differ by up to 2x, so each PR's numbers
are within its own run):

| PR | Change | Before → after (x86 runner) |
|---|---|---|
| #297 | Self-yielding polls fast-forwarded (above) | EPYC 7763: udv sim steady 0.61x → 1.13x; ecu-ams-udv sim 0.45x → 0.68x, bench 0.54x → 0.64x |
| #300 | `udv.yaml` bridges its buses (`host_netdev` as ecu-ams-udv's): acknowledged as on the car | EPYC 9V45: udv bench steady 1.52x → 1.74x (lock-step unchanged) |
| #301 | Tick waits (`HAL_Delay`) fast-forwarded | EPYC 7763: udv boot 1.19x → 1.25x; Mac: the 140 ms window no longer runs |
| #302 | tlib/0002 (vhil.7): the detector armed sees MMIO only | Mac: boots +25-45 %; x86 within ±3 % (RAM hits are inline there) |
| #303 | Patch 0008 (vhil.8): `Packet` caches field attributes | EPYC 9V45: ecu-ams-udv steady 0.85x → 0.91x sim, 1.06x → 1.17x bench; ecu-ams 4.01x → 4.42x |
| #304 | Store-free polls proven from their code, no RAM snapshot | EPYC 9V74: udv steady 1.67x → 1.82x sim, 1.97x → 2.15x bench; ecu-ams-udv 0.95x → 0.99x |
| #305 | Patch 0009 (vhil.9): ICSR's RETTOBASE without reflection | Xeon 8573C: udv steady +3-5 %, ecu-ams-udv +3 %, ecu and ams +2-5 % |
| #306 | The time-bounded cue asks the model before tlib | Xeon 6973P: udv bench steady 2.35x → 2.66x, ecu-ams-udv +1-3 % |
| #299 | ADC3 syncs the time only while a conversion runs | EPYC 9V74: udv bench steady 0.93x → 1.01x, sim 0.66x → 0.69x |
| #307 | ICSR read at a boundary only for an iteration that yields | EPYC 7763: udv steady +2-3 % |
| #308 | `DOTNET_TC_CallCountThreshold=300` | EPYC 9V45: boots +29-63 % (ecu-ams-udv 1.61x → 2.07x), steady -5 % on ecu-ams-udv, ±3 % elsewhere; runs an hour +5-18 % |

#304's StillThere check first went through the system bus (4-6 % of the
uDV's samples); it reads the code from the memory directly, and the A/B of
it with #306 on top measured ecu-ams-udv 1.11x → 1.32x steady on one runner.

**End to end.** Dev 0b7449a (before this round, patch set vhil.6) against
dev 3837a06 (vhil.9, #308 included), interleaved, median of 3. x86: two
GitHub runners (4 vCPU) in two jobs; the first ran the old dev about as the
slowest runners do (ecu-ams-udv steady 0.48x):

| System | Path | Boot 0-3 s: EPYC 9V74 / Xeon 8573C | Steady +5 s: EPYC 9V74 / Xeon 8573C | Steady CPU s/s (9V74) |
|---|---|---|---|---|
| udv | sim | 1.24x → **2.23x** / 1.29x → **2.25x** | 0.66x → **1.90x** / 0.76x → **2.05x** | 1.722 → 0.706 |
| udv | bench | 1.52x → **2.64x** / 1.56x → **2.68x** | 0.92x → **2.42x** / 1.00x → **2.56x** | 1.264 → 0.556 |
| ecu-ams-udv | sim | 0.76x → **1.18x** / 0.83x → **1.27x** | 0.48x → **1.00x** / 0.56x → **1.19x** | 3.524 → 2.348 |
| ecu-ams-udv | bench | 0.81x → **1.16x** / 0.86x → **1.25x** | 0.57x → **0.97x** / 0.66x → **1.10x** | 3.034 → 2.294 |
| ecu | sim | 2.12x → 2.90x / 2.05x → 2.72x | 5.38x → 5.51x / 4.78x → 4.66x | 0.314 → 0.318 |
| ams | sim | 2.15x → 3.06x / 2.14x → 2.88x | 6.00x → 5.86x / 5.47x → 5.29x | 0.286 → 0.282 |
| ecu-ams | sim | 1.23x → 1.79x / 1.28x → 1.86x | 2.25x → 2.44x / 2.36x → 2.59x | 0.936 → 0.940 |
| ecu-ams-ecu | sim | 1.10x → 1.45x / 1.13x → 1.53x | 1.79x → 1.96x / 2.07x → 2.20x | 1.502 → 1.450 |

The M-series Mac (Colima, `VHIL_DOCKER_CPUS=4`), the same two devs, interleaved, median of 3:

| System | Path | Boot 0-3 s | Steady +5 s | Steady CPU s/s |
|---|---|---|---|---|
| udv | sim | 1.05x → **1.72x** | 0.61x → **1.44x** | 1.740 → 0.792 |
| udv | bench | 1.16x → **2.55x** | 0.70x → **2.32x** | 1.542 → 0.516 |
| ecu-ams-udv | sim | 0.67x → **1.54x** | 0.42x → **1.23x** | 3.260 → 1.496 |
| ecu-ams-udv | bench | 0.84x → **1.41x** | 0.64x → **1.11x** | 2.234 → 1.560 |
| ecu / ams | sim | 1.73x → 2.59x / 2.00x → 2.80x | 3.98x → 4.00x / 4.95x → 4.71x | |
| ecu-ams-ecu | sim | 1.29x → 1.82x | 1.96x → 2.01x | 1.078 → 1.092 |

Scenario runs an hour (`ecu-ams/car-arms-through-the-ecu` run on ecu-ams-udv,
its stimuli and expects naming the ECU and the AMS; K at once on one runner,
one 300 s window each, all passed): EPYC 9V74 132 → **192** (K=1), 240 →
**432** (K=4), 276 → **420** (K=8); Xeon 8573C 144 → **204**, 288 → **432**,
288 → **480**.

**One, two, three boards** (Xeon 8573C, sim, after): ecu 2.72x boot / 4.66x
steady, ecu-ams 1.86x / 2.59x, ecu-ams-ecu 1.53x / 2.20x, ecu-ams-udv 1.27x /
1.19x. By thread, steady CPU s per virtual s: udv alone 0.47 (ECU alone
0.20); in ecu-ams-udv the uDV's thread 0.69, the ECU's 0.40, the AMS's 0.36,
the master 0.29 (ecu-ams-ecu: 0.33, 0.32 and 0.30 for its ECU, AMS and
second ECU, master 0.19). The uDV is still the slowest board of a quantum, and the others wait
for it; the hand-off's spin and `sched_yield` are 27 % of ecu-ams-udv's
samples.

**Tried, not adopted:**

- ADC3 reading mid-conversion without syncing the time (an upper bound of
  the time a sync would give, from tlib's unreported instructions and the
  CPU's residuum): exact, but slower on x86 (udv steady -11 %, CPU +9 %),
  not investigated further (a suspect: the cue then compares with a stale
  time and arms for waits too short to skip).
- Proving a store-free poll in one iteration instead of two: the uDV's
  `HAL_ADC_PollForConversion` enters its loop from different registers (the
  first pass is a transient), so it never applied.
- No time-bounded cue at all: ecu-ams-udv steady 0.96x → 0.73x; the
  per-conversion proof pays for itself.
- JIT settings: `DOTNET_TC_CallCountThreshold=1000` (boot +14 %, single
  boards' steady state -11 to -14 %), `DOTNET_TieredPGO=0` (boot +9-13 %,
  steady -12 %), .NET's default 100 ms call-counting delay (boot -30 %,
  steady -15 to -60 %).

**Firmware**: the uDV's transport spin is IFS09-DV-uDV#17 and its ADC3 use
IFS09-DV-uDV#18 (~5 % of its CPU polling on the car, and still its largest
peripheral traffic here: ~300,000 ADC3 accesses a second). Both are handled
exactly on the vHIL side; no firmware was changed.

### A plant on the co-simulation port (M8, #147)

The tractive-system plant ([`m8-plan.md`](m8-plan.md)) must react to the
AMS's coil drives within a few ms, and must not slow a run measurably
(budget: under 5 % host CPU per virtual second on ecu-ams). Measured on the
M-series host through Colima, ecu-ams after both apps are up, 3 s per point
(2026-10-16):

| How the plant would exchange | CPU s / virtual s | Wall s / virtual s |
|---|---|---|
| 100 ms slices, no plant | 0.71-0.82 | 0.43-0.49 |
| a `RunFor` and an edge query every 10 ms | 0.95-1.05 | 0.61-0.68 |
| a frame sent from the monitor every 10 ms | 3.75-4.12 | 13.0-13.4 |
| a frame sent from the monitor every 5 ms | 5.58-5.80 | 25.1-25.3 |

So a lock-step plant at the port's 10 ms step costs +20-30 % from the stops
alone, and sending each 0x466 from the monitor costs ~130 ms of wall time a
frame. The plant is event-driven instead (`vhil/cosim.py`, "Event-driven
plants"):
- The emulation runs until an armed coil pin changes (`emulation
  RunForUntilTrigger`, `models/renode/VhilRunner.cs`: RunFor's loop, one flag
  at each sync point).
- The closed-form plant hands the CAN probe the coming stretch of 0x466
  payloads in one call (`SchedulePeriodic`).

An arm-to-Run scenario stops for the plant 6 times in 11 s.

- **Identity**: a run with a trigger armed and none firing gives a bus
  timeline byte-identical to `RunFor`'s. The fingerprint (ecu, ams, ecu-ams,
  udv, ecu-ams-udv; 10 s) is unchanged through #311 and #314: `f66bae9a`,
  `e34419b8`, `8afef3de`, `f61019b8`, `a329f2fb`. A plant-driven scenario's
  trace is byte-identical run to run (11 of 11), and the same results come on
  CI's x86 runners.
- **Cost**: `ecu-ams/car-arms-through-the-ecu` plant-driven, against the same
  run with the plant off and a scripted 0x466 periodic of the same frame
  count, interleaved, 6 pairs, `VHIL_DOCKER_CPUS=4`. Median Renode CPU per
  virtual s: 1.130 plant-driven, 1.128 scripted (+0.1 %; median paired
  difference +0.5 %). Wall: 1.68 against 1.66. `scripts/speed.py --systems
  ecu-ams --paths sim`, dev against the branch, interleaved x 3: steady CPU
  0.78 / 0.89 / 0.73 against 0.82 / 0.74 / 0.74, noise. The speed path runs
  no plant.
- **The frames, not the plant**: any CAN probe frame cost ~11 ms of wall
  time on ecu-ams. A 10 ms periodic of any id, on either bus, took 100 ms
  slices from 0.48 to 1.57 wall s per virtual s and CPU +20 %, and the table's
  two rows of frames sent from the monitor include the same stall. It was not
  Renode's `TimeSourceBase.WaitIfBlocked` but a 100 ms host sleep in
  `CANTester.SendFrame`'s `Emulation.StartAll`, once per RunFor that sent
  anything; fixed in #312 (next section).

### A probe's frame: 100 ms of host time per run (#260)

A frame from the test probe (`VhilCanProbe`, models/renode/VhilProbe.cs)
cost far more host time than the bus traffic it adds. On ecu-ams after the
apps' start (M-series host through Colima, 8 vCPU, firmware at dev,
2026-10-16), host seconds per virtual second (wall, then Renode's CPU):

| | wall | CPU |
|---|---|---|
| 30 × `run_for(ms=100)` | 0.48 | 0.80 |
| the same with a probe `send_periodic` every 10 ms (`can_inv`, any id) | 1.57 | 0.97 |
| 100 × `run_for(ms=10)` | 0.58 | 0.95 |
| the same with a `send_batch` or `send_at` frame each step | 11-13 | 1.3-4.2 |

Wall time rose, CPU barely, so something slept. Sampling the state of every
Renode thread every 0.5 ms (`/proc/<pid>/task/*/stat`) showed all of them
asleep 68 % of the time with the periodic sender and 90 % with a frame a step,
in gaps of 100 ms that ended with the CPU threads waking. Renode's
time framework never blocked: a throwaway model counting the master's and
each board's time source's `BlockHook` saw none, so
`TimeSourceBase.WaitIfBlocked`'s 10 ms wait was not it.

The sleep was `Emulation.StartAll`'s `Thread.Sleep(100)`. The probe derives
from Renode's `CANTester`, and its sends went through `CANTester.SendFrame`,
which starts the emulation when `Emulation.IsStarted` is false. Under
`emulation RunFor` (every `vhil.sim` run, `Sim.run_for`) it always is:
`Emulation.RunFor` runs the master time source without setting it, and
pauses everything at the end. So the first frame the probe sent in a RunFor
called `StartAll` from the master's sync phase. That set `IsStarted` (later
frames of the same RunFor skipped it), found the time source already
running, and slept 100 ms on the master's thread while every board waited
for it. The next RunFor's `PauseAll` cleared `IsStarted` again: one 100 ms
stall per RunFor that sent anything. With the periodic sender that was 30
stalls in 3 virtual seconds (+1.0 wall-s per virtual s); with a frame a step,
100 in 1 s (+10).

The probe now re-implements `ICAN.FrameSent` and raises it itself; every
send already runs in a synced callback, while time runs, so nothing needs
starting. CANTester's own senders (`SendISOTPMessage`) still reach the bus
through its event, which the probe forwards. The same measurement after:
0.49 wall-s per virtual s with the periodic sender (the baseline's), 0.71-0.76
with a frame a step (the baseline's 0.58 plus the monitor round trip of the
send itself). `test_a_frame_costs_the_run_no_host_sleep`
(`tests/sim/test_probe.py`) sends ten frames over ten 10 ms steps and
requires under 0.8 s of host time; before, the sleeps alone took 1 s.

What a run computes is unchanged: the identity fingerprint is byte-identical
(ecu `f66bae9a`, ams `e34419b8`, ecu-ams `8afef3de`, udv `f61019b8`,
ecu-ams-udv `a329f2fb`, ecu-ams-ecu `4718c51b`, the references above), and
`systems/ecu-ams.scenarios/car-arms-through-the-ecu.yaml`, whose stimuli are
two 10 ms periodic senders, gives the same bus timelines (every frame, in ns),
samples, edges and radio payloads before and after (its trace differs only in
log lines' `wall_s`), all 17 expects passing. Its 8.5 virtual seconds took
13.2-13.8 s before and 6.7-7.1 s after, interleaved. `speed.py` sends nothing
from the probe, so it should not move, and doesn't beyond the host's noise:
three interleaved rounds (`--systems ams ecu ecu-ams --paths sim`), medians
before → after, boot / steady: ecu 2.57x / 3.92x → 2.58x / 4.12x, ecu-ams
1.93x / 2.27x → 1.95x / 2.28x, ams 2.32x / 3.81x → 2.87x / 4.94x; three more
rounds of ams alone, the fixed build first this time: 2.81x / 4.46x → 2.79x /
4.70x, with an outlier on each side (0.57x before, 2.93x after).

Upstream, this is Renode's: `CANTester.SendFrame` (and the other testers'
`StartAll` on a not-started emulation, e.g. `TerminalTester`, `LEDTester`)
should not start the emulation, and above all not sleep, when called on the
time source's thread while it runs, which `Emulation.IsStarted` doesn't
tell under `RunFor`. A fix there (check `TimeDomainsManager`'s current thread
or the master's state instead of `IsStarted`, or drop the sleep) is
upstreamable; the vHIL's doesn't need a patch to Renode.

Again on dev after M8 (#311-#315: the event-driven tractive-system plant,
whose inverter reports 0x466 from power-on through `SchedulePeriodic`), dev's
probe against the fix's, interleaved, on the M-series host (Colima, 8 vCPU)
and on one GitHub runner (x86, 4 vCPU), 2026-10-16. Host cost of probe
frames on ecu-ams after the apps' start (median of 3, wall s, Mac / x86):

| | before | after |
|---|---|---|
| 1 frame, one 10 ms step | 0.135 / 0.134 | 0.033 / 0.033 |
| 10 frames, one per 10 ms step | 1.11 / 1.06 | 0.070 / 0.045 |
| 100 frames, one per 10 ms step | 10.9 / 10.6 | 0.68 / 0.46 |
| 10 frames, one per 1 ms step | 1.04 / 1.03 | 0.024 / 0.017 |
| 3 s, 10 ms periodic, 100 ms slices | 4.78 / 4.02 | 1.43 / 0.95 |
| 3 s, 10 ms periodic, one `RunFor` | 1.72 / 1.04 | 1.40 / 0.90 |
| 3 s, no frames, 100 ms slices | 1.54 / 1.05 | 1.68 / 1.01 |

So ~100 ms per `RunFor` that sent anything, whatever the frame count, and
~0.3 ms per frame after (the monitor round trip of the send). Every committed
scenario (16), whole runs from power-on, wall s (Mac, 2 runs each side; x86,
2 runs, the seven listed): ecu-ams `car-arms-through-the-ecu` 11.0 virtual s
18.7-20.2 → 6.5-6.6 (x86 16.4 → 5.1), `rearm-refused-on-a-charged-link` 47.5
virtual s 81-86 → 27-28 (x86 69 → 21), `inverter-sense-floor-holds-the-
discharge` 70-79 → 23-26 (x86 60 → 18), the other ecu-ams ones 15-23 → 5-9;
ecu `heartbeat-r2d` (two 10 ms periodics) 8.0 → 2.6-3.3 (x86 6.4 → 2.0),
`as-emergency` 20.1 → 5.4; ams `tsms-precharge-run` 9.1 → 2.5-2.7 (x86 7.3 →
1.9). Runs an hour of `car-arms-through-the-ecu`, Renode start included: Mac
K=1 168 → 400; x86 K=1 181 → 427, K=2 335 → 618, K=4 552 → 731. Every
trace (bus frames, samples, edges, payloads) is byte-identical dev against fix,
run to run and Mac against x86, apart from log lines' `wall_s`, every expect
passing; the fingerprint is the references above on both; `speed.py` (no
probe frames) is unchanged on both hosts within noise (x86 steady ecu-ams
2.98-3.12x → 3.04-3.24x, ams 7.55-8.02x → 7.54-7.88x).

### Lock-step runs: the uDV's MMIO hook path (#260)

Host `perf` of `ecu-ams-udv` in steady state (dev at 12e2c00, patch set
vhil.9, M-series host through Colima, 4 vCPU, 15 s at 1999 Hz):

- **The uDV's CPU thread is the critical path.** It has 49 % of Renode's
  samples, is on-CPU about 88 % of the time and executing 83.5 % of it. The
  other boards mostly wait for it in the hand-off. The boards' busy stretches
  rarely overlap, so a multi-board run costs about the sum of its boards plus
  the hand-off's wake-ups.
- **Where the uDV thread goes.** Guest time is mostly `HAL_ADC_PollForConversion`
  (32 % of its host time). On the host, `tlib_read/write_double_word` are 30 %
  (the bus round trip of ADC3's registers, IFS09-DV-uDV#18). The fast-forward's
  memory-access hook is 19 %: `VhilFastForward.OnAccess` 15.2 % (a stopwatch
  2 %, `RestoreContext` 3 %, reading the PC ~4 %, the instruction total 1.7 %)
  and `TryTranslateAddress` 2.5 %. `ADC3.Now` → `SystemBus.TryGetCurrentCPU`
  (a reader-writer lock and a dictionary) is ~4 %.
- **The master thread** spins or waits ~70 % of its samples. Its sync phase
  is ~9 % and the futex wake-ups of `Release` ~11 %.
- **ecu-ams** is flat. The time framework and timers are 5-7 %, then the
  ECU's GPIO bit-bang (`NRF24_BitBangDelay`, 21 % of the ECU's guest time) and
  the counter-skip's block invalidation (2.4 % of the ECU thread). Each CPU
  thread spends ~21-23 % in the hand-off's spin. The AMS is idle 69 % of its
  guest time.

Two changes, both computing exactly what dev computes (identity fingerprint
byte-identical for all six systems):

1. **The detector locates an access only at an iteration's boundary**
   (`models/renode/VhilFastForward.cs`). While armed, every MMIO access used
   to restore the CPU context and read the PC and the instruction total.
   Now the hook's own PC is compared with the boundary first, and the context
   and the total (read straight from the CPUState) are taken only there. One
   call in 32 is timed. A throwaway check build compared the hook's PC with
   `cpu.PC` and the CPUState total with `GetCurrentInstructionsCount` in
   ~1.5 M accesses: 0 mismatches. The hook goes from 19 % to 10.7 % of the
   uDV thread (`OnAccess` 15.2 % → 7.4 %).
2. **Patches 0010-0011, two calls fewer per access** (patch set vhil.10).
   0010: `TryGetCurrentCPU` returns the bus access's initiating CPU from the
   thread-local context, without the lock. 0011: on Cortex-M,
   `OnMemoryAccess` skips the address translation unless an external MMU
   window has a nonzero addend (VhilExecuteNever's identity windows set
   none). A first 0011 that skipped it only with no external MMU at all
   never took effect, because VhilExecuteNever enables one. The hook is
   8.8 % of the uDV thread after both.

One GitHub runner (x86, 4 vCPU) building both Renode packages and
interleaving them, median of 3, dev → both changes (2026-10-30), boot /
steady:

| System | Path | Boot | Steady |
|---|---|---|---|
| udv | sim | 2.71x → 2.97x (+10 %) | 2.37x → 2.81x (+19 %) |
| udv | bench | 3.32x → 3.85x (+16 %) | 2.93x → 3.35x (+14 %) |
| ecu-ams-udv | sim | 1.52x → 1.58x (+4 %) | 1.27x → 1.39x (+9 %) |
| ecu-ams-udv | bench | 1.48x → 1.60x (+8 %) | 1.23x → 1.28x (+4 %) |
| ecu-ams | sim | 2.25x → 2.38x (+6 %) | 3.00x → 3.05x (+2 %) |
| ecu-ams | bench | 2.19x → 2.30x (+5 %) | 2.40x → 2.38x (-1 %) |
| ecu | sim | 3.46x → 3.67x (+6 %) | 7.16x → 6.96x (-3 %) |
| ams | sim | 3.61x → 3.71x (+3 %) | 6.96x → 7.01x (+1 %) |

CPU s/s fell on every uDV row (udv sim steady 0.578 → 0.508). The ECU and
the AMS alone arm the detector rarely, so they move within the runner's noise.
On the M-series host (4 vCPU, interleaved, median of 3, sim, steady):
ecu-ams-udv 0.96x → 1.11x, ecu-ams 1.91x → 2.19x, ams 5.04x → 5.17x, ecu
4.61x → 4.03x. The Mac's noise is about ±20 %, and an earlier run of the
detector alone had ecu at +1 %, so the ECU row is noise. Start-up is
unchanged: Renode ready / every app at 1.65 / 2.86 s → 1.78 / 2.94 s (ecu),
1.96 / 3.37 → 2.00 / 3.35 s (ecu-ams), 2.20 / 3.92 → 2.27 / 3.87 s
(ecu-ams-udv), median of 3, M-series host.

**A 1 ms quantum (measured, not done).** With `time.quantum_s: 0.001` on
every system (dev, M-series host, interleaved, median of 3, sim):

| System | Boot | Steady |
|---|---|---|
| ecu | 2.36x → 3.22x (+36 %) | 4.07x → 4.89x (+20 %) |
| ecu-ams | 1.91x → 2.33x (+22 %) | 2.23x → 2.49x (+12 %) |
| ecu-ams-udv | 1.40x → 1.84x (+31 %) | 1.12x → 1.40x (+25 %) |

That is half the hand-offs. But it changes what a run computes: every
digest of the five systems changed moves except udv's. The frame and edge counts stay the
same and the instruction counts at edges match. Cross-board timings move,
for example the ECU's frames land 131 µs earlier, because a frame between
boards can now lag by up to 1 ms. That is a fidelity decision (CAN latency
against speed) and a new identity reference, so it is left for its own
issue.

**Tried and dropped:**

- **Granting the critical board first** each quantum: 2-3 % at most.
- **A combining barrier** in which the last CPU to report runs the master's
  duties. It moves `SyncHook` and the delayed actions onto a CPU thread,
  which breaks the thread-identity assumptions they make.
- **A long spin on the master only.** It is close enough to the hand-off
  waiter above (no gain on x86, costs throughput) not to repeat it.
- **Reading IPSR through interrupt hooks** instead of a register read. It
  adds hooks on every exception entry and return, for ~1 %.
- **tlib's cached-access reads** for ADC3's registers. They change when a
  peripheral sees an access.
- **Skipping the counter-skip's block invalidation** on pages without
  translated code: ~1 % on the ECU.

What is left on the critical path is the bus round trip itself
(`tlib_read/write_double_word`, ~27 % + 7 % of the uDV thread). That is one
reverse P/Invoke per register access, which needs a Renode/tlib change (a
native fast path for plain registers), or fewer accesses from the uDV
(IFS09-DV-uDV#18).

## What is left

Host `perf` of Renode in lock-step steady state on the final build of this
round (patch set 1.17.0-vhil.3, ReadyToRun, the models' assembly), 20 s at
1999 Hz on the M-series host through Colima (aarch64), share of all of
Renode's samples (its CPU threads, the master time source's loop, which runs
on the monitor's thread, and the slave time sources):

| Where | ecu | ams | ecu-ams |
|---|---|---|---|
| The lock-step hand-off: CoreCLR's spin (one address in `libcoreclr`) and the kernel's scheduling and `sched_yield` | 25 % | 31 % | 16 % |
| The rest of CoreCLR (stubs, P/Invoke and reverse P/Invoke transitions, GC; no symbols) | 27 % | 17 % | 24 % |
| Managed code (Renode's bus, peripherals and time framework, the vHIL's models, the BCL) | 19 % | 29 % | 31 % |
| tlib's helpers and softmmu | 15 % | 10 % | 17 % |
| Translated guest code | 5 % | 6 % | 6 % |
| libc, the loader | 7 % | 7 % | 6 % |

The vHIL's own models are under 1 % on the AMS and ecu-ams; on the ECU the
fast-forward's counter-loop skips are ~4 % (their useful work, half of it
writing the counter back and invalidating the blocks there as a bus write
would). Renode's time framework and timers are 1.5-2 % in their own code (patch 0004).
What could still buy something, most first:

1. **The lock-step hand-off**, 16-31 %: each 500 us quantum a board's CPU
   thread and the master wait for each other spinning (`SemaphoreSlim.Wait` →
   `SpinWait`, `sched_yield`), and on a host running several runs the spin
   takes cores from the others. P1 (a blocking barrier) was slower on 4
   cores, granting several quanta at once is exact almost never, and a
   cheaper wait (a per-thread waiter, a longer spin while a core is spare)
   pays on Apple's hypervisor but not on x86 (above). Upstream Renode work. With several boards, most of it is the slave time sources'
   hop (above): 24-29 % of the host's samples on x86, 2N + 1 spinning threads
   for N boards, so three boards on 4 vCPUs oversubscribed. Since #288 each
   slave runs on the master's thread (patch 0006): N + 1 threads and one
   hand-off a quantum per board.
2. **CoreCLR outside the spin**, 17-27 %: mostly the transitions between tlib
   and C# (every MMIO access is a reverse P/Invoke into the bus, every CPU
   register read a P/Invoke the other way) and their stubs. Profiling it
   needs CoreCLR's symbols (the study had them); batching or avoiding
   transitions is a Renode/tlib change (P3/P5 took the allocations out, not
   the transitions).
3. **tlib's aarch64 softmmu** (Apple silicon): every load and store through a
   helper (`__ldl_mmu`, `__stl_mmu`: 2-3 % alone on the ECU); x86 has an
   inline TLB fast path. (While the detector is armed it no longer brings
   every RAM access to C#: tlib/0002, #302.)
4. **The fast-forward's skips on the ECU** (~4 %, vHIL side): a skip writes
   the counter back and orders the block invalidation a bus write would for
   that RAM; a page known to hold no translated code could skip it.
5. **Per `RunFor` and per monitor command** (several boards): a `RunFor`
   costs 0.54 ms on ecu and 0.68 ms on ecu-ams on top of its quanta (#284,
   #288), which a native test's 10 ms `run_until` steps pay every step; and a
   scenario slice sends three monitor commands per bus and two per board, one
   round trip each (they could go in one).
6. **The JIT at boot**: 0.4-0.8 CPU s per virtual s over a boot's first
   seconds, a quarter of a three-board boot's CPU; every run starts a fresh
   Renode (no warm starts). #308 promotes a method after 300 calls instead
   of 30 (0.24 CPU s per virtual s left over a three-board boot).
7. **The uDV's ADC3 traffic** (IFS09-DV-uDV#18): ~50 register accesses per
   single conversion, six conversions a millisecond, each through Renode's
   bus (`SystemBus` look-ups, reverse P/Invokes): what keeps the uDV at
   twice an ECU's cost per quantum, and so the board ecu-ams-udv waits for.
   Its hook path is cheaper since this round (the detector at the boundary,
   patches 0010-0011, above); the bus round trip itself is what remains.
8. Done: the uDV hook path (the detector at the boundary, patches 0010-0011); the uDV round (#297-#308, above); the slave time sources on the master's thread (#288), the AMS's CYCCNT waits fast-forwarded (#289); start-up (the models' assembly, ReadyToRun), the timer
   update events (patch 0004; its clock-source integer arithmetic, exact but
   with no measurable gain, was dropped in patch set vhil.4), the CMP.W hook
   only at delay loops (patch 0005 and tlib/0001).
