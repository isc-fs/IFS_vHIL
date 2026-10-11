"""Busy-wait fast-forward changes nothing a firmware or a test observes (#154).

models/renode/VhilFastForward.cs charges a busy-wait's instructions instead of
executing them one by one: the ECU's volatile counter delay (nrf24.c
NRF24_BitBangDelay) and the CAN bootloader's polling main loop (main.c
Bootloader_MainLoop) at a fixed point. These run each system from power-on,
through the bootloader's 2 s window and the apps' boot into steady state,
once with it and once without (VHIL_FAST_FORWARD=0: Renode as it is), and
compare everything the run produced:

  - every bus's timeline: each frame's offer, start, end and outcome, in ns;
  - GPIO edges with the CPU's executed-instruction count at each write (the
    ECU's bit-banged nRF24 pins; the AMS's LTC6820 chip select, which it
    times with DWT cycle-counter delays);
  - the radio's payloads and their times;
  - at the end, each CPU's executed instructions and a SHA-256 of its
    architectural state (tlib's CPUState before CPU_COMMON: every register,
    the flags, IT state, the v7-M system registers, the FPU, the exclusive
    monitor) and every RAM byte.

Equal down to the instruction, or the fast-forward is wrong. The run with it
must also have skipped what it is for, so the comparison is not vacuous.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

RUN_MS = 3500        # 2 s bootloader window, then the apps' boot and steady state

# Board pins watched per system: (board, pin) as the catalogue names them.
WATCHED = {
    "ecu": [("ecu", "PA5"), ("ecu", "PA7"), ("ecu", "PB0"), ("ecu", "PC5")],    # nRF24 SCK, MOSI, CSN, CE
    "ams": [("ams", "PB9")],                                                    # LTC6820 CS
    "ecu-ams": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9")],
}


def observe(images, system: str, fast_forward: bool, monkeypatch) -> dict:
    monkeypatch.setenv("VHIL_FAST_FORWARD", "1" if fast_forward else "0")
    with Sim(REPO / "systems" / f"{system}.yaml", images(system)) as sim:
        for board, pin in WATCHED[system]:
            io = sim.io(board)
            io.watch(*io.gpio(pin))
        sim.run_for(ms=RUN_MS)
        out = {"now_us": sim.now_us(), "buses": {}, "edges": {}, "radio": [], "cpu": {}, "skipped": {}}
        for bus in sim.system.buses:
            out["buses"][bus] = sim.can(bus).timeline()
        for board in sim.system.boards:
            out["edges"][board] = sim.io(board).edges()
            ff = f"vhil_ff_{board}"
            out["cpu"][board] = {
                "instructions": sim.monitor("cpu ExecutedInstructions", board=board).strip(),
                "pc": sim.monitor("cpu PC", board=board).strip(),
                "state": sim.monitor(f"{ff} StateDigest", board=board).strip(),
            }
            out["skipped"][board] = sim.monitor(f"{ff} Stats", board=board).strip()
        if "radio" in sim.system.devices:
            out["radio"] = sim.radio("radio").payloads()
        return out


def first_difference(a: list, b: list) -> str:
    at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return f"item {at} of {len(a)}/{len(b)}: {a[at:at + 1]} vs {b[at:at + 1]}"


@pytest.mark.parametrize("system", ["ecu", "ams", "ecu-ams"])
def test_fast_forward_changes_nothing_observable(images, system, monkeypatch):
    plain = observe(images, system, False, monkeypatch)
    fast = observe(images, system, True, monkeypatch)
    print(f"{system}: {fast['skipped']}")
    assert fast["now_us"] == plain["now_us"]
    for bus, timeline in plain["buses"].items():
        assert timeline, f"nothing on {bus}"
        assert fast["buses"][bus] == timeline, f"{bus}: {first_difference(timeline, fast['buses'][bus])}"
    for board, edges in plain["edges"].items():
        assert fast["edges"][board] == edges, f"{board} GPIO: {first_difference(edges, fast['edges'][board])}"
    assert fast["radio"] == plain["radio"], f"radio: {first_difference(plain['radio'], fast['radio'])}"
    assert fast["cpu"] == plain["cpu"]
    # Not vacuous: each bootloader's window was fast-forwarded, and the ECU's
    # nRF24 delays.
    for board, stats in fast["skipped"].items():
        assert "polling loops: 0 skips" not in stats, f"{board}: {stats}"
        assert "VHIL_FAST_FORWARD" not in stats, f"{board}: {stats}"
    if "ecu" in fast["skipped"]:
        assert "counter loops: 0 skips" not in fast["skipped"]["ecu"], fast["skipped"]["ecu"]
        assert any(e for e in plain["edges"]["ecu"]), "the ECU's nRF24 pins never moved"
    for board, stats in plain["skipped"].items():
        assert "VHIL_FAST_FORWARD=0" in stats, f"{board}: the plain run had it on: {stats}"
