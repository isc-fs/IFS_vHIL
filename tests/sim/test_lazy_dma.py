"""ADC3's lazy hand-over to the DMA changes nothing a firmware or a test
observes (#260).

models/renode/Stm32H7Adc3.cs delivers its DMA results when something could
see them (an ADC3, DMA or DMAMUX register access, a read or write of the RAM
the stream writes) and at the result that sets one of the stream's flags,
instead of a clock event per result (the AMS's current sensor: 12.5 kHz).
These run each system with the AMS from power-on, through the bootloader's
2 s window into the apps' steady state with the capture running, once with
it and once without (VHIL_LAZY_DMA=0: an event per result, as before), and
compare everything the run produced:

  - every bus's timeline: each frame's offer, start, end and outcome, in ns;
  - GPIO edges with the CPU's executed-instruction count at each write (the
    AMS's LTC6820 chip select, the ECU's nRF24 pins). Not their t_us: the
    probe stamps an edge with the time the CPU last reported (VhilProbe.cs
    VhilGpioProbe), which moves with where Renode ends a chunk of execution,
    and a clock event per result ends one every 80 us;
  - ADC3's results and DMA1's transfers;
  - at the end, each CPU's executed instructions and a SHA-256 of its
    architectural state and every RAM byte, after ADC3 has handed over
    everything due (Flush).

Equal down to the instruction, or the hand-over is wrong. The lazy run must
also have handed results over lazily, so the comparison is not vacuous.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

RUN_MS = 3500        # 2 s bootloader window, then the apps' boot and steady state
ADC = "sysbus.adc3_h73x"
DMA = "sysbus.dma1_h7"

WATCHED = {
    "ams": [("ams", "PB9")],                                   # LTC6820 CS
    "ecu-ams": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9")],
}


def observe(images, system: str, lazy: bool, monkeypatch) -> dict:
    monkeypatch.setenv("VHIL_LAZY_DMA", "1" if lazy else "0")
    with Sim(REPO / "systems" / f"{system}.yaml", images(system)) as sim:
        for board, pin in WATCHED[system]:
            io = sim.io(board)
            io.watch(*io.gpio(pin))
        sim.run_for(ms=RUN_MS)
        out = {"now_us": sim.now_us(), "buses": {}, "edges": {}, "cpu": {}, "adc": {}}
        for bus in sim.system.buses:
            out["buses"][bus] = sim.can(bus).timeline()
        for board in sim.system.boards:
            out["edges"][board] = [(e.pin, e.level, e.instructions) for e in sim.io(board).edges()]
            sim.monitor(f"{ADC} Flush", board=board)
            out["adc"][board] = {
                "conversions": sim.monitor(f"{ADC} Conversions", board=board).strip(),
                "transfers": [sim.monitor(f"{DMA} Transfers {n}", board=board).strip() for n in range(8)],
                "stats": sim.monitor(f"{ADC} LazyStats", board=board).strip(),
            }
            out["cpu"][board] = {
                "instructions": sim.monitor("cpu ExecutedInstructions", board=board).strip(),
                "pc": sim.monitor("cpu PC", board=board).strip(),
                "state": sim.monitor(f"vhil_ff_{board} StateDigest", board=board).strip(),
            }
        return out


def first_difference(a: list, b: list) -> str:
    at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return f"item {at} of {len(a)}/{len(b)}: {a[at:at + 1]} vs {b[at:at + 1]}"


@pytest.mark.parametrize("system", ["ams", "ecu-ams"])
def test_lazy_dma_changes_nothing_observable(images, system, monkeypatch):
    eager = observe(images, system, False, monkeypatch)
    lazy = observe(images, system, True, monkeypatch)
    print(f"{system}: eager {eager['adc']['ams']['stats']}; lazy {lazy['adc']['ams']['stats']}")
    assert lazy["now_us"] == eager["now_us"]
    for bus, timeline in eager["buses"].items():
        assert lazy["buses"][bus] == timeline, f"{bus}: {first_difference(timeline, lazy['buses'][bus])}"
    for board, edges in eager["edges"].items():
        assert lazy["edges"][board] == edges, f"{board} GPIO: {first_difference(edges, lazy['edges'][board])}"
    for board in eager["adc"]:
        assert {k: v for k, v in lazy["adc"][board].items() if k != "stats"} == \
            {k: v for k, v in eager["adc"][board].items() if k != "stats"}, board
    assert lazy["cpu"] == eager["cpu"]
    # Not vacuous: the AMS's capture ran, eagerly in one run, lazily in the
    # other.
    assert int(eager["adc"]["ams"]["conversions"], 0) > 10000, eager["adc"]["ams"]
    assert eager["adc"]["ams"]["stats"].startswith("lazy 0 horizon 0 "), eager["adc"]["ams"]["stats"]
    lazy_count = int(lazy["adc"]["ams"]["stats"].split()[1])
    assert lazy_count > 1000, lazy["adc"]["ams"]["stats"]
