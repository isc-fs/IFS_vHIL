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
`catalog/platforms/stm32h733.yaml`), recognises two kinds of busy-wait and
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
  found them, are a fixed point: the next iterations repeat them until an
  event.

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
  is computed from the time when read (DWT CYCCNT, SysTick, a timer's CNT,
  ADC3's lazily computed conversions) or on a GPIO a device model drives.

[`tests/sim/test_fast_forward.py`](../tests/sim/test_fast_forward.py) runs
ecu, ams and ecu-ams from power-on into steady state with and without it and
compares every bus's timeline (offer, start, end, outcome, in ns), GPIO edges
with the executed-instruction count at each write, the radio's payloads, and
at the end each CPU's executed instructions, its registers and every RAM byte
(a SHA-256): identical, and the run with it must have skipped both kinds.

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

## What is left

The speed-ups take the boot from the bootloader's polling to the apps' own
start. What remains is Renode's cost per peripheral access and per timer
event (the managed transitions and the time framework above), and, for two
boards, the lock-step barrier at every sync point. Neither is something the
vHIL's models can skip without changing what the firmware sees. #260 tracks
them, with the candidates that are this repo's: ADC3's lazily computed
conversions, which keep `HAL_ADC_PollForConversion` from being
fast-forwarded, and the CAN bus model's wakes on a single-board bus.
