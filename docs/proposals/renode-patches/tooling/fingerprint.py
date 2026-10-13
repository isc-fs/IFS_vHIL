"""Everything a lock-step run observes, for comparing Renode builds: the
observation of tests/sim/test_fast_forward.py (every bus's timeline in ns,
GPIO edges with instruction counts, radio payloads, each CPU's executed
instructions, PC, architectural-state digest and RAM digest via
vhil_ff_<board> StateDigest) after RUN_MS from power-on, per system.
    python fingerprint.py OUT.json [ms]       (RENODE selects the build)"""
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, "/work")
from vhil.sim import Sim
from vhil.system import REPO, System

WATCHED = {"ecu": [("ecu", "PA5"), ("ecu", "PA7"), ("ecu", "PB0"), ("ecu", "PC5")],
           "ams": [("ams", "PB9")],
           "ecu-ams": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9")]}
FW = {"ecu": "/fw/ECU08.elf", "ams": "/fw/AMS.elf", "bootloader": "/fw/CAN_BL.elf"}

def images(system):
    fw = {}
    for b in System(REPO / "systems" / f"{system}.yaml").boards.values():
        fw[b.name] = Path(FW[b.firmware["id"]])
        if b.bootloader is not None:
            fw[f"{b.name}.bootloader"] = Path(FW["bootloader"])
    return fw

def observe(system, ms):
    with Sim(REPO / "systems" / f"{system}.yaml", images(system)) as sim:
        for board, pin in WATCHED[system]:
            io = sim.io(board); io.watch(*io.gpio(pin))
        sim.run_for(ms=ms)
        out = {"now_us": sim.now_us(), "buses": {}, "edges": {}, "radio": [], "cpu": {}}
        for bus in sim.system.buses:
            out["buses"][bus] = [repr(r) for r in sim.can(bus).timeline()]
        for board in sim.system.boards:
            out["edges"][board] = [repr(e) for e in sim.io(board).edges()]
            out["cpu"][board] = {
                "instructions": sim.monitor("cpu ExecutedInstructions", board=board).strip(),
                "pc": sim.monitor("cpu PC", board=board).strip(),
                "state": sim.monitor(f"vhil_ff_{board} StateDigest", board=board).strip()}
        if "radio" in sim.system.devices:
            out["radio"] = [repr(p) for p in sim.radio("radio").payloads()]
        return out

out_path, ms = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3500
res = {}
for system in ("ecu", "ams", "ecu-ams"):
    o = observe(system, ms)
    o["summary"] = {"frames": {b: len(t) for b, t in o["buses"].items()},
                    "edges": {b: len(e) for b, e in o["edges"].items()},
                    "digest": hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest()}
    res[system] = o
    print(system, o["summary"], flush=True)
Path(out_path).write_text(json.dumps(res, indent=0, sort_keys=True))
