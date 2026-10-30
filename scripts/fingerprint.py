"""The identity fingerprint: everything a lock-step run observes, per system,
compared against the references recorded in ci/fingerprints.json.

    python scripts/fingerprint.py observe OUT.json [--ms N] [system ...]
    python scripts/fingerprint.py check ci/fingerprints.json [--out NEW.json] [--full FULL.json]

What a run observes after N ms from power-on (default: the reference's
run_ms): every bus's timeline in ns, the watched GPIO edges with the
instruction count at each, the radio's payloads, and each CPU's executed
instructions, PC, architectural-state and RAM digest (vhil_ff_<board>
StateDigest) and HAL tick (uwTick); hashed per system. It is the study's
fingerprint (docs/proposals/renode-patches/tooling/fingerprint.py), the
identity check of docker/renode/'s patch queue, run by CI's impact guard
(.github/workflows/sim.yml `guard`, docs/ci.md) on every heavy run: a change
that the impact selection left some tests out for still has to leave every
system's first seconds byte-identical, or re-record the references.

The firmware is pinned (the reference's `firmware` commits, built by CI with
`vhil.system build --commit`), so a firmware branch moving never moves the
fingerprint: only the vHIL (models, platform, generator, Renode) can. Images
from VHIL_ECU_ELF, VHIL_AMS_ELF, VHIL_UDV_ELF, VHIL_CAN_BOOTLOADER_ELF.

`check` exits 1 if any system's digest differs from its reference (or a
reference is missing), and writes the would-be reference file to --out: when
the change is intended, commit that file as ci/fingerprints.json (a ci/**
change, so the PR runs everything).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SYSTEMS = ["ecu", "ams", "ecu-ams", "udv", "ecu-ams-udv", "ecu-ams-ecu"]
# Pins whose edges are watched, per system (the study's choice: the ECU's
# bit-banged nRF24 SPI, the AMS's LTC6820 chip select, the uDV's EBS valves
# and SDC relay).
WATCHED = {"ecu": [("ecu", "PA5"), ("ecu", "PA7"), ("ecu", "PB0"), ("ecu", "PC5")],
           "ams": [("ams", "PB9")],
           "ecu-ams": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9")],
           "udv": [("udv", "PB4"), ("udv", "PB5"), ("udv", "PB7")],
           "ecu-ams-udv": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9"), ("udv", "PB4"),
                           ("udv", "PB7")],
           "ecu-ams-ecu": [("ecu", "PA5"), ("ecu", "PB0"), ("ams", "PB9"), ("ecu2", "PA5")]}
ENV = {"ecu": "VHIL_ECU_ELF", "ams": "VHIL_AMS_ELF", "udv": "VHIL_UDV_ELF",
       "can-bootloader": "VHIL_CAN_BOOTLOADER_ELF"}
DEFAULT_MS = 5000


def system_path(system: str) -> Path:
    p = REPO / "systems" / f"{system}.yaml"
    return p if p.exists() else REPO / "scripts" / "speed-systems" / f"{system}.yaml"


def images(system: str) -> dict[str, Path]:
    from vhil.system import System
    fw = {}

    def elf(ident: str) -> Path:
        path = os.environ.get(ENV[ident])
        if not path or not Path(path).is_file():
            sys.exit(f"fingerprint: no {ident} image (${ENV[ident]})")
        return Path(path)

    for b in System(system_path(system)).boards.values():
        fw[b.name] = elf(b.firmware["id"])
        if b.bootloader is not None:
            fw[f"{b.name}.bootloader"] = elf(b.bootloader["id"])
    return fw


def observe(system: str, ms: int) -> dict:
    from vhil import elf as elfsyms
    from vhil.sim import Sim
    from vhil.system import SystemError, require_gate_quantum
    fw = images(system)
    with Sim(system_path(system), fw) as sim:
        # The references are the gate's: 500 us, never #348's 1 ms opt-in.
        try:
            require_gate_quantum(sim.quantum_us, f"fingerprint run of {system}")
        except SystemError as e:
            sys.exit(f"fingerprint: {e}")
        for board, pin in WATCHED[system]:
            io = sim.io(board)
            io.watch(*io.gpio(pin))
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
                "uwTick": (sim.monitor(f"sysbus ReadDoubleWord {tick:#x}", board=board).strip()
                           if tick else None)}
        if "radio" in sim.system.devices:
            out["radio"] = [repr(p) for p in sim.radio("radio").payloads()]
    out["summary"] = {"frames": {b: len(t) for b, t in out["buses"].items()},
                      "edges": {b: len(e) for b, e in out["edges"].items()},
                      "digest": hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()}
    return out


def observe_all(systems: list[str], ms: int, log=print) -> dict[str, dict]:
    res = {}
    for system in systems:
        res[system] = observe(system, ms)
        log(f"{system}: {json.dumps(res[system]['summary'], sort_keys=True)}", flush=True)
    return res


def reference_of(observed: dict[str, dict], ms: int, firmware: dict, renode: str | None) -> dict:
    return {"run_ms": ms, "firmware": firmware, "renode": renode,
            "systems": {s: o["summary"] for s, o in observed.items()}}


def compare(reference: dict, observed: dict[str, dict]) -> list[str]:
    """What differs, one line per system; empty when every digest matches."""
    out = []
    for system in reference.get("systems", {}).keys() | observed.keys():
        ref = reference.get("systems", {}).get(system)
        got = observed.get(system, {}).get("summary")
        if ref is None:
            out.append(f"{system}: no reference recorded")
        elif got is None:
            out.append(f"{system}: not observed")
        elif ref["digest"] != got["digest"]:
            moved = [f"{k} {ref[k]} -> {got[k]}" for k in ("frames", "edges") if ref.get(k) != got.get(k)]
            out.append(f"{system}: digest {ref['digest'][:12]} -> {got['digest'][:12]}"
                       + (f" ({'; '.join(moved)})" if moved else " (same counts: timing or state)"))
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("observe")
    o.add_argument("out", type=Path)
    o.add_argument("--ms", type=int, default=DEFAULT_MS)
    o.add_argument("systems", nargs="*")
    c = sub.add_parser("check")
    c.add_argument("reference", type=Path)
    c.add_argument("--out", type=Path, help="write the would-be reference file here")
    c.add_argument("--full", type=Path, help="write the full observation here")
    args = ap.parse_args(argv)

    from vhil import renode as rn
    from vhil.bench import DEFAULT_RENODE
    renode = rn.build_id(DEFAULT_RENODE)
    print(f"vhil: {renode}", flush=True)
    if args.cmd == "observe":
        res = observe_all(args.systems or SYSTEMS, args.ms)
        args.out.write_text(json.dumps(res, indent=0, sort_keys=True))
        return 0

    reference = json.loads(args.reference.read_text())
    ms = int(reference.get("run_ms", DEFAULT_MS))
    res = observe_all(list(reference.get("systems") or SYSTEMS), ms)
    if args.full:
        args.full.write_text(json.dumps(res, indent=0, sort_keys=True))
    new = reference_of(res, ms, reference.get("firmware", {}), renode)
    if args.out:
        args.out.write_text(json.dumps(new, indent=1, sort_keys=True) + "\n")
    diff = compare(reference, res)
    for line in diff:
        print(f"::error title=fingerprint::{line}")
    if diff:
        print(f"{len(diff)} system(s) differ from {args.reference}: if intended, commit the "
              f"new reference (the `fingerprint` artifact's fingerprints.json) as {args.reference}")
        return 1
    print(f"every system matches {args.reference}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
