# Results: the seven PRs on upstream master

Base: renode master `59f8f5d0` + renode-infrastructure master `5474222a`
(2026-10-12; still the heads on 2026-10-14). "all" is master with the seven
PRs (renode-infrastructure `b935ecc5`), "prNN" master with PR NN alone (02
includes 01). Built by [`tooling/build.sh`](tooling/build.sh) in
`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim` (Linux arm64 on an Apple
M-series Mac through Colima, 8 vCPUs, 7.7 GB), `./build.sh -t --no-gui`, the
portable headless package.

## Renode's own tests

`renode-test` from the build tree, the list in [`tooling/tests.txt`](tooling/tests.txt): every
NUnit project `tests/tests.yaml` lists (except options-parser's) and 38
Robot suites around the time framework, the system bus (tags, locked and
per-core registrations, isolation, TrustZone state-aware look-ups), the
register framework, CPU and opcode hooks, pausing, snapshots, logging,
Cortex-M platforms and the demos.

| Suite | master | all |
|---|---|---|
| UnitTests | 621 passed, 6 skipped | 625 passed, 6 skipped |
| PeripheralsTests | 113 passed, 6 skipped | 113 passed, 6 skipped |
| RenodeTests | 276 passed, 6 skipped | 276 passed, 6 skipped |
| MonitorTests | 85 passed | 85 passed |
| 38 Robot suites | 330 passed, 1 skipped | 330 passed, 1 skipped |

The four extra UnitTests are the series' own. On master's code (the tests
only, `tmp/new-tests-on-master`) PR 01's two fail
(`ShouldRemoveTheGivenEntryWhenAHandlerRemovesAnEntryBeforeIt`,
`ShouldKeepOtherEntriesWhenAHandlerRunByGetClockEntryRemovesAnEntry`) and PR
02's and 04's pass: 01's are the bug's reproduction, 02's and 04's pin the
behaviour the optimisations must keep.

Every PR builds alone (packages pr01 to pr07) and passes
`dotnet format whitespace --folder --verify-no-changes` on the nine changed
files (checked to fail on a mis-indented line).

## The vHIL's identity check

[`../renode-patches/tooling/fingerprint.py`](../renode-patches/tooling/fingerprint.py)
runs ecu, ams and ecu-ams for 3.5 s of virtual time from power-on on the
lock-step path and records every bus timeline in ns, GPIO edges with the
instruction count at each write, radio payloads, each CPU's executed
instructions, PC, architectural state and RAM digest. master and all: the
same file byte for byte (SHA-1 `4cbd307c…`, 790 / 163 / 953 CAN frames,
28 411 / 1 125 / 27 862 edges), twice each.

## Speed

Each run in its own container on 4 cores (`--cpus 4 --cpuset-cpus 4-7`),
packages interleaved run by run, median of n; measured 2026-10-14 around
03:00-03:45. Host noise under Colima is about ±20 % run to run
(docs/performance.md); the differences below that carry a claim are beyond
it.

### The vHIL's systems (scripts/speed.py, n=5)

Real-time factor (virtual s per host s); boot = first 3 s from power-on,
steady = the 5 s after. Firmware: the study's pinned images (ECU dev 44610a5, AMS dev 91d49c9, CAN
bootloader v1.7.0).

| system | path | phase | master | all |
|---|---|---|---|---|
| ams | bench | boot | 1.14x | 1.33x (+17 %) |
| ams | bench | steady | 2.28x | 3.03x (+33 %) |
| ams | sim | boot | 1.01x | 1.31x (+30 %) |
| ams | sim | steady | 2.65x | 4.06x (+53 %) |
| ecu | bench | boot | 1.17x | 1.28x (+9 %) |
| ecu | bench | steady | 1.70x | 2.14x (+26 %) |
| ecu | sim | boot | 1.21x | 1.35x (+12 %) |
| ecu | sim | steady | 1.58x | 1.99x (+26 %) |
| ecu-ams | bench | boot | 0.84x | 0.96x (+14 %) |
| ecu-ams | bench | steady | 0.96x | 1.17x (+22 %) |
| ecu-ams | sim | boot | 0.77x | 0.92x (+20 %) |
| ecu-ams | sim | steady | 1.00x | 1.13x (+13 %) |

Per patch, the study measured the local queue on 1.17.0 (same code but for
the review changes listed in the README; n=3, steady state):
clock source (01+02) +6 to +42 %, system bus (03+04+05) +4 to +51 %,
allocations (06+07) +4 to +25 %
([`../renode-fork-study.md`](../renode-fork-study.md#measured-gains)).

### Generic workloads ([`tooling/bench/`](tooling/bench/), n=5)

Host seconds for the measured virtual time (after a warm-up), and bytes
allocated per virtual second (.NET's `GC.GetTotalAllocatedBytes`).

- `bus-trivial`: upstream's `bus-performance.robot` loop, a Cortex-M4 at 100
  MIPS reading `TrivialPeripheral` (the bus access path alone), 0.25 s;
- `register-read` / `register-write`: the same loop reading
  `STM32_GPIOPort`'s IDR / toggling one field of its OSPEEDR (bus path plus
  the register framework), 0.25 s;
- `demo-*`: Renode's `scripts/single-node/` demos, headless, 10-60 s.

| benchmark | master | all | master alloc | all alloc |
|---|---|---|---|---|
| bus-trivial | 4.14 s | 1.74 s (2.38x as fast) | 32.8 GB/vs (656 B per read) | 4.6 GB/vs (92 B per read, -86 %) |
| register-read | 5.76 s | 3.40 s (1.69x) | 39.3 GB/vs (786 B per read) | 6.3 GB/vs (127 B, -84 %) |
| register-write | 5.59 s | 2.51 s (2.23x) | 45.3 GB/vs (1.36 kB per write) | 9.3 GB/vs (280 B, -79 %) |

Allocation by PR alone (deterministic: spread under 2.5 % for the loops), GB
per virtual second, change against master:

| benchmark | master | pr02 | pr03 | pr04 | pr05 | pr06 | pr07 |
|---|---|---|---|---|---|---|---|
| bus-trivial | 32.9 | 32.8 (0 %) | 18.8 (-43 %) | 20.1 (-39 %) | 32.9 (0 %) | 32.7 (0 %) | 31.5 (-4 %) |
| register-read | 39.3 | 39.5 (0 %) | 25.3 (-36 %) | 26.5 (-33 %) | 39.3 (0 %) | 34.6 (-12 %) | 38.2 (-3 %) |
| register-write | 45.3 | 45.3 (0 %) | 35.9 (-21 %) | 36.7 (-19 %) | 45.3 (0 %) | 28.1 (-38 %) | 44.6 (-2 %) |
| demo-stm32f4_discovery | 0.055 | 0.051 | 0.053 | 0.052 | 0.055 | 0.056 | 0.047 (-13 %) |
| demo-nucleo_h753zi_hubris | 0.027 | 0.027 | 0.026 (-3 %) | 0.027 | 0.027 | 0.027 | 0.026 (-4 %) |
| demo-nrf52840 | 0.012 | 0.012 | 0.012 | 0.012 | 0.012 | 0.012 | 0.012 (-5 %) |

PR 02 and 05 allocate nothing less on these: 02's cost is in clock-entry
look-ups (timers, `ScheduleAction`), which the loops don't use, and 05
removes a lock and dictionary look-ups, not allocations.

**Not measured cleanly: host time on the demos, and host time per PR.** The
per-PR and demo runs (n=3, `generic-perpr.jsonl`) overlapped other
emulation jobs on the same host: their wall times spread by up to ±50 %
(one register-read run took 5 hours), so they are not reported. During this
work another agent kept the host's 8 vCPUs busy back to back, so they could
not be repeated. What they do show is deterministic: the demos allocate
0.01-0.06 GB per virtual second, 600-3,800 times less than the loops, i.e.
they spend almost no time in the paths the series changes (they are idle or
in translated code most of the time), so little speed change is expected
there. To measure on a quiet host (about 45 minutes):

```sh
$T/bench.sh $WORK/results/demos.jsonl 5 "master all" "demo-stm32f4_discovery demo-nucleo_h753zi_hubris demo-nrf52840"
$T/bench.sh $WORK/results/perpr.jsonl 5 "master pr02 pr03 pr04 pr05 pr06 pr07" "bus-trivial register-read register-write"
$T/vhil-ab.sh speed perpr 3 "master pr02 pr03 pr04 pr05 pr06 pr07"
```

## Profile (the evidence in the PRs)

Host `perf` on the vHIL's two-machine STM32H7 workload (ecu-ams, sim path,
steady state, Renode 1.17.0 from source; the study's
[`../renode-patches/tooling/`](../renode-patches/tooling/) `profile-variant.sh` and `stacks.py`):

| Where | stock | with the patches |
|---|---|---|
| clock-entry look-ups (`Misc.IndexOf`, `Delegate.Equals`) | 5.5 % of samples | 0.1 % |
| allocation | 6.3 % | 3.4 % |
| garbage collection | 4.8 % | 1.9 % |

## Raw data

Kept outside the repository (Docker volumes and
`.claude/worktrees/renode-bench/results/`): `generic-ab.jsonl` (the A/B
above), `generic-perpr.jsonl` (per PR), `vhil-ab/*.json` (speed.py, one
file per run), `fp/*.json` (fingerprints). [`tooling/`](tooling/) reproduces
them (README, "Re-running the measurements").
