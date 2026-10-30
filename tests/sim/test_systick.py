"""The Cortex-M7 SysTick: a period of RELOAD + 1 clocks (#294).

Facts (ARMv7-M ARM DDI0403E, B3.3 "The system timer, SysTick"):
  - The counter counts down from RELOAD to 0, and on the clock after 0 loads
    RELOAD again; its step from 1 to 0 sets COUNTFLAG (SYST_CSR bit 16) and,
    with TICKINT, pends the SysTick exception. SYST_RVR "Calculating the
    RELOAD value": a RELOAD of N - 1 gives a period of N clocks, which is why
    every 1 ms tick at 528 MHz writes 527999 (CMSIS SysTick_Config:
    `SysTick->LOAD = ticks - 1`; FreeRTOS port.c: `configSYSTICK_CLOCK_HZ /
    configTICK_RATE_HZ - 1`).
  - COUNTFLAG reads 1 if the counter reached 0 since the last read, and the
    read clears it; so does any write to SYST_CVR.
  - A write to SYST_CVR clears it to 0 (whatever the value); the counter
    loads RELOAD on the next clock, and that load sets no COUNTFLAG.
  - RELOAD = 0 stops the counter at 0 at its next wrap.
  - CLKSOURCE (bit 2) selects the processor clock (1) or the external
    reference (0); Renode's NVIC has no external reference: it reads 1.
Registers (SysTick_Type, core_cm7.h): CTRL 0xE000E010, LOAD 0xE000E014,
VAL 0xE000E018.

Renode's NVIC fired when its clock entry reached 0 and gave it RELOAD:
RELOAD clocks per period, and VAL never read 0 while counting. The vHIL's
Renode patch 0012 (docker/renode/) counts RELOAD + 1. Every MainLite image
ticks on SysTick at 528 MHz with RELOAD 527999: the CAN bootloader's HAL
tick, and each app's FreeRTOS tick (the apps' HAL tick is a TIMx,
test_stm32_timer.py).
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

# Impact selection (ci/impact.yaml, docs/ci.md): boards, systems, subsystems.
pytestmark = [pytest.mark.ecu, pytest.mark.system_ecu, pytest.mark.timers]

CTRL, LOAD, VAL = 0xE000E010, 0xE000E014, 0xE000E018
ENABLE, CLKSOURCE, COUNTFLAG = 1 << 0, 1 << 2, 1 << 16
SYSCLK = 528_000_000        # every MainLite image (AMS/ECU/bootloader main.c SystemClock_Config)


@pytest.fixture
def ecu(images):
    """An ECU at power-on with its core halted: SysTick is the test's."""
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        sim.monitor("cpu IsHalted true")
        yield sim


def _rd(sim, address):
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}").strip(), 16)


def _wr(sim, address, value):
    sim.monitor(f"sysbus WriteDoubleWord {address:#x} {value:#x}")


def _start(sim, hz, reload):
    """SysTick at hz, stopped, VAL cleared, RELOAD written, then enabled
    without TICKINT (the order FreeRTOS's vPortSetupTimerInterrupt uses)."""
    sim.monitor(f"sysbus.nvic Frequency {hz}")
    _wr(sim, CTRL, 0)
    _wr(sim, VAL, 0)
    _wr(sim, LOAD, reload)
    _wr(sim, CTRL, ENABLE)


def _state(sim):
    """(VAL, COUNTFLAG); reading CTRL clears COUNTFLAG."""
    return _rd(sim, VAL), 1 if _rd(sim, CTRL) & COUNTFLAG else 0


@pytest.mark.parametrize("reload", [99, 1])
def test_val_counts_reload_to_zero_and_the_period_is_reload_plus_one(ecu, reload):
    """At 1 MHz, one clock per us: VAL reads 0 after the write, RELOAD one
    clock later, then down to 0, where COUNTFLAG sets, RELOAD + 1 clocks
    after the write; and every RELOAD + 1 clocks after that."""
    _start(ecu, 1_000_000, reload)
    assert _state(ecu) == (0, 0), "a write to VAL clears it and COUNTFLAG"
    seen = []
    for _ in range(reload + 2):
        ecu.run_for(us=1)
        seen.append(_state(ecu))
    assert seen == [(v, 0) for v in range(reload, 0, -1)] + [(0, 1), (reload, 0)]
    for _ in range(3):
        ecu.run_for(us=reload)
        assert _state(ecu) == (0, 1)
        ecu.run_for(us=1)
        assert _state(ecu) == (reload, 0)
    # 1000 periods on: no drift.
    ecu.run_for(us=1000 * (reload + 1) - 1)
    assert _state(ecu) == (0, 1)


def test_the_1ms_tick_is_528000_clocks(ecu):
    """The images' 1 ms tick (RELOAD 527999 at 528 MHz) wraps every 1000 us
    of virtual time exactly: VAL is 0 with COUNTFLAG set at each ms, 528 a us
    before it. With RELOAD clocks per period it wrapped every 999.998 us."""
    _start(ecu, SYSCLK, SYSCLK // 1000 - 1)
    for ms in range(1, 4):
        ecu.run_for(us=999)
        assert _state(ecu) == (SYSCLK // 1_000_000, 0), f"ms {ms}"
        ecu.run_for(us=1)
        assert _state(ecu) == (0, 1), f"ms {ms}"
    ecu.run_for(ms=1000)
    assert _state(ecu) == (0, 1), "1000 ms on"


def test_countflag_reads_once_and_a_val_write_clears_it(ecu):
    _start(ecu, 1_000_000, 9)
    ecu.run_for(us=25)                       # wrapped at 10 and 20 us
    assert _rd(ecu, CTRL) & COUNTFLAG
    assert not _rd(ecu, CTRL) & COUNTFLAG, "the read clears COUNTFLAG"
    ecu.run_for(us=5)                        # wrapped at 30 us
    assert _rd(ecu, VAL) == 0
    _wr(ecu, VAL, 0x1234)                    # any value clears it
    assert _state(ecu) == (0, 0)
    ecu.run_for(us=1)
    assert _state(ecu) == (9, 0), "the load after a VAL write sets no COUNTFLAG"
    ecu.run_for(us=9)
    assert _state(ecu) == (0, 1)


def test_reload_zero_stops_the_counter_at_its_next_wrap(ecu):
    _start(ecu, 1_000_000, 9)
    ecu.run_for(us=3)
    _wr(ecu, LOAD, 0)
    assert _state(ecu) == (7, 0), "a RELOAD write does not change the count"
    ecu.run_for(us=7)
    assert _state(ecu) == (0, 1)
    ecu.run_for(us=50)
    assert _state(ecu) == (0, 0), "stopped at 0: no further wrap"
    # A new RELOAD restarts it from 0: RELOAD + 1 clocks to the next wrap.
    _wr(ecu, LOAD, 4)
    ecu.run_for(us=1)
    assert _state(ecu) == (4, 0)
    ecu.run_for(us=4)
    assert _state(ecu) == (0, 1)


def test_a_reload_write_mid_count_takes_effect_at_the_next_wrap(ecu):
    _start(ecu, 1_000_000, 9)
    ecu.run_for(us=3)
    _wr(ecu, LOAD, 19)
    ecu.run_for(us=7)
    assert _state(ecu) == (0, 1), "the count in progress kept the old RELOAD"
    ecu.run_for(us=1)
    assert _state(ecu) == (19, 0)
    ecu.run_for(us=19)
    assert _state(ecu) == (0, 1)


def test_the_counter_loads_the_reload_written_while_it_is_at_zero(ecu):
    """VAL written before LOAD (FreeRTOS's order): the counter is at 0 when
    the new RELOAD arrives and loads that one, not the RELOAD it had."""
    sim = ecu
    _start(sim, 1_000_000, 99)
    _wr(sim, CTRL, 0)
    _wr(sim, VAL, 0)
    _wr(sim, LOAD, 9)
    _wr(sim, CTRL, ENABLE)
    sim.run_for(us=1)
    assert _state(sim) == (9, 0)
    sim.run_for(us=9)
    assert _state(sim) == (0, 1)


def test_clksource_reads_the_processor_clock(ecu):
    _wr(ecu, CTRL, 0)
    assert _rd(ecu, CTRL) & CLKSOURCE
    _wr(ecu, CTRL, ENABLE)                   # CLKSOURCE = 0 written: ignored
    assert _rd(ecu, CTRL) & CLKSOURCE
