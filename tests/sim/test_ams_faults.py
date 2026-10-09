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

#196, the two paths that injection can't reach (AMS dev, Core/ unless shown):
  freertos.c:120-134: vApplicationMallocFailedHook counts the failure, opens
    the relays, sets the ErrorLatch (BKP1R = 0xA115EE51, ams_config.hpp:1027-
    1028, error_latch.cpp:39-41) and spins, meant to end in an IWDG reset
    and a boot in Error (docs/ARCHITECTURE.md:151-156, item 9; it spins in
    a task, so SafetyTask keeps feeding the dog: strict xfails). FatFs creates
    and deletes its volume's sync object (ff_cre_syncobj, syscall.c:36-58,
    _FS_REENTRANT in ffconf.h:251) at every mount attempt, so the AMS
    allocates in Run too: Sim.fail_malloc fails the next one, as a heap with
    no block big enough would.
  A bus error: Sim.bus_fault_at points HAL_IWDG_Refresh's handle (r0) at the
    platform's reserved range, so its `ldr r3, [r0]` takes a precise
    BusFault (models/renode/VhilBusError.cs). SafetyTask refreshes the dog
    every tick (ams_watchdog_refresh), so this one lands in the safety task
    too. Neither firmware sets SHCSR's MEMFAULTENA/BUSFAULTENA/USGFAULTENA
    (no write to SCB->SHCSR anywhere in Core/; the CAN bootloader doesn't
    either), so on the chip every BusFault and MemManage escalates to
    HardFault with HFSR.FORCED (ARMv7-M ARM B1.5.8): the MemManage, BusFault
    and UsageFault handlers and their reason codes 5-7 are unreachable. The
    tests that reach those handlers write SHCSR themselves, as a firmware
    that enabled them would, and say so.
"""
import pytest

from ams_car import AIR_N, AIR_P, ERROR, GPIOB, PRECHARGE_RELAY, RUN, START, Car
from vhil import elf
from vhil.sim import BUS_ERROR_ADDRESS, Sim
from vhil.system import REPO

FW_HEALTH = 0x6CA
LAST_FAULT = {"HardFault_Handler": 1, "MemManage_Handler": 5, "BusFault_Handler": 6,
              "UsageFault_Handler": 7}
IWDG_MS = 100
MALLOC_HOOK = "vApplicationMallocFailedHook"
ERROR_LATCH = 0xA115EE51                 # RTC BKP1R (ams_config.hpp:1027-1028)
RTC_BKP1R = 0x58004000 + 0x50 + 4       # RTC_BASE + BKP0R (stm32h733xx.h), BKP1R
# SCB fields (CMSIS core_cm7.h): SHCSR MEMFAULTENA 16, BUSFAULTENA 17,
# USGFAULTENA 18; HFSR FORCED 30; CFSR MMARVALID 7, DACCVIOL 1, BFARVALID 15,
# PRECISERR 9.
FAULT_ENABLES = 0x7 << 16
FORCED = 1 << 30
MMARVALID, DACCVIOL, BFARVALID, PRECISERR = 1 << 7, 1 << 1, 1 << 15, 1 << 9
# AMS issues behind the strict xfails below.
NO_FAULT_ENABLES = pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-AMS#633: nothing sets SHCSR MEMFAULTENA/BUSFAULTENA/USGFAULTENA, "
    "so a BusFault escalates to HardFault (ARMv7-M B1.5.8) and 0x6CA reports 1, never "
    "BusFault 6 (stm32h7xx_it.c:146-151, ams_config.hpp:1081-1088)"))


@pytest.fixture
def car(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=3000)
        yield Car(sim)


def _addr(sim, symbol):
    return elf.symbol(sim.firmware["ams"], symbol)[0] & ~1


def _fault_in_the_safety_task(sim):
    """Clear the Thumb bit of ams_watchdog_refresh's return address; return
    the handler the CPU landed in and the virtual time it got there."""
    handlers = set(LAST_FAULT)
    at = _addr(sim, "ams_watchdog_refresh")
    sim.monitor(f'cpu AddHook {at:#x} "from Antmicro.Renode.Peripherals.CPU import RegisterValue; '
                f'self.LR = RegisterValue.Create(self.LR.RawValue & 0xFFFFFFFE, 32)"', board="ams")
    landed = []

    def in_handler():
        name = sim.function_at("ams")
        if name in handlers:
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


# -- #196: allocation failure, bus faults ---------------------------------------

def _landed(sim, names, timeout_ms):
    """Run until the AMS's PC is in one of `names`; (name, virtual us)."""
    found = []

    def there():
        name = sim.function_at("ams")
        if name in names:
            found.append((name, sim.now_us()))
            return True
        return False
    sim.run_until(there, timeout_ms=timeout_ms, step_ms=0.1)
    return found[0]


def _contactors(car):
    return {n: car.io.watch(GPIOB, p) for n, p in
            {"air_p": AIR_P, "air_n": AIR_N, "pre": PRECHARGE_RELAY}.items()}


def _assert_opened_at(car, pins, t_us, what):
    car.sim.run_for(ms=1)
    for name in ("air_p", "air_n"):
        falls = [e for e in car.io.edges(pins[name], since_us=t_us - 1000) if not e.level]
        assert falls, f"{name} still closed 1 ms into {what}"
        assert falls[0].t_us - t_us <= 500, f"{name} opened {falls[0].t_us - t_us} us after {what}"
    assert not car.io.level(pins["pre"])


def _reboot(car):
    """Run until the IWDG resets the spinning AMS, then through its next
    boot; returns the time of the reset."""
    sim = car.sim
    reset = sim.run_until(lambda: not sim.in_app("ams"), timeout_ms=2 * IWDG_MS + 50, step_ms=1)
    sim.wait_for_app()
    sim.run_for(ms=3000)
    return reset


def _fail_an_allocation_in_run(car):
    sim = car.sim
    pins = _contactors(car)
    car.to_run()
    sim.run_for(ms=200)
    assert car.state() == RUN
    sim.fail_malloc("ams")
    _, t = _landed(sim, {MALLOC_HOOK}, timeout_ms=1000)
    return pins, t


def test_a_failed_allocation_in_run_opens_the_contactors_and_latches_error(car):
    """freertos.c:122-134: the heap refuses an allocation in Run. The hook
    opens AIR+, AIR- and PRE at once and sets the ErrorLatch."""
    sim = car.sim
    pins, t = _fail_an_allocation_in_run(car)
    _assert_opened_at(car, pins, t, MALLOC_HOOK)
    assert int(sim.monitor(f"sysbus ReadDoubleWord {RTC_BKP1R:#x}").strip(), 16) == ERROR_LATCH


MALLOC_HOOK_IN_A_TASK = pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-AMS#634: vApplicationMallocFailedHook (freertos.c:122-134) "
    "spins in the allocating task with interrupts on (heap_4 calls it after "
    "xTaskResumeAll): here SdLoggerTask (osPriorityLow, main.c:115-117), so SafetyTask "
    "(osPriorityRealtime) keeps feeding the IWDG and the AMS never resets; it reports "
    "Run with the contactors open until BmsStale (~550 ms), and never stamps "
    "LastFault::MallocFail (3) for 0x6CA"))


@MALLOC_HOOK_IN_A_TASK
def test_a_failed_allocation_resets_the_ams_into_error(car):
    """ARCHITECTURE.md:151-156 (item 9): the hook spins so the IWDG resets
    the node within ~100 ms, and the next boot comes up in Error."""
    sim = car.sim
    pins, _ = _fail_an_allocation_in_run(car)
    _reboot(car)
    assert car.state() == ERROR, f"state {car.state()} after the reset"
    assert not any(car.io.level(p) for p in pins.values()), "a contactor closed after the reset"


@MALLOC_HOOK_IN_A_TASK
def test_the_next_boot_names_the_failed_allocation(car):
    """ams_config.hpp:1079-1082: MallocFail (3) is a 0x6CA last_fault class."""
    sim = car.sim
    _fail_an_allocation_in_run(car)
    reset = _reboot(car)
    health = sim.can("can_acu").frames(FW_HEALTH, since_us=reset)
    assert health and health[0].data[7] == 3, f"0x6CA {health[0].data.hex() if health else None}"


def test_a_bus_error_escalates_to_hardfault(car):
    """What the car does: BUSFAULTENA is clear, so a precise BusFault on a
    load escalates to HardFault (HFSR.FORCED), CFSR keeps PRECISERR with
    BFAR = the address. The landing opens the contactors at once, and the
    next boot reports HardFault (1)."""
    sim = car.sim
    pins = _contactors(car)
    car.to_run()
    sim.run_for(ms=200)
    assert sim.fault_status("ams")["SHCSR"] & FAULT_ENABLES == 0
    sim.bus_fault_at("ams", "HAL_IWDG_Refresh")
    handler, t = _landed(sim, set(LAST_FAULT), timeout_ms=50)
    assert handler == "HardFault_Handler"
    regs = sim.fault_status("ams")
    assert regs["HFSR"] & FORCED
    assert regs["CFSR"] & (PRECISERR | BFARVALID) == PRECISERR | BFARVALID, f"CFSR {regs['CFSR']:#x}"
    assert regs["BFAR"] == BUS_ERROR_ADDRESS
    _assert_opened_at(car, pins, t, handler)
    reset = _reboot(car)
    health = sim.can("can_acu").frames(FW_HEALTH, since_us=reset)
    assert health and health[0].data[7] == LAST_FAULT["HardFault_Handler"]
    assert (car.state(), car.reason()) == (START, 0)


@NO_FAULT_ENABLES
def test_a_bus_error_reports_busfault(car):
    """stm32h7xx_it.c:146-151 / ams_config.hpp:1083-1088: a bad pointer
    (BusFault) is meant to be told apart from a bad instruction on 0x6CA."""
    sim = car.sim
    sim.bus_fault_at("ams", "HAL_IWDG_Refresh")
    handler, _ = _landed(sim, set(LAST_FAULT), timeout_ms=50)
    reset = _reboot(car)
    health = sim.can("can_acu").frames(FW_HEALTH, since_us=reset)
    assert (handler, health[0].data[7]) == ("BusFault_Handler", LAST_FAULT["BusFault_Handler"])


def _no_access_region_over(sim, address):
    """MPU region 0: 64 KB, no access, XN, at `address`; the MPU on with the
    default map behind it for privileged code (PRIVDEFENA). ARMv7-M ARM
    B3.5: MPU_RNR 0xE000ED98, MPU_RBAR 0xE000ED9C, MPU_RASR 0xE000EDA0
    (XN 28, AP 26:24 = 0, SIZE 5:1 = 15 for 2^16, ENABLE 0), MPU_CTRL
    0xE000ED94 (PRIVDEFENA 2, ENABLE 0)."""
    for reg, value in ((0xE000ED98, 0), (0xE000ED9C, address), (0xE000EDA0, 1 << 28 | 15 << 1 | 1),
                       (0xE000ED94, 1 << 2 | 1)):
        sim.monitor(f"sysbus WriteDoubleWord {reg:#x} {value:#x}", board="ams")


@pytest.mark.parametrize("fault", ["BusFault", "MemManage"])
def test_with_the_fault_enables_each_class_lands_in_its_own_handler(car, fault):
    """The vHIL side of #196: with SHCSR's enables set (written here, as a
    firmware that enabled them would; the AMS doesn't), a precise BusFault
    lands in BusFault_Handler, and an MPU no-access violation (the AMS
    programs no MPU region: here region 0 over the reserved range, so the
    same load takes the MPU fault first) in MemManage_Handler, each with
    its own syndrome, nothing escalated. Each landing opens the contactors
    in Run and stamps its own reason for the next boot's 0x6CA."""
    sim = car.sim
    pins = _contactors(car)
    car.to_run()
    sim.run_for(ms=200)
    sim.monitor(f"sysbus WriteDoubleWord 0xE000ED24 {FAULT_ENABLES:#x}", board="ams")
    if fault == "MemManage":
        _no_access_region_over(sim, BUS_ERROR_ADDRESS)
    sim.bus_fault_at("ams", "HAL_IWDG_Refresh")
    handler, t = _landed(sim, set(LAST_FAULT), timeout_ms=50)
    assert handler == f"{fault}_Handler"
    regs = sim.fault_status("ams")
    assert not regs["HFSR"] & FORCED
    if fault == "BusFault":
        assert regs["CFSR"] & (PRECISERR | BFARVALID) == PRECISERR | BFARVALID, f"CFSR {regs['CFSR']:#x}"
        assert regs["BFAR"] == BUS_ERROR_ADDRESS
    else:
        assert regs["CFSR"] & (DACCVIOL | MMARVALID) == DACCVIOL | MMARVALID, f"CFSR {regs['CFSR']:#x}"
        assert regs["MMFAR"] == BUS_ERROR_ADDRESS
    _assert_opened_at(car, pins, t, handler)
    reset = _reboot(car)
    health = sim.can("can_acu").frames(FW_HEALTH, since_us=reset)
    assert health and health[0].data[7] == LAST_FAULT[handler]
