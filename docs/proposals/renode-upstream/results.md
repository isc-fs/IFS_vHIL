# Results: the twelve PRs on upstream master

Base: renode master `9b5195e6`, renode-infrastructure master `3e4301cf` and
tlib master `0dd9a30c`, the heads on 2026-10-14 (PRs 01-07 were first
measured on `59f8f5d0` / `5474222a`; that round's numbers are replaced by
these). "all" is master with every PR (01-09 and 11 in
renode-infrastructure, its tlib submodule at master + 10, renode at master
+ 12), "prNN" master with PR NN alone (02 includes 01, 11 includes 10).
Built by [`tooling/build.sh`](tooling/build.sh) in
`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim` (Linux arm64 on an Apple
M-series Mac through Colima, 8 vCPUs, 7.7 GB), `./build.sh -t --no-gui`, the
portable headless package. Measured 2026-10-14, 18:00-24:00, with no other
emulation on the host (each run waited for that and checked it after,
[`tooling/idle.sh`](tooling/idle.sh): 4 of the 400 benchmark runs found
another container and were dropped).

## Renode's own tests

`renode-test` from the build tree, the list in
[`tooling/tests.txt`](tooling/tests.txt): every NUnit project
`tests/tests.yaml` lists (except options-parser's) and 47 Robot files
around the time framework, timers, the system bus (tags, locked and
per-core registrations, isolation, TrustZone state-aware look-ups), the
register framework, CPU and opcode hooks, execution tracing, pausing,
snapshots, logging, Cortex-M platforms (every STM32 platform suite: all use
`STM32_Timer`) and the demos. Counts from
[`tooling/summarize_tests.py`](tooling/summarize_tests.py).

| Suite | master | all |
|---|---|---|
| UnitTests | 621 passed, 6 skipped | 645 passed, 6 skipped |
| PeripheralsTests | 113 passed, 6 skipped | 113 passed, 6 skipped |
| RenodeTests | 276 passed, 6 skipped | 276 passed, 6 skipped |
| MonitorTests | 85 passed | 85 passed |
| Robot (47 files) | 422 passed, 5 skipped, 3 failed | 427 passed, 5 skipped, 2 failed |

- The 24 extra UnitTests are the series' own: 01's two, 02's and 04's one
  each, 09's 20 (three tests, parametrised). On master's code (the tests
  only) 01's two fail and the others pass: 01's are the bug's
  reproduction, 02's, 04's and 09's pin the behaviour the optimisations
  must keep (`tests09`: 641 passed).
- The 5 extra Robot passes: 12's four new cases and `ramn`'s "Should Run
  Without Watchdog Being Triggered", which failed on master. The failures
  are all in `tests/platforms/ramn.robot` and not the series': "ECUA Should
  Display Boot Menu" and "ECUA Should Display Actuators Changes" fail on
  master and on all alike (twice each), the watchdog case on master once;
  the 5 skips are `ramn`'s and two others' skip-on-failure cases, the same
  on both.
- 12's tests: 6 of 6 pass on pr11 and on all (the file's two existing
  cases and the four new ones); on master the four new ones fail (no such
  method). A first revision of 11, which left the invalidation of already
  translated blocks to the caller, failed its own benchmark (below) and
  the "Translated Code" case was added for it.

Every PR builds alone (packages pr02 to pr11), passes `dotnet format
whitespace --folder --verify-no-changes` (from renode's root, its
`.editorconfig`) on its changed C# files, and 10 passes `clang-format`
19.1.0 with tlib's `.clang-format`.

## The vHIL's identity check

[`../renode-patches/tooling/fingerprint.py`](../renode-patches/tooling/fingerprint.py)
runs ecu, ams and ecu-ams for 3.5 s of virtual time from power-on on the
lock-step path and records every bus timeline in ns, GPIO edges with the
instruction count at each write, radio payloads, each CPU's executed
instructions, PC, architectural state and RAM digest. master and all: the
same file byte for byte (SHA-1 `9092e539…`, 790 / 163 / 953 CAN frames,
28,411 / 1,125 / 27,862 edges), twice each, and again after 11's revision.
`VhilFastForward` would use 11's API on all (it looks the method up), but
its hook does not fire on upstream master (see the end of "Speed"), so this
compares 01-09 only; 11 is covered by its Robot tests and the hook
benchmark.

## PR 09's arithmetic against the Fraction path

[`tooling/clock-fuzz/`](tooling/clock-fuzz/) (`run.sh`) compiles master's
two update handlers and 09's, with master's `Fraction`, into one program and
runs both on 20 million random entries (realistic and arbitrary ratios,
residua with and without a denominator dividing q, values, periods and
times at and around 0, 1 and 2^31, both directions, one-shot and periodic)
and on 200,000 sequences of 200 updates evolving one entry on each path: 0
differences in the result, value, residuum (numerator, denominator, sign),
enabled flag or nearest limit; 7.8 million cases (39 %) took the integer
path. With a deliberately wrong rounding the same harness reports 2,176
differences in 200,000 cases.

## Speed

Each run in its own container on 4 cores (`--cpus 4 --cpuset-cpus 4-7`),
packages interleaved run by run. Host noise: the bus and register loops are
bimodal on this host (the same package takes about 4 s or about 6 s,
whichever host cores the VM's vCPUs land on), so a single PR's median moves
by up to ±20 % from noise alone there; the fastest run (min) is steadier.
Differences below that carry no claim. `stm32-timer` and the hook loops are
unimodal (±10 %).

### Generic workloads ([`tooling/bench/`](tooling/bench/))

Host seconds for 0.25 s of virtual time after a warm-up, and bytes
allocated per virtual second (.NET's `GC.GetTotalAllocatedBytes`):

- `bus-trivial`: upstream's `bus-performance.robot` loop, a Cortex-M4 at 100
  MIPS reading `TrivialPeripheral` (the bus access path alone);
- `register-read` / `register-write`: the same loop reading
  `STM32_GPIOPort`'s IDR / toggling one field of its OSPEEDR (bus path plus
  the register framework);
- `stm32-timer` (new): STM32F4's TIM2 (`STM32_Timer`, 10 MHz) with a 1 MHz
  update event and no channel in use, the Cortex-M4 spinning: the clock
  source and timer update path, about 250,000 update events;
- `hook-*` (new): the Cortex-M4 running a loop of 15 `nop`s and a branch,
  without a hook, with a pre-opcode execution hook on `nop` (C#,
  [`HookBench.cs`](tooling/bench/HookBench.cs)) called every time, or
  excluding itself at each address after its first call (PR 11);
- `demo-*`: Renode's `scripts/single-node/` demos, headless, 10-60 s.

**Per PR, n = 5 (median, its change in speed against master, fastest run,
allocation):**

| package | bus-trivial: median (speed), min, alloc GB/vs | register-read: median (speed), min, alloc GB/vs | register-write: median (speed), min, alloc GB/vs | stm32-timer: median (speed), min, alloc GB/vs |
|---|---|---|---|---|
| master | 5.00 s, 4.20, 32.8 | 6.29 s, 5.47, 39.4 | 6.07 s, 5.50, 45.3 | 3.26 s, 3.17, 9.0 |
| pr02 | 4.07 s (+23 %), 4.00, 32.8 | 5.70 s (+10 %), 5.46, 39.3 | 5.80 s (+5 %), 5.36, 45.3 | 1.83 s (+78 %), 1.38, 3.3 |
| pr03 | 3.45 s (+45 %), 2.78, 18.7 | 4.49 s (+40 %), 4.04, 25.2 | 5.32 s (+14 %), 4.46, 35.9 | 3.25 s (+1 %), 3.16, 9.0 |
| pr04 | 3.69 s (+36 %), 3.08, 19.9 | 4.67 s (+35 %), 4.51, 26.4 | 5.90 s (+3 %), 4.83, 36.8 | 3.74 s (-13 %), 3.38, 9.0 |
| pr05 | 6.26 s (-20 %), 4.47, 32.9 | 6.12 s (+3 %), 6.04, 39.4 | 5.89 s (+3 %), 5.70, 45.3 | 3.34 s (-2 %), 3.15, 9.0 |
| pr05old | 5.75 s (-13 %), 4.17, 32.9 | 6.91 s (-9 %), 5.90, 39.4 | 6.27 s (-3 %), 5.53, 45.3 | 3.71 s (-12 %), 3.40, 9.0 |
| pr06 | 6.15 s (-19 %), 4.48, 32.9 | 5.65 s (+11 %), 5.06, 34.5 | 4.85 s (+25 %), 4.15, 28.2 | 3.44 s (-5 %), 3.21, 9.0 |
| pr07 | 4.17 s (+20 %), 3.88, 31.6 | 7.06 s (-11 %), 5.39, 38.2 | 6.21 s (-2 %), 5.27, 44.5 | 3.38 s (-3 %), 3.16, 8.9 |
| pr08 | 4.15 s (+20 %), 4.00, 32.8 | 6.04 s (+4 %), 5.37, 39.3 | 5.98 s (+1 %), 5.46, 45.3 | 2.60 s (+26 %), 2.31, 6.5 |
| pr09 | 6.11 s (-18 %), 4.18, 32.9 | 5.62 s (+12 %), 5.52, 39.3 | 6.07 s (-0 %), 5.48, 45.3 | 3.43 s (-5 %), 3.14, 9.0 |
| pr11 | 4.35 s (+15 %), 3.96, 32.8 | 6.68 s (-6 %), 5.56, 39.3 | 5.68 s (+7 %), 5.38, 45.3 | 3.21 s (+2 %), 3.17, 9.0 |
| all | 1.81 s (+176 %), 1.45, 4.6 | 2.80 s (+124 %), 2.51, 6.3 | 3.18 s (+91 %), 2.51, 9.4 | 1.24 s (+164 %), 1.11, 1.5 |

n = 5 per cell

pr11 and all here are 11's first revision; the loops never call the
exclusion API, which is all the revision changed.

What is beyond the noise:

- **02** on `stm32-timer`: 1.78x (every clock entry look-up of the timer's
  update, `Limit`/`Enabled`/`Direction` writes, was a linear delegate
  search); allocation -63 %.
- **03 and 04** on the bus loops: bus read 1.36-1.45x, register read
  1.35-1.40x (fastest runs 1.3-1.5x), allocation -33 to -43 % each.
- **06** on `register-write`: 1.25x, allocation -38 %.
- **08** on `stm32-timer`: 1.26x, allocation -28 %.
- **all**: bus read 2.76x, register read 2.24x, register write 1.91x,
  `stm32-timer` 2.64x as fast as master; allocation per bus read 656 → 92 B,
  register write 1.36 kB → 280 B, `stm32-timer` -83 %.
- 05, 07, 09 and 11 alone: within the noise on these loops (05 removes a
  lock, not allocations; 07's allocations are small here; 09 and 11 below).

**Leave-one-out, n = 7** (all, and all without one PR, or with 05's first
revision; change against all, negative = the PR helps):

| package | bus-trivial: median (speed), min, alloc GB/vs | register-read: median (speed), min, alloc GB/vs | stm32-timer: median (speed), min, alloc GB/vs |
|---|---|---|---|
| all | 1.78 s, 1.66, 4.6 | 2.78 s, 2.48, 6.3 | 1.29 s, 1.16, 1.5 |
| all-no05 | 2.03 s (-12 %), 1.70, 4.6 | 2.97 s (-7 %), 2.76, 6.3 | 1.33 s (-3 %), 1.15, 1.5 |
| all-05old | 1.66 s (+7 %), 1.41, 4.6 | 2.60 s (+7 %), 2.39, 6.3 | 1.33 s (-3 %), 1.15, 1.5 |
| all-no08 | 1.74 s (+2 %), 1.40, 4.6 | 2.62 s (+6 %), 2.38, 6.3 | 1.73 s (-25 %), 1.51, 3.2 |
| all-no09 | 2.58 s (-31 %), 1.57, 4.6 | 2.60 s (+7 %), 2.51, 6.3 | 1.30 s (-1 %), 1.15, 1.5 |

n = 6, 7 per cell

- **08** is 25 % of the timer workload's speed within the whole series
  (all without it: 1.73 s instead of 1.29 s, allocation 3.2 instead of 1.5
  GB/vs).
- **09**: no measurable host-time effect, alone or within the series
  (-1 % on `stm32-timer`; the `bus-trivial` -31 % is the bimodal noise, its
  fastest run is 1.57 s against 1.66 s). Its gain is below this host's
  resolution: the vHIL's profile put it, with 08, at about 1 point of host
  samples (`BaseClockSource.Advance` 4.8 → 3.8 % and 6.6 → 5.3 %), and the
  timer workload here is dominated by the timer's own update work.
- **05**: removing it costs 7-12 % on the loops; its first revision
  (thread-held entry) and the current one (dictionary-owned, weakly held)
  are within ±7 % of each other, i.e. the weak reference costs nothing
  measurable.

**PR 11, the hook (median of 5):**

| package | no hook | hook on every `nop` | hook excluding itself |
|---|---|---|---|
| master | 0.027 s | 2.49 s (13.7 GB/vs allocated) | - (no API) |
| all | 0.023 s | 0.68 s (0.05 GB/vs) | **0.022 s** |

With the exclusion, the hook's cost is gone: it is called during the
warm-up's first execution chunk only, instead of about 23 million times
(15 of every 16 instructions), and the loop runs as fast as without a hook (31x as fast as with
it on the same build). The hook on every `nop` is itself 3.7x cheaper on
all than on master: 07 formats the hook handler's noisy log message only
when it is logged. The first revision of 11 left the invalidation of the
blocks already translated to the caller (`InvalidateTranslationBlocks`,
which nothing calls by itself): the hook kept being called, now through a
failing exclusion each time, and this benchmark took 3.6-5.7 s; 11 now
invalidates them itself before the next execution chunk.

**Renode's demos, master vs all, n = 5:**

| demo | master | all | allocation |
|---|---|---|---|
| `stm32f4_discovery` (10 s) | 13.72 s | 13.99 s (-2 %) | 55 → 56 MB/vs |
| `nucleo_h753zi_hubris` (60 s) | 4.59 s | 3.80 s (+21 %; fastest runs 3.36 / 3.44 s) | 28 → 24 MB/vs |
| `nrf52840` (60 s) | 2.19 s | 2.06 s (+6 %) | 12 → 12 MB/vs |

No measurable change, as expected: the demos allocate 600-3,800 times less
per virtual second than the loops, i.e. spend almost no time in the paths
the series changes (they are idle or in translated code).

### The vHIL's systems (scripts/speed.py)

Real-time factor (virtual s per host s); boot = first 3 s from power-on,
steady = the 5 s after. Firmware: the study's pinned images (ECU dev
44610a5, AMS dev 91d49c9, CAN bootloader v1.7.0). The counter-loop hook of
`VhilFastForward` does not fire on upstream master (below), on either side.

**master vs all, both paths, n = 5:**

| system | path | phase | master | all |
|---|---|---|---|---|
| ams | bench | boot | 1.50x n=5 | 1.79x (+20 %) n=5 |
| ams | bench | steady | 2.20x n=5 | 3.02x (+37 %) n=5 |
| ams | sim | boot | 1.25x n=5 | 1.57x (+25 %) n=5 |
| ams | sim | steady | 3.12x n=5 | 5.07x (+62 %) n=5 |
| ecu | bench | boot | 1.48x n=5 | 1.61x (+9 %) n=5 |
| ecu | bench | steady | 1.40x n=5 | 2.11x (+50 %) n=5 |
| ecu | sim | boot | 1.33x n=5 | 1.52x (+14 %) n=5 |
| ecu | sim | steady | 1.63x n=5 | 1.87x (+15 %) n=5 |
| ecu-ams | bench | boot | 0.96x n=5 | 1.14x (+18 %) n=5 |
| ecu-ams | bench | steady | 0.95x n=5 | 1.20x (+27 %) n=5 |
| ecu-ams | sim | boot | 0.80x n=5 | 0.88x (+9 %) n=5 |
| ecu-ams | sim | steady | 1.05x n=5 | 1.24x (+18 %) n=5 |

**Per PR, sim path, n = 3** (each PR alone against master, interleaved;
this host's run-to-run noise on these is about ±10 %):

| system | path | phase | master | all | pr02 | pr03 | pr04 | pr05 | pr06 | pr07 | pr08 | pr09 | pr11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ams | sim | boot | 1.00x n=3 | 1.56x (+56 %) n=3 | 1.05x (+5 %) n=3 | 1.27x (+27 %) n=3 | 1.22x (+22 %) n=3 | 1.16x (+16 %) n=3 | 1.25x (+25 %) n=3 | 1.21x (+21 %) n=3 | 1.23x (+23 %) n=3 | 1.10x (+10 %) n=3 | 1.21x (+22 %) n=3 |
| ams | sim | steady | 2.85x n=3 | 4.70x (+65 %) n=3 | 2.61x (-9 %) n=3 | 3.02x (+6 %) n=3 | 3.06x (+7 %) n=3 | 2.75x (-4 %) n=3 | 3.22x (+13 %) n=3 | 3.00x (+5 %) n=3 | 3.22x (+13 %) n=3 | 2.71x (-5 %) n=3 | 2.81x (-1 %) n=3 |
| ecu | sim | boot | 1.25x n=3 | 1.44x (+15 %) n=3 | 1.19x (-5 %) n=3 | 1.12x (-11 %) n=3 | 1.17x (-7 %) n=3 | 1.13x (-10 %) n=3 | 1.33x (+6 %) n=3 | 1.31x (+5 %) n=3 | 1.02x (-19 %) n=3 | 1.34x (+7 %) n=3 | 1.33x (+6 %) n=3 |
| ecu | sim | steady | 1.52x n=3 | 1.87x (+23 %) n=3 | 1.52x (-0 %) n=3 | 1.40x (-8 %) n=3 | 1.42x (-7 %) n=3 | 1.38x (-10 %) n=3 | 1.58x (+4 %) n=3 | 1.57x (+3 %) n=3 | 1.01x (-34 %) n=3 | 1.57x (+3 %) n=3 | 1.54x (+1 %) n=3 |
| ecu-ams | sim | boot | 0.75x n=3 | 0.80x (+6 %) n=3 | 0.61x (-19 %) n=3 | 0.65x (-14 %) n=3 | 0.75x (-0 %) n=3 | 0.71x (-5 %) n=3 | 0.81x (+8 %) n=3 | 0.76x (+1 %) n=3 | 0.73x (-3 %) n=3 | 0.77x (+2 %) n=3 | 0.78x (+4 %) n=3 |
| ecu-ams | sim | steady | 0.98x n=3 | 1.08x (+11 %) n=3 | 0.93x (-5 %) n=3 | 0.86x (-12 %) n=3 | 0.97x (-1 %) n=3 | 0.94x (-3 %) n=3 | 1.01x (+4 %) n=3 | 0.98x (+0 %) n=3 | 0.94x (-3 %) n=3 | 0.96x (-2 %) n=3 | 1.00x (+2 %) n=3 |

Single PRs are not resolved here: some runs drop to 0.9-1.0x whatever the
package (master's first ecu run, pr03's second, pr08's first and third,
pr11's third: the host's slow phases), which a median of 3 does not absorb,
and single PRs move these systems by less than that. What stands out is
all (ams +56 to +65 %, ecu +15 to +23 %, ecu-ams +6 to +11 %, the last
also at n = 5 above). The per-PR effects are measured on the generic
loops above instead.

**The vHIL's `CMP.W` hook does not fire on upstream master.** Every run
here, master and all alike, reports 0 calls of `VhilFastForward`'s
counter-loop hook (`speed.py`'s `fast_forward` field): upstream changed what
opcode hooks see for 32-bit Thumb-2 instructions (tlib `ff1bb507`,
renode-infrastructure `c47c11a6`, 2026-09-25: the whole instruction word,
first halfword in the upper 16 bits, instead of the first halfword), so the
hook's mask (`0xFBF0` / `0xF1B0`, the first halfword) no longer matches. So
these vHIL numbers exclude counter-loop skipping on both sides, and they and
the fingerprint do not exercise 11; the Robot tests and the hook benchmark
above do. For the vHIL this is a bump item: on the first release with that
change, `VhilFastForward` must match the new encoding (mask `0xFBF00000`,
value `0xF1B00000`) or it silently loses its counter-loop skips.

## Profile (the evidence in the PRs)

Host `perf` on the vHIL's two-machine STM32H7 workload (ecu-ams, sim path,
steady state, Renode 1.17.0 from source; the study's
[`../renode-patches/tooling/`](../renode-patches/tooling/) `profile-variant.sh` and `stacks.py`):

| Where | stock | with the patches |
|---|---|---|
| clock-entry look-ups (`Misc.IndexOf`, `Delegate.Equals`) | 5.5 % of samples | 0.1 % |
| allocation | 6.3 % | 3.4 % |
| garbage collection | 4.8 % | 1.9 % |

And for 08 + 09 (local patch 0004, #279; ecu / ams, sim, steady state,
inclusive): `BaseClockSource.Advance` 4.78 → 3.76 % / 6.59 → 5.30 %,
`STM32_Timer`'s update event 2.08 → 1.26 % / 2.82 → 1.92 %; for 10 + 11
(local 0005 + tlib/0001, #281): the AMS's managed hook calls in 8 s of
virtual time 258,777 → 472, `HandlePreOpcodeExecutionHook` 1.0 % of samples
→ below 0.02 %.

## Raw data

Kept outside the repository (Docker volumes and
`.claude/worktrees/renode-bench/results/r2/`): `loops.jsonl`, `lou.jsonl`,
`hooks.jsonl`, `demos.jsonl` (bench.sh), `r2-ab/`, `r2-perpr/` (speed.py,
one file per run), `fp-*.json`, `fp2-*.json` (fingerprints), and the test
results in `renode-up-pkgs:/<package>.tests/`. [`tooling/`](tooling/)
reproduces them (README, "Re-running the measurements").
