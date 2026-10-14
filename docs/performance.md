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
    [--systems ams ecu ecu-ams] [--paths bench sim] [--boot 3] [--steady 5] \
    [--baseline] [--repeat N] [--profile DIR] [--setup "vhil_ff_{board} Enabled false"]
```

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
tiering delay). `VHIL_DOCKER_CPUS=N` limits the container to N cores, as a CI
runner (GitHub's have 4). The `emulation speed` job of
[`sim.yml`](../.github/workflows/sim.yml) runs it with `--baseline` on every
full-ci pull request and every night, into the job summary and an artifact.

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
`catalog/platforms/stm32h733.yaml`), recognises three kinds of busy-wait and
charges their instructions instead of executing them:

- **Counter loops**: the `-O0` volatile counter delay of
  `NRF24_BitBangDelay` (`nop; ldr; adds #1; str; ldr; cmp.w #N; bcc`). A
  pre-opcode hook on CMP.W (immediate) decodes the code around it once per
  PC, exactly that shape or nothing, and checks its bytes again on every use.
  m iterations later the counter (register and memory) is i + m and the flags
  are those of the last ADDS; nothing else changed.
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
  event.
- **Time-bounded polls** (#260): a poll on a register computed from the time
  when read, whose model knows until when it stands (`VhilStableUntil`): ADC3,
  up to its next result, so `HAL_ADC_PollForConversion` (the AMS's
  `read_leg_q4`, 80 us a call). The read joins the whitelist above and a skip
  ends before that time, every skipped read before it with a margin for
  Renode's instruction-to-time rounding (time advances in steps of
  `mips / gcd(mips, ticks per us)` instructions: 231 on the ECU, 66 on the
  AMS). The model's `VhilOnRead` cue arms the detector when the poll starts.

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
  it stands (DWT CYCCNT, SysTick, a timer's CNT) or on a GPIO a device model
  drives.

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
same instruction. Lock-step runs only (`vhil.sim` and `speed.py`'s sim path
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
ecu, ams and ecu-ams from power-on into steady state with and without it and
compares every bus's timeline (offer, start, end, outcome, in ns), GPIO edges
with the executed-instruction count at each write, the radio's payloads, and
at the end each CPU's executed instructions, its registers and every RAM byte
(a SHA-256): identical, and the run with it must have skipped every kind (the
AMS's ADC3 polls included).

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

## What is left

Host `perf` of the CPU threads in lock-step steady state, on the patched
build: the vHIL's own models are a few percent of a board's CPU thread
(ADC3/DMA delivery on the AMS, the fast-forward's hooks on the ECU, the CAN
bus model), the rest Renode's, tlib's and .NET's. What only a change there
could still buy, most first:

1. **The lock-step hand-off.** Each 500 us quantum a board's CPU thread waits
   for the master and the master for it, spinning (`SemaphoreSlim.Wait` →
   `SpinWait`, `sched_yield`): ~14 % of the ECU's CPU thread and ~16 % of the
   master thread on one board; on ecu-ams the slave time sources spin for ~19 %
   of all samples. A board asleep in WFI pays it every quantum, and on a host
   running several runs the spinning takes cores from the others. The study's
   blocking barrier (P1) was slower on 4 cores; granting a board with nothing
   to synchronise several quanta at once would not be.
2. **`STM32_Timer`'s update event** reprograms its four compare-channel
   timers every period even when unused, and `BaseClockSource.Update` runs
   `Fraction` arithmetic (a GCD) per entry per update: the 1 kHz HAL
   timebase (TIM23/TIM6) was ~10 % of the ECU's CPU thread before P2.
3. **Managed transitions per MMIO access**: tlib → C# reverse P/Invoke for
   every peripheral access (the AMS's SPI/DMA, the ECU's GPIO bit-bang),
   20-30 % of a CPU thread before P3/P5.
4. **tlib's aarch64 softmmu** (Apple silicon): every load and store through a
   helper; and while the fast-forward's detector is armed, a C# call with an
   address translation per RAM access (~16 % of the ECU's boot there; x86
   never calls it for RAM).
5. **Hooks dispatched in C#**: the CMP.W pre-opcode hook reaches managed code
   for every CMP.W executed (~4 % of the ECU's CPU thread); a native filter
   by PC would leave only the delay loops'.
6. **Start-up**: Roslyn compiles the models in every process (1.4 s since
   #269) and a bare Renode is 445 MB of RSS; a cached plugin assembly and
   ReadyToRun images would cut both.
