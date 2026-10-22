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

# Impact selection (ci/impact.yaml, docs/ci.md): boards, systems, subsystems.
pytestmark = [pytest.mark.ecu, pytest.mark.system_ecu, pytest.mark.adc]

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


DIFSEL = ADC3 + 0xB0                # LTR2_DIFSEL on ADC3 (stm32h733xx.h)
NOISE_RMS, START_RMS = 1.07, 0.32   # the AMS carrier's, catalog/boards/mainlite.yaml (#327)


def _noisy_codes(sim, seed, n):
    """n single conversions of differential channel 3 (INP3 1.65 V + 33 mV,
    INN3 = INP7 at 1.65 V), each its own ADSTART, with the AMS carrier's
    measured noise seeded by `seed`."""
    adc = "sysbus.adc3_h73x"
    sim.monitor(f"{adc} SetVoltage 1683000 3")
    sim.monitor(f"{adc} SetVoltage 1650000 7")
    sim.monitor(f"{adc} DifferentialNoise 3 {NOISE_RMS} {START_RMS} {seed}")
    _wr(sim, DIFSEL, 1 << 3)
    _start(sim, 0)
    codes = []
    for _ in range(n):
        sim.run_for(us=10)
        codes.append(_rd(sim, DR))
        _wr(sim, CR, ADEN | ADSTART)
    return codes


def test_differential_noise_is_the_measured_spread_and_repeats(images):
    """#327: a differential channel with noise reads its ideal code
    (1 + 33 mV / 3.3 V) x 2048 = 2068.48 spread by the per-conversion and
    per-start rms (sqrt(1.07^2 + 0.32^2) = 1.12 codes for one conversion per
    start), as integer codes; the same seed gives the same codes in another
    run, another seed others."""
    runs = []
    for seed in (5, 5, 6):
        with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
            runs.append(_noisy_codes(sim, seed, 400))
    codes = runs[0]
    mean = sum(codes) / len(codes)
    rms = (sum((c - mean) ** 2 for c in codes) / len(codes)) ** 0.5
    assert abs(mean - 2068.48) < 0.25, f"mean {mean:.2f}"
    assert 0.95 < rms < 1.3, f"rms {rms:.2f} codes"
    assert runs[1] == codes, "the same seed drew differently"
    assert runs[2] != codes, "another seed drew the same"


def _single_ended_codes(sim, channel, n):
    """n single conversions of single-ended `channel` at 1.0 V, each its own
    ADSTART, through whatever the ECU role's catalogue gives it."""
    _wr(sim, CR, ADEN)
    sim.monitor(f"sysbus.adc3_h73x SetVoltage 1000000 {channel}")
    _wr(sim, SQR1, channel << 6)
    _wr(sim, ISR, 0x7FF)
    codes = []
    for _ in range(n):
        _wr(sim, CR, ADEN | ADSTART)
        sim.run_for(us=10)
        codes.append(_rd(sim, DR))
    return codes


def test_the_ecus_sampled_pins_carry_the_derived_noise_and_repeat(images):
    """#326: APPS_1 (PF8, INP7) single-ended carries the ECU role's
    datasheet-derived 0.42 codes rms (catalog/boards/mainlite.yaml): around
    the ideal 1.0 / 3.3 x 4095 = 1240.91, rounding adding its own 0.29
    (sqrt(0.42^2 + 1/12) = 0.51), never past the draw's bound (4.9 rms, 2.06
    codes: 1239..1243 once rounded); the same seed repeats, another differs. A
    pin the role leaves ideal (PF10, INP6, never sampled) reads one code."""
    runs = []
    for i, seed in enumerate((5, 5, 6)):
        with Sim(REPO / "systems" / "ecu.yaml", images("ecu"), seed=seed) as sim:
            runs.append(_single_ended_codes(sim, 7, 400))
            if i == 0:
                assert set(_single_ended_codes(sim, 6, 50)) == {1241}, "an ideal pin read noisily"
    codes = runs[0]
    mean = sum(codes) / len(codes)
    rms = (sum((c - mean) ** 2 for c in codes) / len(codes)) ** 0.5
    assert abs(mean - 1240.91) < 0.1, f"mean {mean:.2f}"
    assert 0.42 < rms < 0.62, f"rms {rms:.2f} codes"
    assert all(1239 <= c <= 1243 for c in codes), sorted(set(codes))
    assert runs[1] == codes, "the same seed drew differently"
    assert runs[2] != codes, "another seed drew the same"
