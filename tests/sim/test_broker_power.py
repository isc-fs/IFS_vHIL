"""The virtual broker's power path against a real board (#63): a power-on
must not depend on how fast the monitor commands land.

Renode's MCAN, unconnected, keeps each TX request pending, and a firmware
whose TX FIFO fills refuses every later frame (the ECU ignores the refusal,
IFS08-CE-ECU#251), so a bus connected after the firmware has booted stays
silent until the next power cycle. CI hit this when the Connect landed
~80-200 ms (virtual) after the reset; the broker now connects first.
"""
import pytest

from vhil.broker import make_backend
from vhil.sim import Sim
from vhil.system import REPO

K4 = (0x20, 0, 3)                       # MLC4 relay = the ECU (systems/ecu.yaml)


class _Base:
    def tca_write_pin(self, addr, port, pin, value):
        pass

    def _tick(self):
        pass


class _SlowMonitor:
    """The broker's monitor, with virtual time passing after every command,
    as a loaded host does between monitor round-trips."""

    def __init__(self, sim, lag_ms):
        self.sim, self.lag_ms = sim, lag_ms

    def execute(self, command):
        reply = self.sim.monitor(command)
        if self.lag_ms and not command.startswith("mach set"):
            self.sim.run_for(ms=self.lag_ms)
        return reply


@pytest.mark.parametrize("lag_ms", [0, 100])
def test_a_slow_power_on_still_boots_onto_every_bus(firmware, lag_ms):
    with Sim(REPO / "systems" / "ecu.yaml", {"ecu": firmware("ecu")}) as sim:
        config = sim.system.bench_config()
        sim.monitor('mach set "ecu"')
        for controller, hub in config["carriers"][0]["can"].items():   # as vhil/bench.py starts
            sim.monitor(f"connector Disconnect {controller} {hub}")
        backend = make_backend(_Base, _SlowMonitor(sim, lag_ms), config)
        backend.tca_write_pin(*K4, True)
        sim.run_for(ms=300)
        backend.tca_write_pin(*K4, False)
        sim.run_for(ms=300)                       # unpowered: running, unheard
        backend.tca_write_pin(*K4, True)
        t = sim.now_us()
        sim.run_for(ms=1000)
        assert sim.can("can_acu").count(0x100, since_us=t) >= 90, "ACU bus silent after power-on"
        assert sim.can("can_inv").count(0x360, since_us=t) >= 90, "inverter bus silent after power-on"
