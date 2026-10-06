# ECU backplane (BACKPLANE ECU v3.0)

A MainLite on this backplane is the vehicle control unit. The backplane
brings in the pedal sensors and the START button and drives the RTDS
buzzer. It also carries the inverter, ACU and dash buses, the GPS and the
nRF24 telemetry radio.

**Traced from:**

- Schematic: isc-fs/IFS08-ES, `boards/BACKPLANE_ECU/kicad/BACKPLANE_ECU.kicad_sch`,
  v3.0 (symbol MCU1, `MAIN_LITE`), on `main` @ bcb4511.
- Firmware: IFS08-CE-ECU `dev` @ 544b651: `Core/Inc/main.h`, the `.ioc`
  and `Core/Inc/app/can_frame.hpp`.

**Bootloader:** CAN bootloader in sector 0, node id **0x1**, flashed over
**FDCAN2**, the ACU bus. See the [index](README.md#common-to-every-unit).

## Pin map

| MainLite pin / peripheral | Car net / signal | Backplane connector pin | Conditioning | Firmware symbol (file:line) |
|---|---|---|---|---|
| FDCAN1 (CAN_1, MainLite U2; PD0/PD1; MCU1 pads 22 `CANH_1` / 21 `CANL_1`) | `CAN_H_INV` / `CAN_L_INV`: the inverter | J1.9 / J1.10, J2.1 / J2.2 | MainLite R1 120 Ω on board | `can_frame.hpp:25` `CanBus::Inv` |
| FDCAN2 (CAN_2, U3; PB12/PB13) | nets `CAN_H_TEL` / `CAN_L_TEL`. **The name is stale:** this is the ACU bus to the AMS and the uDV | J1.11 / J1.12 | MainLite R5 120 Ω; no terminator on the backplane | `can_frame.hpp:26` `CanBus::Acu` |
| FDCAN3 (CAN_3, U4; PG9/PG10) | nets `CAN_H_SENS` / `CAN_L_SENS`. **The name is stale:** this is the dash bus, transmit-only | J1.13 / J1.14 | MainLite R9 120 Ω | `can_frame.hpp:27` `CanBus::Dash` |
| PB4 (GPIO1, MCU1 pad 6) | `RTDS`: ready-to-drive sound | J1.20 | **none**: bare MCU pin, no driver or protection ([#254](https://github.com/isc-fs/IFS08-CE-ECU/issues/254)) | `main.h:82` `RTDS_Pin`; `control_task.cpp:370` |
| PB5 (GPIO2) | `START_FIL`, from `START` | J1.19 | R6 1k / R7 2k divider, D4 zener | `main.h:84` `START_Pin` (high = pressed) |
| PF7 (GPIO7), ADC3 INP3 | `S_BRAKE_FIL`, from `S_BRAKE` | J1.18 | R8 1k / R9 2k divider, D5 zener | `main.h:60` `S_BRAKE_Pin` |
| PF8 (GPIO8), ADC3 INP7 | `APPS_1` | J1.3 | R1 100k / C1 100n | `main.h:62` `APPS_1_Pin` |
| PF9 (GPIO9), ADC3 INP2 | `APPS_2` | J1.6 | R2 100k / C2 100n | `main.h:64` `APPS_2_Pin` |
| PB6 (GPIO3, MCU1 pad 8) | the firmware's DC-link discharge coil-interrupt | header J3.18 only | **none on any schematic** ([#253](https://github.com/isc-fs/IFS08-CE-ECU/issues/253)) | `main.h:86` `D3_Pin`; `control_task.cpp:296-301` |
| PD5 | `S_TEMP_REFRI` (1-Wire) | J1.15 | not traced | none (no firmware) |
| PG11 / PG12 (USART10) | the GPS | not traced | not traced | not traced |
| PB0, PC5, PC4 + PA5/PA6/PA7 (SPI1 pins) | the nRF24 telemetry radio | on board | — | `main.h:76` `NRF24_CS_Pin`, `:74` `NRF24_CE_Pin`, `:72` `NRF24_IRQ_Pin`; bit-banged in `nrf24.c` (SPI1 removed in 16b61e1) |
| PB7, PB8, PF10, PC0, PC2 | — | header J3 only | — | `main.h:88` `D4_Pin`, `:90` `D5_Pin`, `:66` `A4_Pin`, `:68` `A5_Pin` |
| PB9, PC1 | spare | header J3 only | — | `main.h:92` `D6_Pin`, `:70` `A6_Pin` |

## In the vHIL

The ECU systems (`systems/ecu.yaml`, `ecu-bl.yaml`, `ecu-ams.yaml`) use these
pins:

- `ecu.FDCAN1` on `can_inv`, `ecu.FDCAN2` on `can_acu`, and `ecu.FDCAN3` on
  `can_dash`;
- `ecu.PF7` / `PF8` / `PF9` for brake, APPS_1 and APPS_2, through the
  co-simulation port and the bench DAC1 routes;
- `ecu.PB5` for START and `ecu.PB4` for RTDS, through the port;
- `ecu.PB6`, the DC-link discharge output, through the port (`discharge` in
  `ecu.yaml`; `tests/sim/test_ecu_discharge.py`).

Every other routed pin is emulated too, as the board models it: PD5 and the
nRF24's PB0/PC5/PC4 are plain GPIOs (no 1-Wire sensor or radio is modelled),
PB7/PB8 GPIOs, PF10/PC0/PC2_C ADC3 INP6/INP10/INP0 (what `ECU.ioc` configures
for A4/A5), and `ecu.USART10` the platform's USART10 (no GPS is modelled).
A test drives or watches one with `Sim.io("ecu").gpio("PB6")` or
`set_voltage("PF10", v)`.

## Known gotchas

- **The TEL/SENS net names are stale**
  ([IFS08-CE-ECU#255](https://github.com/isc-fs/IFS08-CE-ECU/issues/255)):
  - "TEL" (FDCAN2, J1.11/12) is the ACU bus, and "SENS" (FDCAN3, J1.13/14)
    is the dash bus.
  - The copper is consistent end to end; only the names are wrong. They are
    also stale in the backplane datasheet and in the ECU's
    `docs/PINES_RUTEADOS_IOC.md`.
  - Unlike the AMS, the ECU's ACU bus is **FDCAN2**.
- **No DC-link discharge driver on PB6**
  ([IFS08-CE-ECU#253](https://github.com/isc-fs/IFS08-CE-ECU/issues/253)):
  - The firmware drives the discharge coil-interrupt from PB6, but on this
    backplane PB6 only reaches header J3.18.
  - No schematic in isc-fs has the NPN, NC relay or base pull-down the ECU
    docs describe, and PB6 has no pull on either released board.
- **RTDS is unbuffered**
  ([IFS08-CE-ECU#254](https://github.com/isc-fs/IFS08-CE-ECU/issues/254)):
  - PB4 goes to the harness on J1.20 as a bare 3.3 V MCU pin. That pin can't
    power a ready-to-drive sounder.
  - It is unprotected on a connector that also carries +24 V, and PB4 resets
    as NJTRST with a pull-up, so the line sits weakly high until
    `MX_GPIO_Init`.
