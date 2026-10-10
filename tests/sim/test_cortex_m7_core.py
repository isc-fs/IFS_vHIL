"""The Cortex-M7 core's DWT cycle counter and L1 cache controls
(platforms/cpus/stm32h733.repl `dwt`, models/renode/VhilCaches.cs,
docs/cpu-timing.md "The cycle counter").

Every MainLite image starts CYCCNT in the CAN bootloader (DEMCR.TRCENA,
DWT_CTRL.CYCCNTENA; stm32-can-bootloader main.c:489-491) and the app inherits
it, so the AMS shows it on any build. The I-cache is the AMS's from
IFS08-CE-AMS#637 (SCB_EnableICache in main()); on a build without it those
tests check that it stays off.
"""
import pytest

BOARD = "ams"
SYSCLK_MHZ = 528                 # AMS/ECU/bootloader main.c SystemClock_Config
DWT_CTRL, DWT_CYCCNT = 0xE0001000, 0xE0001004
CCR, CCR_IC = 0xE000ED14, 1 << 17


def _read(sim, address: int) -> int:
    return int(sim.monitor(f"sysbus ReadDoubleWord {address:#x}", board=BOARD).strip(), 0)


def _caches(sim, prop: str) -> str:
    return sim.monitor(f"vhil_caches_{BOARD} {prop}", board=BOARD).strip()


@pytest.fixture(scope="module")
def ams(make_sim):
    sim = make_sim("ams")
    sim.run_for(ms=100)
    return sim


def test_the_bootloader_leaves_the_cycle_counter_running(ams):
    assert _read(ams, DWT_CTRL) & 1, "DWT_CTRL.CYCCNTENA clear after the bootloader"


def test_the_cycle_counter_counts_sysclk_in_virtual_time(ams):
    # Read at sync points, where the monitor's time and the counter agree:
    # CYCCNT advances SYSCLK cycles per virtual us, so each instruction adds
    # 528 / cpu.mips of them.
    t0, c0 = ams.now_us(), _read(ams, DWT_CYCCNT)
    ams.run_for(ms=10)
    t1, c1 = ams.now_us(), _read(ams, DWT_CYCCNT)
    cycles = (c1 - c0) % (1 << 32)
    assert cycles == pytest.approx(SYSCLK_MHZ * (t1 - t0), abs=SYSCLK_MHZ)


def test_ccr_ic_reads_back_what_the_firmware_wrote(ams):
    enabled = _caches(ams, "ICacheEnabled") == "True"
    assert bool(_read(ams, CCR) & CCR_IC) == enabled
    invalidations = int(_caches(ams, "ICacheInvalidations"), 0)
    # SCB_EnableICache invalidates once, then sets CCR.IC (cachel1_armv7.h:57-70).
    assert invalidations == (1 if enabled else 0)


def test_every_reset_turns_the_i_cache_off(make_sim):
    sim = make_sim("ams")
    sim.run_for(ms=100)
    if _caches(sim, "ICacheEnabled") != "True":
        pytest.skip("this AMS build leaves the I-cache off (before IFS08-CE-AMS#637)")
    sim.power_cycle(BOARD)
    sim.run_for(ms=50)                       # in the bootloader, which never enables it
    assert not _read(sim, CCR) & CCR_IC
    sim.wait_for_app()
    sim.run_for(ms=100)
    assert _read(sim, CCR) & CCR_IC
