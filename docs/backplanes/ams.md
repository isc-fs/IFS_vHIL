# AMS backplane (AMS IFS08)

A MainLite on this backplane is the accumulator management system. The
backplane carries the LTC6820 isoSPI master for the battery chain, the pack
and DC-DC current-sensor inputs, the AIR/precharge and AMS_OK relay drivers,
and the ACU bus.

**Traced from:**

- Schematic: isc-fs/IFS08-CE-AMS, `pcbs/AMS IFS08/AMS IFS08.kicad_sch`
  (symbol MCU1, `MAIN_LITE`).
- Firmware: IFS08-CE-AMS `dev`, `Core/Inc/main.h`, `AMS.ioc` and
  `Core/Inc/app/ams_config.hpp`. Line numbers were checked at `dev` @ 1508d13.

**Bootloader:** CAN bootloader in sector 0, node id **0x2**, flashed over
**FDCAN1**. The ECU and the uDV flash over FDCAN2. See the
[index](README.md#common-to-every-unit).

## Pin map

| MainLite pin / peripheral | Car net / signal | Backplane connector pin | Conditioning | Firmware symbol (file:line) |
|---|---|---|---|---|
| FDCAN1 (CAN_1, MainLite U2 TCAN330; MCU1 pads 21 `CANL_1` / 22 `CANH_1`) | `CAN_H_ACU` / `CAN_L_ACU`: the ACU (accumulator) bus to the ECU | J1.9 / J1.10 | R35 120 Ω across the pair on the backplane, plus the MainLite's fixed R1 120 Ω (over-terminated: [#622](https://github.com/isc-fs/IFS08-CE-AMS/issues/622)). **H and L are swapped at the MainLite** ([#621](https://github.com/isc-fs/IFS08-CE-AMS/issues/621)) | `ams_config.hpp:521`, `:584` (ACU bus = FDCAN1); `main.c:466` `MX_FDCAN1_Init`; `acu_can_task.cpp:52` `hfdcan1` |
| FDCAN2 (CAN_2) | no-connect | — | — | — |
| FDCAN3 (CAN_3) | no-connect | — | — | — |
| SPI1 (MOSI / MISO / SCK) | to U4 LTC6820. U4's IP/IM reach the BMS chain through T1 | J5 (isoSPI side, through T1) | T1 isoSPI transformer | `main.c:596` `MX_SPI1_Init` |
| PB9 (GPIO6) | `LTC6820_CS`: U4 chip select | on board (U4) | direct | `main.h:82` `LTC6820_CS_Pin` |
| PB4 (GPIO1) | `AMS_OK`: K5 AMS latch relay in the shutdown circuit ² | J1.4 | R21 1 kΩ gate / R9 2.2 kΩ pull-down / Q10 low side of K5's coil, D17 flyback ² | `main.h:74` `AMS_OK_Pin` |
| PB5 (GPIO2) | `AIR+`: AIR+ coil low side; TSAL | J11.6 (coil), J7.4 (TSAL) | R7 / Q2 low-side driver | `main.h:76` `RELAY_AIR_P_Pin` |
| PB6 (GPIO3) | `AIR-`: AIR- coil low side; TSAL | J11.5 (coil), J7.6 (TSAL) | R15 1 kΩ gate / R14 2.2 kΩ pull-down / Q1 DMN6075S low-side driver, flyback D6 to AIR+_VCC ¹ | `main.h:78` `RELAY_AIR_N_Pin` |
| PB7 (GPIO4) | `PRECHARGE`: precharge relay coil low side; TSAL | J11.4 (coil), J7.5 (TSAL) | R5 1 kΩ gate / R3 2.2 kΩ pull-down / Q3 DMN6075S low-side driver, flyback D2 to AIR+_VCC ¹ | `main.h:80` `RELAY_PRECHARGE_Pin` |
| PF7 (GPIO7), ADC3 INP3 | `S_CURRENT_P`: pack current sensor OUTP | J4.3 | R10 100 Ω series | `main.h:66` `S_CURRENT_P_Pin` (ADC3 INP3/INN3 differential) |
| PF8 (GPIO8), ADC3 INN3 | `S_CURRENT_N`: pack current sensor OUTN | J4.2 | R11 100 Ω series | `main.h:68` `S_CURRENT_N_Pin` |
| PF9 | `TSMS_FIL`: TSMS, a **digital input**. It senses AIR+_VCC, the contactor coils' (shutdown-circuit-switched) supply ¹ | J1.16 (AIR+_VCC), J11.1-3 | R18 62 kΩ from AIR+_VCC, R19 / D4 ¹ | `main.h:70` `TSMS_Pin` |
| PF10 | `RST_PIL_FIL`: DASH_CHG, a **digital input** | not traced | not traced | `main.h:72` `DASH_CHG_Pin` |
| PC0 | `S_TEMP_DCDC` | J3.7 | not traced | none |
| PC1 (GPIO12), ADC3 INP11 | `S_CURRENT_DCDC`: the DC-DC's ACS758 | J3.5 | not traced | **not sampled on `dev`**, by design: no DC-DC is fitted (af07ec8; `ams_config.hpp:607`, `:713-715`) |
| PB8, PB0, PC2 | spare | J1.20, J1.21, J1.22 | — | — |

¹ Traced for M8 (#147, [`m8-plan.md`](../m8-plan.md)) from the backplane's
current schematic, isc-fs/IFS08-ES main bcb4511,
`boards/AMS_IFS08/kicad/AMS IFS08.kicad_sch` (its netlist): AIR+, AIR- and
PRECHARGE are MainLite pads 7, 8 and 9; the three coils' high side and
flyback diodes (D5, D6, D2) are on AIR+_VCC.

² Traced for TS-2 (#147, [`m8-plan.md`](../m8-plan.md#ts-2-the-shutdown-circuit))
from the same schematic: the SDC runs SC_IN (J1.14) → J4.5/J4.6 (off-board) →
J7.7/J7.8 (HV_INTERLOCK+/-) → K4 IMD latch → K5 AMS latch → J2.4/J2.3
(off-board) → SC_OUT (J1.15); the harness returns it as TSMS = AIR+_VCC on
J1.16. K4 (IMD_OK, J1.3, Q8) and K5 (AMS_OK, Q10) are self-holding latches:
their coils' high side is RST_IMD / RST_BMS, fed by the RST_EXT button
(J1.7, through D9/D10) and held by each relay's pole 1, so each closes on an
RST_EXT press with its OK line high and drops when it falls. RST_PILOTO
(J1.6) reaches only PF10 (DASH_CHG), not the latches, whatever the board's
datasheet says.

The MainLite's own microSD (SDMMC1) and BMI088 (I2C2) are not routed by
the backplane.

## In the vHIL

The AMS systems (`systems/ams.yaml`, `ams-bl.yaml`, `ecu-ams.yaml`) use these
pins:

- `ams.FDCAN1` on `can_acu`;
- `ams.SPI1` with `cs: ams.PB9` for the LTC6820 and its LTC6811 chain;
- `ams.PF7` / `ams.PF8` for the pack current pair;
- `ams.PC1` for the DC-DC sensor, which the firmware ignores;
- in `ams.yaml`'s co-simulation port, the cockpit inputs `ams.PF9` (TSMS)
  and `ams.PF10` (DASH_CHG), and the relay drivers `ams.PB4` (AMS_OK),
  `ams.PB5` (AIR+), `ams.PB6` (AIR-) and `ams.PB7` (precharge).
  `tests/sim/test_ams_contactors.py` watches the relays at those pins, and
  `test_ams_fsm_charger.py` presses DASH_CHG through the port.

The MainLite models PF9 and PF10 as analog inputs (ADC3 INP2, INP6), but on
the AMS they are the TSMS and DASH_CHG digital inputs, so the AMS role makes
them GPIOs (`gpio:` in its roles entry): they take `gpio_in` port signals,
and an analog source on either is refused. PC0 (ADC3 INP10) and the spares
(PB8, PB0 GPIOs; PC2_C, ADC3 INP0) are emulated as the board models them;
the firmware uses none of them.

The pack-current pair is read through the car's carrier, not an ideal ADC
(#320). The AMS firmware's calibration (`CurrentMvPerAmpe1` 46 for the
SSA-2's 5 mV/A, `CurrentZeroCount` 2054; IFS08-CE-AMS `ams_config.hpp:785-797`)
absorbs a gain of 0.92 on the differential and a zero 6 codes above
mid-scale, measured on the car's carrier (the gain read back exact against
an aux-PSU current; the HIL bench carrier, with a DAC and no backplane, saw
0.924). The AMS role's `adc_differential` (`catalog/boards/mainlite.yaml`)
gives ADC3 that transfer on INP3/INN3 (`Stm32H7Adc3.cs`
`DifferentialTransfer`), so a true current on the sensor model reads as
itself and `CurrentMaxMa` (185 A) trips at 185 A of true current. On the
ideal ADC the AMS read about 1.087 × I − 2.1 A and tripped at 173 A. No
datasheet term found explains the gain (VREF+ is VDD through 47 Ω, VDD
3.30 V ± 0.10 V; the series R10/R11 100 Ω against the pins' weak pull-downs
take a few tenths of a percent), so the figures are the measurement's.
Single-ended reads stay ideal: nothing measured them.

## Known gotchas

- **CAN_H/L swapped at the MainLite connector**
  ([IFS08-CE-AMS#621](https://github.com/isc-fs/IFS08-CE-AMS/issues/621),
  closed by the owner):
  - `CAN_H_ACU` (J1.9) lands on MCU1 pad 21 `CANL_1`, and `CAN_L_ACU` (J1.10)
    lands on pad 22 `CANH_1`. The PCB follows the schematic.
  - The likely cause is the MainLite's L-then-H CAN_1 pin order.
  - If the car works, the harness crosses pins 9/10 somewhere. A harness
    built to the backplane labels would take the AMS off the ACU bus.
  - The vHIL's CAN model has no polarity, so it can't show this.
- **ACU bus over-termination**
  ([IFS08-CE-AMS#622](https://github.com/isc-fs/IFS08-CE-AMS/issues/622),
  closed):
  - Backplane R35 and the MainLite's fixed R1 put 60 Ω at the AMS node alone.
  - With the ECU (MainLite R5), the bus sees 40 Ω. With the uDV as well
    (MainLite R5 + MicroDV2 R39), it sees 24 Ω, against ISO 11898-2's 60 Ω.
- **PF9 is `TSMS_FIL` and PF10 `RST_PIL_FIL` (DASH_CHG), digital inputs**
  (`main.h:70` `TSMS_Pin`, `:72` `DASH_CHG_Pin`), not the analog inputs the
  MainLite board models; the AMS role makes them GPIOs.
- **The DC-DC current (PC1) is routed but not read.** AMS `dev` dropped the
  measurement in af07ec8 (no DC-DC fitted). The 0x135 frame's DC-DC slot is
  sent as 0 (`ams_config.hpp:607`), and the DC-DC temperature is a stub
  (`ams_config.hpp:713-715`).
