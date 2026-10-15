"""Everything a lock-step run observes, for comparing Renode builds or the
vHIL's models (a pure speed change must leave it byte-identical): the
observation of tests/sim/test_fast_forward.py (every bus's timeline in ns,
GPIO edges with instruction counts, radio payloads, each CPU's executed
instructions, PC, architectural-state digest and RAM digest via
vhil_ff_<board> StateDigest, and the HAL tick uwTick) after RUN_MS from
power-on, per system.
    python fingerprint.py OUT.json [ms] [system ...]    (RENODE selects the build)
Firmware: VHIL_ECU_ELF, VHIL_AMS_ELF, VHIL_UDV_ELF and VHIL_CAN_BOOTLOADER_ELF,
else /fw/ECU08.elf, /fw/AMS.elf, /fw/uDV.elf and /fw/CAN_BL.elf. Systems:
ecu, ams, ecu-ams, udv, ecu-ams-udv and the three-board benchmark ecu-ams-ecu
(scripts/speed-systems/) by default."""
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ.get("VHIL_REPO", "/work"))
from vhil import elf as elfsyms
from vhil.sim import Sim
from vhil.system import REPO, System

WATCHED = {"ecu": [("ecu", "PA5"), ("ecu", "PA7"), ("ecu", "PB0"), ("ecu", "PC5")],
           "ams": [("ams", "PB9")],
           "ecu-ams": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9")],
           "udv": [("udv", "PB4"), ("udv", "PB5"), ("udv", "PB7")],
           "ecu-ams-udv": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9"), ("udv", "PB4"), ("udv", "PB7")],
           "ecu-ams-ecu": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9"), ("ecu2", "PA5")]}
FW = {"ecu": os.environ.get("VHIL_ECU_ELF", "/fw/ECU08.elf"),
      "ams": os.environ.get("VHIL_AMS_ELF", "/fw/AMS.elf"),
      "udv": os.environ.get("VHIL_UDV_ELF", "/fw/uDV.elf"),
      "bootloader": os.environ.get("VHIL_CAN_BOOTLOADER_ELF", "/fw/CAN_BL.elf")}
SYSTEMS = ["ecu", "ams", "ecu-ams", "udv", "ecu-ams-udv", "ecu-ams-ecu"]


def path(system):
    p = REPO / "systems" / f"{system}.yaml"
    return p if p.exists() else REPO / "scripts" / "speed-systems" / f"{system}.yaml"


def images(system):
    fw = {}
    for b in System(path(system)).boards.values():
        fw[b.name] = Path(FW[b.firmware["id"]])
        if b.bootloader is not None:
            fw[f"{b.name}.bootloader"] = Path(FW["bootloader"])
    return fw


def observe(system, ms):
    fw = images(system)
    with Sim(path(system), fw) as sim:
        for board, pin in WATCHED[system]:
            io = sim.io(board); io.watch(*io.gpio(pin))
        sim.run_for(ms=ms)
        out = {"now_us": sim.now_us(), "buses": {}, "edges": {}, "radio": [], "cpu": {}}
        for bus in sim.system.buses:
            out["buses"][bus] = [repr(r) for r in sim.can(bus).timeline()]
        for board in sim.system.boards:
            out["edges"][board] = [repr(e) for e in sim.io(board).edges()]
            tick = elfsyms.symbols(fw[board]).get("uwTick", (None,))[0]
            out["cpu"][board] = {
                "instructions": sim.monitor("cpu ExecutedInstructions", board=board).strip(),
                "pc": sim.monitor("cpu PC", board=board).strip(),
                "state": sim.monitor(f"vhil_ff_{board} StateDigest", board=board).strip(),
                "uwTick": sim.monitor(f"sysbus ReadDoubleWord {tick:#x}", board=board).strip() if tick else None}
        if "radio" in sim.system.devices:
            out["radio"] = [repr(p) for p in sim.radio("radio").payloads()]
        return out


out_path = sys.argv[1]
ms = int(sys.argv[2]) if len(sys.argv) > 2 else 3500
res = {}
for system in sys.argv[3:] or SYSTEMS:
    o = observe(system, ms)
    o["summary"] = {"frames": {b: len(t) for b, t in o["buses"].items()},
                    "edges": {b: len(e) for b, e in o["edges"].items()},
                    "digest": hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest()}
    res[system] = o
    print(system, o["summary"], flush=True)
Path(out_path).write_text(json.dumps(res, indent=0, sort_keys=True))
