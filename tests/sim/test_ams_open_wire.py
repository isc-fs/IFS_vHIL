"""AMS open-wire detection (ADOW) against open cell-sense conductors (#24).

Firmware facts (IFS08-CE-AMS):
  open_wire.hpp: per IC, ADOW pull-up then pull-down; an interior conductor
    C(n) is open when CELL_PU(n+1) - CELL_PD(n+1) < -threshold, C0 when
    CELL_PU(1) == 0, the top conductor when CELL_PD(N) == 0.
  Upper IC (chain index even) carries 9 cells, lower (odd) 10: module m is
    ICs 2m and 2m+1 (open_wire.hpp, POLL-INTEGRATION CONTRACT).
  CellOpenWire = fault reason 16, detail = offending-module mask, any state
    (safety_predicates.hpp:68-74, 200-203); CellOpenWireCheck = true.
  0x6C0 byte 0 = FSM state (5 = Error), 6 = reason, 7 = detail, once 0x7F0
    DE AD BE EF arms the pit stream.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

PIT_ARM, PIT_FSM = 0x7F0, 0x6C0
ERROR, CELL_OPEN_WIRE = 5, 16


@pytest.mark.parametrize("chip, conductor, module", [
    (3, 4, 1),     # interior conductor, lower IC of module 1
    (0, 0, 0),     # bottom conductor C0, upper IC of module 0
    (4, 9, 2),     # top conductor of a 9-cell upper IC (module 2)
    (9, 10, 4),    # top conductor of a 10-cell lower IC (module 4)
], ids=["interior", "bottom", "top-upper", "top-lower"])
def test_an_open_wire_faults_the_right_module(firmware, chip, conductor, module):
    with Sim(REPO / "systems" / "ams.yaml", {"ams": firmware("ams")}) as sim:
        can = sim.can("can_acu")
        sim.run_for(ms=1500)
        can.send(PIT_ARM, bytes.fromhex("DEADBEEF"))
        t = sim.run_for(ms=2500)                  # past boot grace, healthy
        before = can.frames(PIT_FSM, since_us=t - 1_000_000)
        assert before and {f.data[0] for f in before} == {0}, "AMS not healthy before the fault"

        sim.monitor(f"sysbus.spi1.isospi.cells{chip} OpenWire {conductor} true", board="ams")
        t = sim.run_for(ms=2000)
        status = can.last(PIT_FSM)
        assert status.data[0] == ERROR, f"AMS state {status.data[0]}, not Error"
        assert status.data[6] == CELL_OPEN_WIRE, f"fault reason {status.data[6]}"
        assert status.data[7] == 1 << module, f"module mask 0x{status.data[7]:02X}"


def test_a_healthy_chain_never_reads_open(firmware):
    """ADOW runs on every voltage poll; with every wire connected it must
    not fault."""
    with Sim(REPO / "systems" / "ams.yaml", {"ams": firmware("ams")}) as sim:
        can = sim.can("can_acu")
        sim.run_for(ms=1500)
        can.send(PIT_ARM, bytes.fromhex("DEADBEEF"))
        t = sim.run_for(ms=8000)
        status = can.frames(PIT_FSM, since_us=t - 6_000_000)
        assert status and {(f.data[0], f.data[6]) for f in status} == {(0, 0)}
