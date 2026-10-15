"""STM32 timers: the update event every (ARR + 1) x (PSC + 1) timer clocks (#292).

Facts (RM0468 Rev 3):
  - 44.3.2 "Counter modes" (TIM2/3/4/5/23/24; the same in 43.3.2 for TIM1/8
    and 47.3.2 for TIM6/7): counting up, the counter counts from 0 to the
    auto-reload value (TIMx_ARR), then restarts from 0 and generates a
    counter overflow event; counting down, from ARR down to 0, then restarts
    from ARR and generates an underflow event. ARR + 1 values either way.
    The update event sets UIF (TIMx_SR bit 0); UG (TIMx_EGR bit 0)
    re-initializes the counter, to 0 up and to ARR down.
  - 44.4.14 TIMx_PSC: "The counter clock frequency CK_CNT is equal to
    fCK_PSC / (PSC[15:0] + 1)".
Register offsets (TIM_TypeDef) and bases from stm32h733xx.h: TIM2 0x40000000,
TIM6 0x40001000, TIM23 0x4000E000 (D2_APB1PERIPH_BASE + 0x0000 / 0x1000 /
0xE000), TIM1 0x40010000 (D2_APB2PERIPH_BASE).

Which timers the firmwares run (each at a 1 MHz counter clock, PSC = timer
clock / 1 MHz - 1): the HAL's 1 kHz timebase, ARR = 999, on TIM23 (ECU,
IFS08-CE-ECU stm32h7xx_hal_timebase_tim.c:75-92), TIM6 (AMS, IFS08-CE-AMS
stm32h7xx_hal_timebase_tim.c:84-101) and TIM1 (uDV, IFS09-DV-uDV
stm32h7xx_hal_timebase_tim.c:75-91); the uDV's 400 Hz IMU tick, TIM2 with
PSC = 263 and ARR = 2499 (tim.c:43-46). The CAN bootloader and FreeRTOS tick
on SysTick, not a TIMx.

Renode's STM32_Timer counted ARR values per period (its clock entry's limit
was ARR): the HAL tick came every 999 us and TIM2's every 2.499 ms. The
vHIL's Renode patch 0007 (docker/renode/) counts ARR + 1.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

CR1, DIER, SR, EGR, CNT, PSC, ARR = 0x00, 0x0C, 0x10, 0x14, 0x24, 0x28, 0x2C
CEN, DIR, UIF, UG = 1 << 0, 1 << 4, 1 << 0, 1 << 0
TIMERS = {"timer23": 0x4000E000, "timer2": 0x40000000, "timer6": 0x40001000,
          "timer1": 0x40010000}


@pytest.fixture
def ecu(images):
    """An ECU at power-on with its core halted: the timers are the test's."""
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        sim.monitor("cpu IsHalted true")
        yield sim


def _rd(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}").strip(), 16)


def _wr(sim, address, value):
    sim.monitor(f"sysbus WriteDoubleWord {address:#x} {value:#x}")


def _start(sim, name, us_per_count, arr, down=False):
    """Count every us_per_count us (PSC from the timer's clock) up to ARR =
    arr, re-initialized by UG, UIF cleared, then enabled."""
    base = TIMERS[name]
    clock = int(sim.monitor(f"sysbus.{name} Frequency").strip(), 0)
    assert clock * us_per_count % 1_000_000 == 0, clock
    _wr(sim, base + PSC, clock * us_per_count // 1_000_000 - 1)
    _wr(sim, base + ARR, arr)
    _wr(sim, base + CR1, DIR if down else 0)
    _wr(sim, base + EGR, UG)
    _wr(sim, base + SR, 0)
    _wr(sim, base + CR1, (DIR if down else 0) | CEN)
    return base


@pytest.mark.parametrize("name", TIMERS)
@pytest.mark.parametrize("us_per_count, arr", [(1, 999), (2, 49)])
def test_the_update_event_comes_every_arr_plus_one_counts(ecu, name, us_per_count, arr):
    """Counting up: CNT reaches ARR, the overflow comes one count later, and
    every period after it is (ARR + 1) x (PSC + 1) timer clocks."""
    base = _start(ecu, name, us_per_count, arr)
    period = (arr + 1) * us_per_count
    ecu.run_for(us=arr * us_per_count)
    assert (_rd(ecu, base + CNT), _rd(ecu, base + SR) & UIF) == (arr, 0), "overflowed at ARR"
    ecu.run_for(us=us_per_count)
    assert (_rd(ecu, base + CNT), _rd(ecu, base + SR) & UIF) == (0, UIF)
    for _ in range(3):
        _wr(ecu, base + SR, 0)
        ecu.run_for(us=period - us_per_count)
        assert (_rd(ecu, base + CNT), _rd(ecu, base + SR) & UIF) == (arr, 0)
        ecu.run_for(us=us_per_count)
        assert (_rd(ecu, base + CNT), _rd(ecu, base + SR) & UIF) == (0, UIF)
    # 1000 periods on: no drift.
    _wr(ecu, base + SR, 0)
    ecu.run_for(us=1000 * period - us_per_count)
    assert _rd(ecu, base + CNT) == arr


@pytest.mark.parametrize("name", ["timer23", "timer2", "timer1"])
def test_counting_down_the_underflow_comes_after_zero(ecu, name):
    """Counting down: UG loads ARR, CNT reaches 0 after ARR counts and the
    underflow (back to ARR) comes one count later."""
    arr = 99
    base = _start(ecu, name, 1, arr, down=True)
    assert _rd(ecu, base + CNT) == arr
    ecu.run_for(us=arr)
    assert (_rd(ecu, base + CNT), _rd(ecu, base + SR) & UIF) == (0, 0), "underflowed at 1"
    ecu.run_for(us=1)
    assert (_rd(ecu, base + CNT), _rd(ecu, base + SR) & UIF) == (arr, UIF)
    _wr(ecu, base + SR, 0)
    ecu.run_for(us=(arr + 1) * 10 - 1)
    assert (_rd(ecu, base + CNT), _rd(ecu, base + SR) & UIF) == (0, UIF)


@pytest.mark.parametrize("system, board", [("ecu", "ecu"), ("ams", "ams"), ("udv", "udv")])
def test_the_hal_tick_is_one_millisecond(make_sim, system, board):
    """The HAL timebase (TIM23, TIM6 or TIM1 at 1 kHz) adds 1 to uwTick
    (stm32h7xx_hal.c HAL_IncTick) every 1000 us of virtual time: 5000 in 5 s,
    give or take the one in flight at either end. With ARR counts per period
    it was 5005."""
    sim = make_sim(system)
    sim.run_for(ms=100)
    t0, tick0 = sim.now_us(), sim.read_symbol(board, "uwTick", 4)
    sim.run_for(ms=5000)
    t1, tick1 = sim.now_us(), sim.read_symbol(board, "uwTick", 4)
    assert t1 - t0 == 5_000_000
    assert abs((tick1 - tick0) - 5000) <= 1, tick1 - tick0
