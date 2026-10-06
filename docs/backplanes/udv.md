# uDV backplane (MicroDV2)

A MainLite on this backplane is the driverless controller. The backplane
drives the EBS valves, the SDC relay and the ASSIs, and senses the EBS
pressures and 24 V. It also takes the DV CAN (RES, datalogger) and the ACU
bus.

**Traced from:**

- Schematic: isc-fs/IFS09-DV-uDV,
  `MicroDV_PCB_Schematics/MicroDV2/MicroDV2.kicad_sch` (symbol MCU1,
  `MAIN_LITE`).
  - The schematic exists **only on branch `feat/5-hardware`**, not on `dev`.
  - It was traced at 6766d99 ("Feedback aplicado"). The MCU pin mapping is
    the same in the pre-feedback revision 71db3d7.
- Firmware: IFS09-DV-uDV `dev` @ dbdfca4: `Core/Inc/main.h`, `uDV.ioc`,
  `Core/Src/hardware_io.c` and `can_interface.cpp`.

**Bootloader:** CAN bootloader in sector 0, node id **0x3**, flashed over
**FDCAN2**, the ACU bus. See the [index](README.md#common-to-every-unit).

## Pin map

| MainLite pin / peripheral | Car net / signal | Backplane connector pin | Conditioning | Firmware symbol (file:line) |
|---|---|---|---|---|
| FDCAN1 (CAN_1, MainLite U2; MCU1.21/22) | `/CAN 1 L/H DATALOGGER`: one net, also labelled steering motor, GF and IMU | J6.15–J6.22 | MainLite R1 120 Ω | `can_interface.cpp:114-150` (RES CANopen `0x191`/`0x711`), `:670-700` (DV datalogger frames) |
| FDCAN2 (CAN_2, U3; MCU1.23/24) | `/CAN H 2` / `/CAN L 2`: the ACU bus to the ECU | J6.13 / J6.14 | R39 120 Ω on MicroDV2, plus MainLite R5 120 Ω (IFS08-CE-AMS#622) | `can_interface.cpp:152-213` |
| FDCAN3 (CAN_3, U4; MCU1.25/26) | **no-connect** (`unconnected-(MCU1-CANH_3-Pad25)`) | — | MainLite R9 120 Ω | `can_interface.cpp:584-723`: steering `0x520`–`0x522`, AMI `0x503`/`0x50A`, ASSI `0x100` ([#6](https://github.com/isc-fs/IFS09-DV-uDV/issues/6)) |
| PB4 (GPIO1) | `/EBS micro 1 (D1)`: EBS valve 1 | J10.2 | Q3 low-side driver | `main.h:84` `D1_Pin`; `hardware_io.c:84-87` (low = fire) |
| PB5 (GPIO2) | `/EBS Micro 2 (D2)`: EBS valve 2 | J10.6 | Q4 low-side driver | `main.h:86` `D2_Pin`; `hardware_io.c:89-92` |
| PB6 (MCU1.8) | `/Debug 1`: LED D26 | on board | 1k to the LED | `main.h:88` `D3_Pin` (set low once, never written; [#7](https://github.com/isc-fs/IFS09-DV-uDV/issues/7)) |
| PB7 | `/Control SDC Micro (D4)`: the K1 SDC relay | not traced | not traced | `main.h:90` `D4_Pin`; `hardware_io.c:79-82` `hardware_io_set_as_close_sdc` |
| PB8 (MCU1.10) | `/PWM yellow (D5)`: ASSI yellow (−) | not traced | R32, Q6 (BCV29), Q1 (AOD4185) | `main.h:92` `D5_Pin` (never driven; [#7](https://github.com/isc-fs/IFS09-DV-uDV/issues/7)) |
| PB9 (GPIO6, MCU1.11) | `/PWM blue (D6)`: ASSI blue (−) | J6.1 / J6.3 / J6.5 | R34, Q7, Q8 | `main.h:94` `D6_Pin` (never driven; [#7](https://github.com/isc-fs/IFS09-DV-uDV/issues/7)) |
| PF7 (GPIO7, MCU1.12), ADC3 INP3 | `/Micro in pres 1 (A1)`: EBS tank pressure 1 (Festo SPAN, 0–10 V) | J6.27 | R12 10k / R11 5k (÷3) | `main.h:64` `A1_Pin`; read only as `hardware_io_read_sdc_is_ready()` (`hardware_io.c:96-100`), which nothing calls ([#5](https://github.com/isc-fs/IFS09-DV-uDV/issues/5)) |
| PF8 (GPIO8, MCU1.13), ADC3 INP7 | `/Micro in pres 2 (A2)`: EBS tank pressure 2 | J6.28 | R13 10k / R14 5k (÷3) | `main.h:66` `A2_Pin`; read only as `hardware_io_read_sdc_res_open()` (`hardware_io.c:115-119`), which nothing calls ([#5](https://github.com/isc-fs/IFS09-DV-uDV/issues/5)) |
| PF9 (GPIO9, MCU1.14), ADC3 INP2 | `/Micro in EBS (A3)`, from `/24V EBS`: the EBS-supply indicator | on board (SW1, D7) | R15 10k / R16 1k (÷11) | `main.h:68` `A3_Pin`; read as **ASMS** by `hardware_io_read_asms_on()` (`hardware_io.c:103-107`; [#4](https://github.com/isc-fs/IFS09-DV-uDV/issues/4)) |
| PF10 (MCU1.15), ADC3 INP6 | `/Sensor SDC micro (A4)`, from `/SDC out` | on board | R18 10k / R19 1k (÷11) | `main.h:70` `A4_Pin`; read as **EBS pressure 2** (`hardware_io.c:144-148`; [#5](https://github.com/isc-fs/IFS09-DV-uDV/issues/5)) |
| PC0 (MCU1.16), ADC3 INP10 | `/Lectura RES (A5)`, from `Señal RES` | J6.29 | R40 10k / R41 1k (÷11) | `main.h:72` `A5_Pin`; read as **EBS pressure 1** (`hardware_io.c:138-142`; [#5](https://github.com/isc-fs/IFS09-DV-uDV/issues/5)) |
| PC1 (MCU1.17) | `/Debug 2`: LED D27, an **output** on this board | on board | 1k to the LED | `main.h:74` `A6_Pin`, configured analog and read as TSMS (`hardware_io.c:109-113`, uncalled; [#7](https://github.com/isc-fs/IFS09-DV-uDV/issues/7)) |
| PC2 (MCU1.18) | `/Debug 3`: LED D28 | on board | 1k to the LED | `main.h:76` `A7_Pin` (left in analog reset state; [#7](https://github.com/isc-fs/IFS09-DV-uDV/issues/7)) |
| PG11 / PG12, USART10 (MCU1.4/5) | **no-connect** | — | — | `usart.c:98-107`; `ws2812.c:39` sends the ASSI colours to it ([#7](https://github.com/isc-fs/IFS09-DV-uDV/issues/7)) |
| (none) | ASMS: `/ASMS in 1`, through K3, to `/ASMS 2 (24V)` | J8.1 | **no MCU sense** | expected by `hardware_io_read_asms_on()`, which reads PF9 instead ([#4](https://github.com/isc-fs/IFS09-DV-uDV/issues/4)) |

## In the vHIL

No uDV system exists yet, and none can until the catalogue has the uDV's
firmware: the udv role names `udv`, which `catalog/firmware/` lacks, so a
system placing it is refused. When one is added, its endpoints will name
these MainLite pins (`udv.FDCAN2` for the ACU bus, and so on). The MainLite
models PC1 as an analog input, but on MicroDV2 it is a debug LED output, so
the udv role makes it a GPIO (`gpio: PC1`) and refuses an analog source on
it. PB6–PB8, PF10, PC0 and PC2 (`PC2_C`) are `unwired` on the board: shown in
the editor, refused by `validate` as not emulated yet.

## Known gotchas

- **FDCAN3 is no-connect, but the firmware uses it**
  ([IFS09-DV-uDV#6](https://github.com/isc-fs/IFS09-DV-uDV/issues/6)):
  - The firmware sends steering, AMI and the ASSI status frame on FDCAN3,
    but MCU1.25/26 are unconnected.
  - The steering-motor CAN is routed onto CAN_1 instead.
  - On this board the uDV can't command or hear the steering. The
    `steer_emergency` path can never fire, and AMI mission select (`0x503`)
    is never received.
- **EBS pressures read from the wrong pins, with the wrong scale**
  ([IFS09-DV-uDV#5](https://github.com/isc-fs/IFS09-DV-uDV/issues/5)):
  - The tanks are on PF7/PF8, but the firmware reads PC0 (RES) and PF10
    (SDC sense).
  - `PRES_DIVIDER = 11` (`hardware_io.c:27`), but the board divides by 3
    (10k/5k). With only the pins fixed, the reading would be about 3.7×
    the true pressure.
- **ASMS has no MCU sense**
  ([IFS09-DV-uDV#4](https://github.com/isc-fs/IFS09-DV-uDV/issues/4)):
  - `hardware_io_read_asms_on()` reads PF9, which is the EBS-supply
    indicator. Its `/24V` source is unsourced on this revision.
  - The ASMS rail reaches no MCU pin, so the firmware always sees ASMS off,
    and the car never leaves AS OFF.
- **ASSI outputs never driven, and USART10 unwired**
  ([IFS09-DV-uDV#7](https://github.com/isc-fs/IFS09-DV-uDV/issues/7)):
  - PB8/PB9 drive the ASSI stages but are only set low at init.
  - The ASSI renderer sends its colours to an Arduino over USART10
    (PG11/PG12), which is unconnected on MicroDV2.
  - The debug LEDs (PB6, PC1, PC2) are unusable: PC1 and PC2 are configured
    as analog inputs, and PB6 is never written.
