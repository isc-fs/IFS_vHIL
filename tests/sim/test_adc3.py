"""ADC3 (models/renode/Stm32H7Adc3.cs) at register level, poked on an ECU at
t = 0, before its first instruction (the CAN bootloader that runs next never
touches ADC3).

Facts (RM0468 ADC3; offsets and bits from stm32h733xx.h): ISR 0x00 (ADRDY 0,
EOSMP 1, EOC 2, EOS 3, OVR 4), CR 0x08 (ADEN 0, ADSTART 2), CFGR 0x0C (CONT
13), SQR1 0x30 (L [3:0], SQ1 [10:6]). One conversion at 2.5 + 12.5 ADC
clock cycles takes 156 ns at the MainLite's 96 MHz kernel clock. A single-
mode sequence converts once and clears ADSTART; a continuous one keeps
converting, and a result that lands while EOC is still set sets OVR.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

ADC3 = 0x58026000
ISR, CR, CFGR, SQR1, DR = ADC3 + 0x00, ADC3 + 0x08, ADC3 + 0x0C, ADC3 + 0x30, ADC3 + 0x40
ADEN, ADSTART = 1 << 0, 1 << 2
EOC, EOS, OVR = 1 << 2, 1 << 3, 1 << 4
CONT = 1 << 13


def _rd(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}").strip(), 16)


def _wr(sim, address, value):
    sim.monitor(f"sysbus WriteDoubleWord {address:#x} {value:#x}")


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        yield sim


def _start(sim, cfgr):
    _wr(sim, CR, ADEN)
    _wr(sim, CFGR, cfgr)
    _wr(sim, SQR1, 3 << 6)          # one conversion, channel 3
    _wr(sim, ISR, 0x7FF)
    _wr(sim, CR, ADEN | ADSTART)


def test_a_single_conversion_read_late_is_one_result(ecu):
    _start(ecu, 0)
    ecu.run_for(ms=1)               # thousands of conversion times later
    isr = _rd(ecu, ISR)
    assert isr & (EOC | EOS) == EOC | EOS
    assert isr & OVR == 0, f"ISR {isr:#x}: a single conversion overran"
    assert _rd(ecu, CR) & ADSTART == 0, "single mode: ADSTART clears after the sequence"


def test_continuous_conversions_read_late_overrun(ecu):
    _start(ecu, CONT)
    ecu.run_for(ms=1)
    isr = _rd(ecu, ISR)
    assert isr & (EOC | OVR) == EOC | OVR
    assert _rd(ecu, CR) & ADSTART, "continuous mode: still converting"
