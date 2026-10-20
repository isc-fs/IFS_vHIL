Packet: Look up each field's attributes once

### Related issue

Part of renode/renode#ISSUE_B.

### Description

Performance improvement in `Packet`, no behaviour change.

`Packet.Decode`, `DecodeInto`, `Encode` and the length and offset
calculations read each field's attributes through
`FieldPropertyInfoWrapper`: `ByteOffset`, `BitOffset`, `ByteAlign`,
`BytePaddingBefore`, `Order`, the `Width` attribute behind `Width` and
`BitWidth`, and `IsLSBFirst` (the member's and its declaring type's
`LeastSignificantByteFirst`). Each read called `GetCustomAttributes`
(`GetCustomAttribute` for the type), so decoding one value did several
reflection look-ups per field, each allocating the attribute array and new
attribute instances. The wrappers themselves are already cached per type
(`GetFieldsAndProperties`), and a member's attributes never change.

This looks them up once, in the wrapper's constructor (one private
constructor that both public ones chain to), and keeps them in read-only
fields; the properties read those. `GetAttribute<T>()` becomes a private
static helper used there only. Every value decoded or encoded is the same:
the same attributes are read, at another time. The one observable
difference is when a malformed attribute would throw (when the wrapper is
built, the first time the type is used, instead of at the first read of
that attribute), and no attribute in the tree throws.

Who gains: every model that decodes or encodes with `Packet` per frame,
descriptor or command (43 files: the CAN controllers, `MCAN`,
`NXP_FlexCAN`, `STM32_FDCAN`, `SocketCAN`; DMA descriptors, `LDMA`,
`NXP_eDMA`, `Arm_Dma350`; the Synopsys Ethernet MACs' descriptors; USB
descriptors and transfers; `ARM_SMMUv3`, VirtIO, UFS). `MCAN` is the
extreme case: `FilterMessage` decodes every filter element of the list from
the message RAM for every frame it receives (`DecodeFilterElement`), and
the Tx path decodes the Tx buffer's header and encodes the Rx FIFO and Tx
event elements, so a CAN bus with a few filters per node spends most of
its host time in this reflection.

### Usage example

No behaviour change, so nothing new to show in a reproduction repository.
`PacketTests` gets one test that pins what the change must keep: a type
without a byte order of its own whose fields carry their own
(`LeastSignificantByteFirst` on some, not on others), offsets and widths,
decoded and encoded three times in a row (it passes on master too).

`tooling/bench/mcan-loopback.resc` in our measurements: an STM32H753's
FDCAN1 (`MCAN`) in internal loopback with 16 standard filter elements (15
ranges that don't match, then a classic filter for the frame's ID into Rx
FIFO 0, overwrite mode) and one dedicated Tx buffer, configured from the
monitor; a Cortex-M7 at 100 MIPS requests a transmission every ~400
instructions (about 25,000 frames in the measured 0.1 s of virtual time).
Master vs master + this PR, median of 5, interleaved, Linux arm64 in
Docker, 4 cores: 23.40 s → 1.35 s of host time (17x as fast: from 0.94 ms
to 54 us per frame), allocation 73 → 3.5 GB per virtual second (-95 %),
process CPU 456 → 32 s per virtual second (the garbage collector's threads
included).

Our use: three STM32H733 boards (FreeRTOS firmware with HAL FDCAN) on four
CAN buses; on x86 (`perf`, steady state) the attribute reflection was 6 %
of all samples and `MCAN`'s receive path 8-12 %. With this change (on
Renode 1.17.0, with our other changes) the two-board system ran 10 %
faster in steady state and the three-board one 7-10 %, with the same
results.

### Additional information

Validated in Docker (`mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim`, Linux
arm64), renode master d0c70ee3 with renode-infrastructure master
48cee122:

- headless portable build (`./build.sh -t --no-gui`): succeeded
- `UnitTests`: 622 passed, 6 skipped (master: 621 / 6; the new test, which
  passes on master's code as well); `PeripheralsTests`: 113 passed, 6
  skipped, as master
- Robot, as master (141 passed): `MCAN` (Zephyr's CAN tests and samples on
  STM32H753, Linux with `MCAN` on Zynq), `UT32_CAN`, `NXP_FlexCAN`,
  `IMX8MP_Flexcan`, `SynopsysDWCEthernetQualityOfService`,
  `STM32F4_Discovery` (Synopsys Ethernet MAC), `nucleo_h753zi` (USB, FDCAN),
  `external-mmu` (`ARM_SMMUv3`), and the CPU hook suites
- `dotnet format whitespace --verify-no-changes` on the changed files:
  passed
- byte-identical results: on our STM32H733 systems (one, two and three
  boards, CAN between them, 10 s of virtual time), every CAN frame's timing
  in ns, every GPIO edge with the instruction count at its write, and each
  CPU's executed instructions, PC, register state and RAM digest are the
  same on master and on master with this PR and two others (a tlib and a
  `TranslationCPU` change for memory access hooks)
