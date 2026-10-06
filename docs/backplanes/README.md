# Backplanes (reference)

Every unit on the car is the same board, the MainLite (STM32H733), mounted on
a different backplane PCB. The backplane decides which MainLite pins reach the
car and what they carry. These pages record that routing, traced from each
backplane's KiCad schematic and checked against the firmware:

| Page | Unit | Schematic |
|---|---|---|
| [`ams.md`](ams.md) | AMS (accumulator management) | IFS08-CE-AMS `pcbs/AMS IFS08/AMS IFS08.kicad_sch` |
| [`ecu.md`](ecu.md) | ECU (vehicle control unit) | isc-fs/IFS08-ES `boards/BACKPLANE_ECU/kicad/BACKPLANE_ECU.kicad_sch` (v3.0) |
| [`udv.md`](udv.md) | uDV (driverless controller) | IFS09-DV-uDV `MicroDV_PCB_Schematics/MicroDV2/MicroDV2.kicad_sch` (`feat/5-hardware`) |

**These pages are the reference; the catalogue copies only labels.** The
vHIL has no backplane kind. Systems name the MainLite's own connectors and pins
(`ams.PB9`, `ecu.FDCAN2`) and cite the car signal in a trailing comment:

```yaml
isospi: {model: ltc6820, spi: ams.SPI1, cs: ams.PB9}  # SPI1 + LTC6820_CS -> U4 LTC6820 (docs/backplanes/ams.md)
```

isc-fs/IFS_vHIL#138 briefly made backplanes a catalogue kind that aliased
signal names to pins. It was removed because it caught no wiring mistakes:
it only checked a backplane against its own map. It also changed no generated
script, and it added an alias layer that every endpoint feature had to honour.
Its maps were incomplete by design.

What the roles table (`catalog/boards/mainlite.yaml`) takes from these pages
is display and warnings only, never aliasing. Each role lists the pins its
backplane routes, labelled with the car signal (`PF8: APPS_1`), and the board
lists what is connected in every role (`onboard`: SDMMC1, I2C2). The editor
shows the labels (`PF8 · APPS_1`, `FDCAN3 · n.c.`), and
`python -m vhil.system validate` warns, without failing, when a system wires
a pin its role leaves unconnected:

```
warning: systems/x.yaml: ams: FDCAN3 is not connected on the AMS backplane (docs/backplanes/ams.md)
```

A role may also make a pin the board models as an analog input a GPIO
(`gpio:`): the AMS's PF9 (TSMS, a digital input) and the uDV's PC1 (a debug
LED output). No system used either, so no generated script changed. When a
pin map here changes, update the role's `pins` with it.

These pages keep everything #138 traced,
including the pins the vHIL does not model.

## Common to every unit

- Every MainLite carries the CAN bootloader in flash sector 0, and the
  application at `0x08020000`.
- Bootloader node id: **ECU 0x1, AMS 0x2, uDV 0x3**.
- Flash bus: **FDCAN2** for the ECU and the uDV, **FDCAN1** for the AMS
  (owner, 2026-10-05).
- Both are data on the board, not here: the MainLite's `roles` table
  (`catalog/boards/mainlite.yaml`) maps each role to its node id, flash bus
  and firmware (ecu, ams; udv has none in the catalogue yet), and a system
  places each MainLite in one (`role: ecu`) and names no firmware. The vHIL
  boots every MainLite through its bootloader, seeded with its role's node id.
- The MainLite's CAN_1 header pins are L-then-H (J4.3 `CANL1`, J4.4
  `CANH1`). CAN_2 and CAN_3 are H-then-L. This ordering is the likely cause of
  the AMS swap (IFS08-CE-AMS#621).
- Every MainLite terminates all three CAN channels on board, with 120 Ω R1,
  R5 and R9 and no jumper (IFS08-CE-AMS#622).
- The microSD (SDMMC1) and the BMI088 IMU (I2C2, PF0/PF1) are on the MainLite
  itself, not on a backplane.
- Which FDCAN carries which car bus depends on the unit. Don't generalise
  it: the ACU bus is FDCAN1 on the AMS but FDCAN2 on the ECU and the uDV.
