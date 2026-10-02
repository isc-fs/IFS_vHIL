"""Systems as data: load, check, render and build them.

A system file (systems/*.yaml) places boards from the catalogue (catalog/),
gives each its firmware, and wires them together. This module turns one into
what the backend needs: a Renode script, the virtual broker's wiring, and the
firmware images. Nothing outside the platform's `renode:` section is
Renode-specific, and nothing here knows about any particular MCU.

    python -m vhil.system validate systems/ecu.yaml
    python -m vhil.system render   systems/ecu.yaml [--firmware ecu=ECU08.elf] [--socketcan] [-o out.resc]
    python -m vhil.system bench    systems/ecu.yaml          # virtual broker wiring, as JSON
    python -m vhil.system build    systems/ecu.yaml [--workdir build/fw] [--ref ecu=<ref>]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "schemas" / "vhil.schema.json"
CATALOG = REPO / "catalog"


class SystemError(ValueError):
    pass


def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(json.loads(SCHEMA.read_text()))


def _load(path: Path, kind: str) -> dict:
    doc = yaml.safe_load(Path(path).read_text())
    errors = sorted(_validator().iter_errors(doc), key=str)
    if errors:
        raise SystemError(f"{path}: " + "; ".join(e.message for e in errors[:3]))
    if doc["kind"] != kind:
        raise SystemError(f"{path}: expected a {kind}, found a {doc['kind']}")
    return doc


def _entry(kind: str, ident: str, catalog: Path) -> dict:
    folder = {"platform": "platforms", "board": "boards", "firmware": "firmware",
              "model": "models"}[kind]
    path = catalog / folder / f"{ident}.yaml"
    if not path.is_file():
        raise SystemError(f"no {kind} '{ident}' in the catalogue ({path})")
    doc = _load(path, kind)
    if doc["id"] != ident:
        raise SystemError(f"{path}: id '{doc['id']}' does not match its file name")
    return doc


@dataclass
class Board:
    name: str           # instance name in the system, e.g. "ecu"
    board: dict         # catalogue board
    platform: dict      # catalogue platform
    firmware: dict      # catalogue firmware source

    def endpoint(self, connector: str) -> tuple[str, object]:
        """(kind, target) for a connector or pin: ('can'|'spi'|'sdmmc', peripheral),
        ('analog', {adc, channel}) or ('gpio', {port, pin})."""
        for kind, section in (("can", "can"), ("spi", "spi"), ("sdmmc", "sdmmc"),
                              ("analog", "analog_in"), ("gpio", "gpio")):
            if connector in self.board.get(section, {}):
                return kind, self.board[section][connector]
        raise SystemError(f"board '{self.name}' ({self.board['id']}) has no "
                          f"connector or pin '{connector}'")


class System:
    def __init__(self, path: Path, catalog: Path = CATALOG):
        self.path = Path(path)
        self.doc = _load(self.path, "system")
        self.id = self.doc["id"]
        self.boards: dict[str, Board] = {}
        for name, spec in self.doc["boards"].items():
            board = _entry("board", spec["board"], catalog)
            self.boards[name] = Board(name, board, _entry("platform", board["platform"], catalog),
                                      _entry("firmware", spec["firmware"], catalog))
        self.buses = self.doc.get("buses", {})
        self.bench = self.doc.get("bench", {})
        self.devices = {name: dict(spec, model_doc=_entry("model", spec["model"], catalog))
                        for name, spec in self.doc.get("devices", {}).items()}
        self._check()

    def resolve(self, endpoint: str) -> tuple[Board, str, object]:
        name, connector = endpoint.split(".", 1)
        if name not in self.boards:
            raise SystemError(f"'{endpoint}': no board instance '{name}' in {self.path}")
        kind, target = self.boards[name].endpoint(connector)
        return self.boards[name], kind, target

    def _check(self) -> None:
        seen: dict[str, str] = {}
        for bus, spec in self.buses.items():
            for node in spec["nodes"]:
                _, kind, _ = self.resolve(node)
                if kind != spec["kind"]:
                    raise SystemError(f"bus '{bus}' is {spec['kind']} but '{node}' is {kind}")
                if node in seen:
                    raise SystemError(f"'{node}' is on both '{seen[node]}' and '{bus}'")
                seen[node] = bus
        for name, dev in self.devices.items():
            interface = dev["model_doc"].get("interface", {})
            boards = set()
            for port, want in (("spi", "spi"), ("cs", "gpio"), ("sdmmc", "sdmmc")):
                if interface.get(port) and port not in dev:
                    raise SystemError(f"device '{name}' ({dev['model']}) needs '{port}'")
                if port in dev:
                    if not interface.get(port):
                        raise SystemError(f"device '{name}' ({dev['model']}) takes no '{port}'")
                    board, kind, _ = self.resolve(dev[port])
                    if kind != want:
                        raise SystemError(f"device '{name}': {port} '{dev[port]}' is {kind}, not {want}")
                    boards.add(board.name)
            if interface.get("attach"):
                parent = self.devices.get(dev.get("attach"))
                if parent is None:
                    raise SystemError(f"device '{name}' ({dev['model']}) must attach to a device "
                                      f"providing {interface['attach']}")
                if parent["model_doc"].get("interface", {}).get("provides") != interface["attach"]:
                    raise SystemError(f"device '{name}': '{dev['attach']}' provides no "
                                      f"{interface['attach']} port")
            elif "attach" in dev or "count" in dev:
                raise SystemError(f"device '{name}' ({dev['model']}) does not attach to a device")
            if len(boards) > 1:
                raise SystemError(f"device '{name}' spans boards {sorted(boards)}")
            unknown = set(dev.get("params", {})) - set(dev["model_doc"].get("params", {}))
            if unknown:
                raise SystemError(f"device '{name}': unknown params {sorted(unknown)}")
        for name in self.devices:
            self.board_of_device(name)   # every device reaches a board, no cycles
        for c in self.bench.get("carriers", []):
            if c["board"] not in self.boards:
                raise SystemError(f"bench carrier '{c['board']}' is not a board in this system")
        for r in self.bench.get("dac_routes", []):
            _, kind, _ = self.resolve(r["to"])
            if kind != "analog":
                raise SystemError(f"DAC route to '{r['to']}': not an analog input")

    def can_of(self, board: str) -> dict[str, str]:
        """CAN controller -> bus name, for one board instance."""
        out = {}
        for bus, spec in self.buses.items():
            for node in spec["nodes"]:
                b, _, controller = self.resolve(node)
                if b.name == board:
                    out[controller] = bus
        return out

    def board_of_device(self, name: str, _seen: tuple = ()) -> str:
        if name in _seen:
            raise SystemError(f"devices attach in a cycle: {' -> '.join(_seen + (name,))}")
        dev = self.devices[name]
        for port in ("spi", "cs", "sdmmc"):
            if port in dev:
                return self.resolve(dev[port])[0].name
        return self.board_of_device(dev["attach"], _seen + (name,))

    def devices_on(self, board: str) -> list[str]:
        """Device names on one board, parents before the devices attached to them."""
        mine = [n for n in self.devices if self.board_of_device(n) == board]
        ordered: list[str] = []
        def visit(n):
            if n not in ordered:
                if "attach" in self.devices[n]:
                    visit(self.devices[n]["attach"])
                ordered.append(n)
        for n in mine:
            visit(n)
        return ordered

    def _renode_device(self, name: str, dev: dict) -> str:
        """Platform-description text attaching one device (or a counted chain)."""
        rn = dev["model_doc"]["renode"]
        local = lambda path: path.split(".", 1)[1] if path.startswith("sysbus.") else path
        params = dict(dev["model_doc"].get("params", {}), **dev.get("params", {}))
        body = [f"    {rn.get('params', {}).get(k, k)}: "
                f"{str(v).lower() if isinstance(v, bool) else v}" for k, v in params.items()]
        lines = []
        if "attach" in dev:
            for i in range(dev.get("count", 1)):
                lines += [f"{name}{i}: {rn['type']} @ {dev['attach']} {i}"] + body
        else:
            port = dev.get("spi") or dev.get("sdmmc")
            parent = local(self.resolve(port)[2]) if port else "sysbus"
            lines += [f"{name}: {rn['type']} @ {parent}"] + body
        if "cs" in dev:
            gpio = self.resolve(dev["cs"])[2]
            lines += ["", f"{local(gpio['port'])}:", f"    {gpio['pin']} -> {name}@0"]
        return "\n".join(lines)

    # -- outputs ------------------------------------------------------------

    def render_renode(self, firmware: dict[str, Path] | None = None,
                      socketcan: bool = False) -> str:
        """The Renode script for this system. Without a path for a board, the
        script expects `$elf_<board>` to be set before it is included."""
        firmware = firmware or {}
        out = [f":name: {self.id} (generated by vhil.system from {self.path.name})",
               ":description: Generated. Edit the system file, not this script.", "",
               "using sysbus", ""]
        quantum = self.doc.get("time", {}).get("quantum_s")
        if quantum:
            out += [f'emulation SetGlobalQuantum "{quantum:g}"', ""]
        for bus in self.buses:
            out.append(f'emulation CreateCANHub "{bus}"')
        out.append("")
        sources = sorted({(REPO / d["model_doc"]["renode"]["source"]).as_posix()
                          for d in self.devices.values() if "source" in d["model_doc"]["renode"]})
        if sources:
            out += [f"include @{src}" for src in sources] + [""]
        for b in self.boards.values():
            if b.platform["backend"] != "renode":
                raise SystemError(f"board '{b.name}': backend {b.platform['backend']} is not supported")
            rn = b.platform["renode"]
            cpu = rn.get("cpu", "cpu")
            var = f"elf_{b.name}"
            out.append(f"# --- {b.name}: {b.board['id']} running {b.firmware['id']} "
                       f"({b.firmware['repo']})")
            out.append(f'mach create "{b.name}"')
            if b.name in firmware:
                # After `mach create`: a variable set while a machine is
                # selected is local to it, so the next board would not see it.
                out.append(f"${var}=@{Path(firmware[b.name]).resolve().as_posix()}")
            out.append(f"machine LoadPlatformDescription @{(REPO / rn['repl']).as_posix()}")
            out += rn.get("setup", [])
            for name in self.devices_on(b.name):
                dev = self.devices[name]
                out += [f"# device {name}: {dev['model']}"
                        + (f" x{dev['count']} on {dev['attach']}" if "count" in dev else ""),
                        'machine LoadPlatformDescriptionFromString """',
                        self._renode_device(name, dev), '"""']
            for controller, bus in self.can_of(b.name).items():
                out.append(f"connector Connect {controller} {bus}")
            out += ["macro reset", '"""', f"    sysbus LoadELF ${var}"]
            vtor = b.firmware.get("load", {}).get("vector_table")
            if vtor is not None:
                out.append(f"    {cpu} VectorTableOffset 0x{vtor:08X}")
            out += ['"""', "runMacro $reset", ""]
        if socketcan:
            bridged = [(bus, spec) for bus, spec in self.buses.items() if spec.get("host_netdev")]
            for bus, spec in bridged:
                owner = self.resolve(spec["nodes"][0])[0].name
                out += [f'mach set "{owner}"',
                        f'machine CreateSocketCANBridge "br_{bus}" "{spec["host_netdev"]}"',
                        f"connector Connect br_{bus} {bus}"]
        return "\n".join(out).rstrip() + "\n"

    def bench_config(self) -> dict:
        """The virtual broker's wiring (see vhil/broker.py)."""
        carriers = [{"machine": c["board"], "slot": c.get("slot"), "relay": c["relay"],
                     "ina_addr": c["ina_addr"], "current_A": c["current_A"],
                     "can": self.can_of(c["board"])}
                    for c in self.bench.get("carriers", [])]
        routes = []
        for r in self.bench.get("dac_routes", []):
            board, _, target = self.resolve(r["to"])
            routes.append({"dac": r["dac"], "channel": r["channel"], "machine": board.name,
                           "adc": target["adc"], "adc_channel": target["channel"]})
        return {"carriers": carriers, "dac_routes": routes}

    def build_firmware(self, workdir: Path, refs: dict[str, str] | None = None,
                       log=print) -> dict[str, Path]:
        """Clone each firmware source at its ref and build it with its recipe.
        Returns board instance -> ELF; a flat .bin is written next to it."""
        refs, built, workdir = refs or {}, {}, Path(workdir)
        by_source: dict[tuple, Path] = {}
        for b in self.boards.values():
            fw = b.firmware
            ref = refs.get(b.name, fw["ref"])
            key = (fw["id"], fw["repo"], ref)
            if key not in by_source:
                src = workdir / f"{fw['id']}@{ref.replace('/', '_')}"
                if src.exists():
                    shutil.rmtree(src)
                log(f"[{b.name}] cloning {fw['repo']}@{ref}")
                cmd = ["git", "clone", "-q", "--depth", "1", "-b", ref,
                       f"https://github.com/{fw['repo']}", str(src)]
                if fw.get("submodules"):
                    cmd[2:2] = ["--recurse-submodules"]
                subprocess.run(cmd, check=True)
                # Build output goes to stderr: stdout carries only the
                # board=elf results, so callers can parse it.
                for step in ("configure", "build"):
                    log(f"[{b.name}] {fw['build'][step]}")
                    subprocess.run(fw["build"][step], shell=True, cwd=src, check=True,
                                   stdout=sys.stderr)
                elf = src / fw["build"]["elf"]
                subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(elf),
                                str(elf.with_suffix(".bin"))], check=True)
                by_source[key] = elf
            built[b.name] = by_source[key]
        return built


def _pairs(items) -> dict[str, str]:
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemError(f"expected <board>=<value>, got '{item}'")
        out[key] = value
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "render", "bench", "build"):
        s = sub.add_parser(name)
        s.add_argument("system", type=Path)
        if name == "render":
            s.add_argument("--firmware", action="append", metavar="BOARD=ELF")
            s.add_argument("--socketcan", action="store_true")
            s.add_argument("-o", "--output", type=Path)
        if name == "build":
            s.add_argument("--workdir", type=Path, default=REPO / "build" / "fw")
            s.add_argument("--ref", action="append", metavar="BOARD=REF")
    args = p.parse_args(argv)
    try:
        system = System(args.system)
        if args.cmd == "validate":
            print(f"{args.system}: OK ({', '.join(system.boards)})")
        elif args.cmd == "render":
            text = system.render_renode({k: Path(v) for k, v in _pairs(args.firmware).items()},
                                        socketcan=args.socketcan)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(text)
            else:
                sys.stdout.write(text)
        elif args.cmd == "bench":
            print(json.dumps(system.bench_config(), indent=2))
        elif args.cmd == "build":
            for board, elf in system.build_firmware(args.workdir, _pairs(args.ref),
                                                    log=lambda m: print(m, file=sys.stderr)).items():
                print(f"{board}={elf}")
    except SystemError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
