# IFS_vHIL — vision

**Status:** agreed direction, 2026-10-01. Phase 1 ([#1](https://github.com/isc-fs/IFS_vHIL/issues/1))
proved the foundation: unmodified firmware on emulated silicon passes the
physical bench's own tests. This document says where that foundation goes.
The Phase 0/1 design and findings stay in [`proposal.md`](proposal.md).

## 1. From a bench to a workbench

[IFS_HIL](https://github.com/isc-fs/IFS_HIL) is bounded by its hardware:
one backplane PCB, four board slots, the stimulus that happens to be
wired, and one bench that someone has to keep alive. The set of hardware and
software combinations it can test is small and fixed.

IFS_vHIL removes that bound. A *system* is data: which boards exist, which MCU
each one carries, which firmware each one runs (pulled from its own
repository), and how they are wired together. Engineers compose systems in a
**shared web app**, run them, watch and stimulate them, and test them. That
covers combinations that do not exist physically yet, such as a new ECU
revision before its PCB is ordered, and many in parallel.

The physical bench remains the final word on what emulation cannot model:
analog accuracy, bus physics, power and flash wear. The vHIL filters what
reaches it.

## 2. Concepts

| Concept | What it is | Example |
|---|---|---|
| **Platform** | An emulatable MCU or SoC: its memory map and peripheral models. The only layer that knows about a CPU architecture. | `stm32h733` ([`platforms/cpus/stm32h733.repl`](../platforms/cpus/stm32h733.repl)) |
| **Chip model** | An external component on a board, behind a bus the platform exposes. | LTC6820 + LTC6811 chain on SPI1; BMI088 on I²C2 |
| **Board** | A platform plus its chip models plus named pins and connectors. User-definable. | MainLite (STM32H733, 3× FDCAN, ADC3 inputs; AMS, ECU or uDV by backplane, whose routing is reference documentation in [`backplanes/`](backplanes/)) |
| **Firmware source** | Repository + ref + build recipe for one board's image. Never an emulator-only build. | `isc-fs/IFS08-CE-ECU@dev`, IFS_HIL's `configs/firmware/ecu.yaml` |
| **System** | Boards placed, each with its firmware, plus the nets between them: CAN buses, wires, analog lines. Stored in a git-versionable file. | ECU + AMS + uDV on a shared ACU bus |
| **Plant** | Something outside the electronics that the system senses and drives, connected through a co-simulation port. | Scripted inverter, cell voltages; later MingoCIL |
| **Scenario / suite** | Stimulus and assertions against a system, in virtual time. IFS_HIL's pytest suites run unmodified in a compatibility mode. | ECU `smoke`; "precharge with a cell open" |

The catalogue of platforms, chip models and boards is what bounds the vHIL
now. Every new MCU or chip needs a model, so models have to be cheap to write,
testable on their own, and shared.

## 3. Principles

1. **The system file is the source of truth, not the UI.** The web app edits
   it, CI runs it, a PR can change it. Renode scripts are *generated* from it.
   Today's hand-written `scripts/ecu.resc` and `configs/vbench.yaml` are the
   first things to generate.
2. **Virtual time is the contract.** One Cortex-M7 runs at about real time on a
   desktop; a full car will not, and SoCs are far slower. New tests and the
   UI's timelines speak virtual time, so a run is deterministic whether it
   goes faster or slower than reality. Wall-clock pacing exists only to run
   IFS_HIL's legacy suites, and Phase 1 showed how fragile it is.
3. **Real firmware, modelled hardware.** Never patch or rebuild firmware for
   the emulator. Fix the model.
4. **Gaps are explicit.** Unmodelled hardware the firmware touches fails a run
   unless explained ([`configs/peripherals.yaml`](../configs/peripherals.yaml)).
   Tests a system cannot pass are skipped with a reason
   ([`configs/gaps.yaml`](../configs/gaps.yaml)).
5. **Backend-agnostic above the platform layer.** Renode today. The system
   model, runtime API and UI must not assume STM32, or even Renode, because
   other MCU families and Linux SoCs are coming.
6. **Plants live outside.** The vHIL exposes a co-simulation port. Vehicle
   physics belongs to **MingoCIL** (car in the loop), which will drive
   IFSSIM through that port.

## 4. Architecture

```
            ┌──────────────────────── shared web app ────────────────────────┐
            │ system editor (boards, buses, wires) · firmware picker (repo/ref)│
            │ live bus + signal views · scenarios · run history                │
            └───────────────┬───────────────────────────────▲──────────────────┘
                            │ system file (git)              │ results, traces
        ┌───────────────────▼────────────┐        ┌──────────┴──────────────┐
        │ generator                      │        │ runtime API             │
        │ system → emulator scripts,     │───────▶│ run / step virtual time │
        │ hubs, wires; builds firmware   │        │ inject · observe · trace│
        └───────────────────┬────────────┘        └──────────┬──────────────┘
                            │                                │
        ┌───────────────────▼────────────────────────────────▼──────────────┐
        │ workers (Linux): Renode, one machine per board, CAN hubs, vcan,   │
        │ co-simulation port ◀──▶ plants (scripted now; MingoCIL later)     │
        └───────────────────────────────────────────────────────────────────┘
        catalogue: platforms · chip models · boards  (versioned, tested)
```

- **Workers** are Linux, so SocketCAN, `vcan` and pacing behave as they do in
  CI. Engineers do not need a local toolchain or WSL kernel modules to use the
  vHIL, only to develop it.
- **Firmware access** goes through a GitHub App, not personal tokens: the same
  fix HANDOVER.md recommends for IFS_HIL's CI.
- **IFS_HIL compatibility** stays. The virtual broker and pytest plugin keep
  the physical bench's suites running, and IFS_HIL can route runs here
  ([#3](https://github.com/isc-fs/IFS_vHIL/issues/3)).

## 5. Roadmap

| Milestone | Deliverable | Done when |
|---|---|---|
| **M1: system description** ([#10](https://github.com/isc-fs/IFS_vHIL/issues/10)) | Schema for boards, firmware sources and nets; generator to Renode scripts | Today's ECU setup is *generated*, and IFS_HIL ECU smoke still gives 14 passed |
| **M2: AMS chip model** ([#2](https://github.com/isc-fs/IFS_vHIL/issues/2)) | LTC6820/LTC6811 chain as the first catalogue chip model | AMS boots out of Error with seeded cells; AMS smoke passes |
| **M3: multi-node systems** ([#11](https://github.com/isc-fs/IFS_vHIL/issues/11)) | ECU + AMS in one system on a shared ACU bus, no simulators | IFS_HIL's F-real-AMS tests leave `gaps.yaml` |
| **M4: runtime API, virtual-time tests** ([#12](https://github.com/isc-fs/IFS_vHIL/issues/12)) | Start, step, inject, observe over an API; a test library in virtual time | A scenario runs deterministically at any emulation speed |
| **M5: shared web app** ([#13](https://github.com/isc-fs/IFS_vHIL/issues/13)) | Workers, GitHub App, system editor, live views | An engineer composes, runs and inspects a system from the browser |
| **M6: co-simulation port** ([#14](https://github.com/isc-fs/IFS_vHIL/issues/14)) | Time-synchronised signal port for plants; scripted plants first | A scripted plant drives the ECU's pedals and inverter in closed loop; MingoCIL can attach |
| **M7: beyond STM32** ([#15](https://github.com/isc-fs/IFS_vHIL/issues/15)) | Platform abstraction exercised by a second MCU family, then a Linux SoC spike | A non-STM32 board runs in a system next to the H733 boards |
| IFS_HIL routing ([#3](https://github.com/isc-fs/IFS_vHIL/issues/3)) | Virtual bench descriptor in IFS_HIL | A firmware PR gets a virtual verdict without bench-01 (after M1) |
| Depth ([#4](https://github.com/isc-fs/IFS_vHIL/issues/4)) | Bootloader emulation, fault injection, coverage | A-003 and Block D run virtually |

M1 comes first because everything else plugs into it. The AMS model (M2) is
the first test of how cheaply the catalogue grows. M5 waits for M1–M4,
because a UI built before the API under it would be rebuilt.

## 6. Renode: upstream first, fork when it pays

Renode is a dependency we pin (1.17.0) and extend, not code we own. Most
growth needs no fork:

- **Chip and peripheral models** are C# files compiled at load time
  (`include @model.cs`) or `.repl` descriptions. They live in this repo's
  catalogue.
- **Core bugs** go upstream with a local workaround until a release carries
  the fix, as with [renode/renode#1021](https://github.com/renode/renode/issues/1021)
  (BASEPRI after reset).

**Consider forking `renode` / `renode-infrastructure` when one of these holds:**

- We need a core change upstream will not take, or will not release in time:
  the time framework (pacing, co-simulation sync for MingoCIL), `tlib` CPU
  internals, or distributing many machines across workers.
- Workarounds for unfixed core bugs start to shape our own code.
- A platform we need (a new MCU family or SoC) requires core support beyond
  what out-of-tree models can provide.

**When we do, keep it cheap:** fork at a release tag, keep our changes as a
small patch set rebased on each upgrade, send every generally useful patch
upstream, and record the fork's base and patches in this repo. A fork that
drifts from upstream loses the models and fixes we rely on Antmicro for.

## 7. Risks

- **The catalogue is the real limit.** "Infinite combinations" holds only for
  what is modelled. Track model coverage, and make chip models small,
  documented and unit-tested.
- **Scale and time.** Many boards at once, or a SoC, run below real time.
  Principle 2 covers tests; the UI has to show virtual time, not imply real
  time.
- **Multi-backend creep.** Keep the backend boundary thin, and add a second
  backend only when a platform needs it.
- **Two benches, neither the truth.** A pass on either is not a pass on the
  car. When they disagree, check both against the car's hardware and the
  firmware's intent: the gap can be in the models or in the physical bench.
