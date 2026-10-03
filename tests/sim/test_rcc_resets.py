"""RCC peripheral resets, RCC_AHB3RSTR .. RCC_APB4RSTR (#68), modelled per
RM0468 Rev 3 §8.7.27-8.7.35 (models/renode/VhilRccResets.cs).

Facts:
  RM0468: each RSTR bit "1: resets the <X> block", reset value 0x0000 0000;
    a peripheral comes back from a 1-then-0 pulse with its registers at their
    reset values. FDCANRST (APB1HRSTR bit 8) resets the whole FDCAN block.
    GPIO port resets (AHB4RSTR) do not change levels driven from outside.
  The CAN bootloader's HAL_DeInit pulses every bus with the H72x/H73x force
    values (stm32h7xx_hal.c HAL_DeInit, stm32h7xx_hal_rcc.h
    __HAL_RCC_<bus>_FORCE_RESET for STM32H7_DEV_ID 0x483) before it jumps to
    the application (can-bootloader v1.7.0 main.c:945).
Register addresses: RCC_BASE 0x58024400 and the peripheral bases are from
stm32h733xx.h (RCC_BASE line 2288).

The register-level tests poke a board at t = 0, before its first instruction,
so the firmware never sees the peripherals they reset.
"""
import pytest

from vhil import peripheral_guard as guard
from vhil.sim import Sim
from vhil.system import REPO

RCC = 0x58024400
AHB3, AHB1, AHB2, AHB4, APB3, APB1L, APB1H, APB2, APB4 = (
    0x7C, 0x80, 0x84, 0x88, 0x8C, 0x90, 0x94, 0x98, 0x9C)
# __HAL_RCC_<bus>_FORCE_RESET on the H72x/H73x (stm32h7xx_hal_rcc.h:4667,
# 4737, 4781, 4847, 4913, 4935, 4940, 5029, 5096): every defined bit.
FORCE = {AHB3: 0x00E95011, AHB1: 0x02008023, AHB2: 0x00030271, AHB4: 0x032806FF,
         APB3: 0x00000008, APB1L: 0xEAFFC3FF, APB1H: 0x03000136, APB2: 0x405730F3,
         APB4: 0x0420DEAA}
AUTO_JUMP_MS = 2000

# (name in the repl, register address, value to write, RSTR, bit). Bases from
# stm32h733xx.h: TIM23 0x4000E000, USART10 0x40011C00, SPI1 0x40013000,
# FDCAN1/2/3 0x4000A000/0x4000A400/0x4000D400, ADC3 0x58026000, SDMMC1
# 0x52007000, TIM2 0x40000000, GPIOD 0x58020C00, USART3 0x40004800.
CASES = [
    ("timer23",   0x4000E000 + 0x2C, 0x1234,     APB1H, 24),   # ARR
    ("usart10",   0x40011C00 + 0x0C, 0x01A1,     APB2,  7),    # BRR
    ("spi1",      0x40013000 + 0x08, 0x00070017, APB2,  12),   # CFG1
    ("fdcan1_h7", 0x4000A000 + 0x18, 0x00000003, APB1H, 8),    # CCCR INIT|CCE
    ("fdcan2_h7", 0x4000A400 + 0x18, 0x00000003, APB1H, 8),
    ("fdcan3_h7", 0x4000D400 + 0x18, 0x00000003, APB1H, 8),
    ("adc3_h73x", 0x58026000 + 0x14, 0x00000007, AHB4,  24),   # SMPR1
    ("sdmmc1",    0x52007000 + 0x08, 0x00001234, AHB3,  16),   # ARGR
    ("timer2",    0x40000000 + 0x2C, 0x1234,     APB1L, 0),    # ARR, H743 bit too
    ("usart3",    0x40004800 + 0x0C, 0x01A1,     APB1L, 18),   # BRR
    ("gpioPortD", 0x58020C00 + 0x00, 0x55555555, AHB4,  3),    # MODER
]


def _rd(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}").strip(), 16)


def _wr(sim, address, value):
    sim.monitor(f"sysbus WriteDoubleWord {address:#x} {value:#x}")


def _resets(sim, board, name):
    """How often a reset bit has reset `name` (VhilRccResets.ResetsOf)."""
    return int(sim.monitor(f'vhil_rcc_{board} ResetsOf "{name}"', board=board).strip(), 0)


def _pulse(sim, offset, mask):
    _wr(sim, RCC + offset, mask)
    _wr(sim, RCC + offset, 0)


@pytest.fixture
def ecu(firmware):
    """An ECU at t = 0: platform loaded, no instruction run yet."""
    with Sim(REPO / "systems" / "ecu.yaml", {"ecu": firmware("ecu")}) as sim:
        yield sim


@pytest.mark.parametrize("name, address, value, offset, bit", CASES,
                         ids=[c[0] for c in CASES])
def test_a_reset_pulse_restores_the_reset_value(ecu, name, address, value, offset, bit):
    reset_value = _rd(ecu, address)
    _wr(ecu, address, value)
    assert _rd(ecu, address) != reset_value, f"{name}: the write didn't take, the test proves nothing"
    _pulse(ecu, offset, 1 << bit)
    assert _rd(ecu, address) == reset_value
    assert _resets(ecu, "ecu", name) == 2   # set and release


def test_a_held_bit_keeps_the_peripheral_in_reset(ecu):
    """While TIM23RST reads 1, writes to TIM23 don't stick (RM0468: the block
    is held in reset); released, the timer takes them again."""
    arr = 0x4000E000 + 0x2C
    _wr(ecu, RCC + APB1H, 1 << 24)
    _wr(ecu, arr, 0x1234)
    _wr(ecu, RCC + APB1H, 0)
    assert _rd(ecu, arr) == 0xFFFFFFFF
    _wr(ecu, arr, 0x1234)
    assert _rd(ecu, arr) == 0x1234


def test_the_h73x_bits_are_held_and_read_back(ecu):
    """Every defined bit, the H72x/H73x-only ones included (TIM23/24, USART10,
    UART9, I2C5, DTS, OCTOSPI2, FMAC, CORDIC, OTFDEC, IOMNGR, DFSDM1 at 30),
    reads back what was written; a release reads 0."""
    for offset, mask in FORCE.items():
        _wr(ecu, RCC + offset, mask)
        assert _rd(ecu, RCC + offset) == mask, f"RSTR {offset:#x}"
        _wr(ecu, RCC + offset, 0)
        assert _rd(ecu, RCC + offset) == 0, f"RSTR {offset:#x}"


def test_single_bit_read_modify_write(ecu):
    """__HAL_RCC_<X>_FORCE_RESET is RSTR |= bit, RELEASE is RSTR &= ~bit."""
    _wr(ecu, RCC + APB2, 1 << 7)                       # USART10RST
    _wr(ecu, RCC + APB2, _rd(ecu, RCC + APB2) | 1 << 12)  # SPI1RST
    assert _rd(ecu, RCC + APB2) == (1 << 7) | (1 << 12)
    _wr(ecu, RCC + APB2, _rd(ecu, RCC + APB2) & ~(1 << 7))
    assert _rd(ecu, RCC + APB2) == 1 << 12
    assert _resets(ecu, "ecu", "usart10") == 2
    assert _resets(ecu, "ecu", "spi1") == 1


def test_peripherals_without_a_reset_bit_are_untouched(ecu):
    """DMAMUX1 has no RSTR bit (RM0468 §8.7.28: AHB1RSTR has DMA1/DMA2 only),
    nor the IWDG: a full HAL_DeInit sequence leaves them be. Every model on a
    bit takes its reset without error."""
    dmamux_c0cr = 0x40020800
    _wr(ecu, dmamux_c0cr, 0x5)
    for offset, mask in FORCE.items():
        _pulse(ecu, offset, mask)
    assert _rd(ecu, dmamux_c0cr) == 0x5


def test_a_gpio_reset_keeps_levels_driven_from_outside(ecu):
    """START (PB5) held HIGH from outside: GPIOBRST puts PB's modes back but
    the pin still reads HIGH, as on silicon."""
    gpiob = 0x58020400
    moder_reset = _rd(ecu, gpiob)
    _wr(ecu, gpiob, moder_reset & ~(0x3 << 10))        # PB5 input
    ecu.io("ecu").set_input("sysbus.gpioPortB", 5, True)
    assert _rd(ecu, gpiob + 0x10) & (1 << 5)
    _pulse(ecu, AHB4, 1 << 1)
    assert _rd(ecu, gpiob) == moder_reset
    assert _rd(ecu, gpiob + 0x10) & (1 << 5), "PB5 lost its external level"


def test_reserved_bits_and_cpurst_are_flagged(firmware, tmp_path):
    """A reserved bit (APB1HRSTR bit 0) or AHB3RSTR.CPURST (a CPU reset, not
    modelled) is dropped and logged where the peripheral guard sees it."""
    log = tmp_path / "ecu.log"
    with Sim(REPO / "systems" / "ecu.yaml", {"ecu": firmware("ecu")}, log_path=log) as sim:
        _wr(sim, RCC + APB1H, 1 << 0 | 1 << 24)
        assert _rd(sim, RCC + APB1H) == 1 << 24
        _wr(sim, RCC + AHB3, 1 << 31)
        assert _rd(sim, RCC + AHB3) == 0
    found = {(k[1], k[2]) for k, _ in _guard(log)}
    assert {("rcc", APB1H), ("rcc", AHB3)} <= found, found


def test_a_power_cycle_clears_the_registers(ecu):
    _wr(ecu, RCC + APB1H, 1 << 24)
    ecu.power_cycle("ecu")
    assert _rd(ecu, RCC + APB1H) == 0


# -- through the bootloader ----------------------------------------------------

def _bl(firmware, board, tmp_path):
    fw = {board: firmware(board), f"{board}.bootloader": firmware("can-bootloader")}
    return Sim(REPO / "systems" / f"{board}-bl.yaml", fw, log_path=tmp_path / f"{board}-bl.log")


def _guard(log):
    return guard.unexplained(log.read_text(errors="replace"), guard.load_rules())


def test_the_ecu_bootloader_resets_its_peripherals_before_the_app(firmware, tmp_path):
    """HAL_DeInit pulses every bit once: each model the ECU uses comes back
    at reset, the app starts as on ecu.yaml, and nothing in the handoff is
    unexplained to the peripheral guard (no KNOWN GAP #68 entries)."""
    with _bl(firmware, "ecu", tmp_path) as sim:
        sim.run_for(ms=AUTO_JUMP_MS + 1000)
        assert sim.can("can_acu").count([0x100]) > 0, "the application never started"
        # The ECU app pulses FDCANRST once more itself, before MX_FDCAN*_Init
        # (IFS08-CE-ECU dev Core/Src/main.c:112-113, "Bootloader-handoff
        # hardening"): 2 edges from HAL_DeInit + 2 from the app.
        for name, edges in (("fdcan1_h7", 4), ("fdcan2_h7", 4), ("fdcan3_h7", 4),
                            ("timer23", 2), ("usart10", 2), ("gpioPortB", 2),
                            ("gpioPortD", 2), ("adc3_h73x", 2)):
            assert _resets(sim, "ecu", name) == edges, name
    assert _guard(tmp_path / "ecu-bl.log") == []


def test_the_ams_comes_up_healthy_after_the_bootloader(firmware, tmp_path):
    """The AMS app after HAL_DeInit reset SPI1, SDMMC1 and ADC3 under it: a
    healthy Start with AMS_OK, every cell read over the reset SPI1 (as on
    ams.yaml, test_ams_boot.py), and a clean guard."""
    with _bl(firmware, "ams", tmp_path) as sim:
        sim.run_for(ms=AUTO_JUMP_MS + 4000)
        for name in ("spi1", "sdmmc1", "adc3_h73x", "fdcan1_h7"):
            assert _resets(sim, "ams", name) == 2, name
        status = sim.can("can_acu").last(0x4A0)
        assert status is not None, "the AMS app never reported"
        assert status.data[0] == 0 and status.data[1] == 1, f"0x4A0 {status.data.hex()}"
        assert int.from_bytes(status.data[4:6], "big") == 3700
        assert int.from_bytes(status.data[6:8], "big") == 3700
        pack = int.from_bytes(sim.can("can_acu").last(0x4A1).data[0:4], "little")
        assert pack == 95 * 3700
    assert _guard(tmp_path / "ams-bl.log") == []
