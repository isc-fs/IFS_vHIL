"""ams-faults: a CPU fault in the AMS with the tractive system live.

AMS facts (IFS08-CE-AMS, Core/ unless shown):
  stm32h7xx_it.c:55-78, 110-170: HardFault, MemManage, BusFault and
    UsageFault call ams_fault_landing(reason): ams_relays_open_all_c() FIRST
    (relay_driver.cpp:22-40, one BSRR write opening AIR+ PB5, AIR- PB6 and
    PRE PB7), then the BKP3R last-fault stamp; then they spin until the IWDG
    resets the AMS (~100 ms nominal: reload 100 at LSI/32, main.c:553-570).
    docs/ARCHITECTURE.md:150-173 (item 9): relays first is the point, a fault
    in Run would otherwise hold both AIRs closed across a live pack with no
    firmware running; these handlers deliberately do NOT set the ErrorLatch,
    the BKP3R sentinel makes the crash visible on the next boot instead.
  ams_config.hpp:989-1010: 0x6CA byte 7 last_fault, LastFault HardFault 1,
    MemManage 5, BusFault 6, UsageFault 7; BKP3R survives the IWDG reset.
Fault injection, as test_ecu_robustness.py does it (nothing patched in the
image): ams_watchdog_refresh's saved LR loses its Thumb bit on entry, so its
`pop {r7, pc}` takes an INVSTATE UsageFault (HardFault when UsageFault is not
enabled). SafetyTask calls it every tick (safety_task.cpp:259, 363), so the
fault lands in the safety task itself, in Run.
"""
import pytest

from ams_car import AIR_N, AIR_P, GPIOB, PRECHARGE_RELAY, RUN, START, Car
from vhil import elf
from vhil.sim import Sim
from vhil.system import REPO

FW_HEALTH = 0x6CA
LAST_FAULT = {"HardFault_Handler": 1, "MemManage_Handler": 5, "BusFault_Handler": 6,
              "UsageFault_Handler": 7}
IWDG_MS = 100


@pytest.fixture
def car(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=3000)
        yield Car(sim)


def _addr(sim, symbol):
    return elf.symbol(sim.firmware["ams"], symbol)[0] & ~1


def _pc(sim):
    return int(sim.monitor("cpu PC", board="ams").strip(), 16)


def _fault_in_the_safety_task(sim):
    """Clear the Thumb bit of ams_watchdog_refresh's return address; return
    the handler the CPU landed in and the virtual time it got there."""
    handlers = {name: _addr(sim, name) for name in LAST_FAULT}
    at = _addr(sim, "ams_watchdog_refresh")
    sim.monitor(f'cpu AddHook {at:#x} "from Antmicro.Renode.Peripherals.CPU import RegisterValue; '
                f'self.LR = RegisterValue.Create(self.LR.RawValue & 0xFFFFFFFE, 32)"', board="ams")
    landed = []

    def in_handler():
        pc = _pc(sim)
        for name, a in handlers.items():
            if a <= pc < a + 64:
                landed.append((name, sim.now_us()))
                return True
        return False
    try:
        sim.run_until(in_handler, timeout_ms=50, step_ms=0.1)
    finally:
        sim.monitor(f"cpu RemoveHooksAt {at:#x}", board="ams")
    assert landed, "no fault handler reached"
    return landed[0]


def test_a_cpu_fault_in_run_opens_the_contactors_before_the_watchdog(car):
    """In Run, a fault in SafetyTask: AIR+, AIR- and PRE open within the
    fault handler's first instructions (well inside a millisecond), not at
    the watchdog reset ~100 ms later; the IWDG then resets the AMS."""
    sim = car.sim
    pins = {n: car.io.watch(GPIOB, p) for n, p in
            {"air_p": AIR_P, "air_n": AIR_N, "pre": PRECHARGE_RELAY}.items()}
    car.to_run()
    sim.run_for(ms=200)
    assert car.state() == RUN and car.io.level(pins["air_p"]) and car.io.level(pins["air_n"])
    handler, t_fault = _fault_in_the_safety_task(sim)
    sim.run_for(ms=1)
    for name in ("air_p", "air_n"):
        falls = [e for e in car.io.edges(pins[name], since_us=t_fault - 1000) if not e.level]
        assert falls, f"{name} still closed 1 ms into {handler}"
        assert falls[0].t_us - t_fault <= 500, f"{name} opened {falls[0].t_us - t_fault} us after the fault"
    assert not car.io.level(pins["pre"])
    reset = sim.run_until(lambda: not sim.in_app("ams"), timeout_ms=2 * IWDG_MS + 50, step_ms=1)
    assert reset, "the IWDG never reset the spinning AMS"
    assert not any(car.io.level(p) for p in pins.values()), "a contactor closed again before the reset"


def test_the_next_boot_reports_the_fault_and_starts_unlatched(car):
    """After the IWDG reset the AMS reports the fault class on 0x6CA byte 7
    (BKP3R survives the reset) and, as ARCHITECTURE.md item 9 intends,
    boots to Start rather than Error: a CPU fault is not a pack fault."""
    sim = car.sim
    car.to_run()
    handler, _ = _fault_in_the_safety_task(sim)
    sim.run_until(lambda: not sim.in_app("ams"), timeout_ms=2 * IWDG_MS + 50, step_ms=1)
    t = sim.now_us()
    sim.wait_for_app()
    sim.run_for(ms=3000)
    health = sim.can("can_acu").frames(FW_HEALTH, since_us=t)
    assert health, "no 0x6CA after the reboot"
    assert health[0].data[7] == LAST_FAULT[handler], \
        f"0x6CA last_fault {health[0].data[7]} after {handler}"
    assert (car.state(), car.reason()) == (START, 0)
