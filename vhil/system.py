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
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

import yaml

from vhil import flash_image
from vhil import pin_model as pm
from vhil import renode as rn
from vhil.flash_image import FLASH_BASE

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "schemas" / "vhil.schema.json"
CATALOG = REPO / "catalog"

# What a system file may name, as the schema says (schemas/vhil.schema.json):
# checked again here so that a schema that loosens can't let a value reach a
# generated script, a catalogue path or a git command line.
ID = re.compile(r"[a-z0-9][a-z0-9-]*")                       # $defs/id
ROLES = ("ecu", "ams", "udv")                                # $defs/role
NAME = rn.IDENT                                              # $defs/name
ENDPOINT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_]+")   # $defs/endpoint
NETDEV = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,14}")           # a Linux interface name
REF = re.compile(r"(?![-/])(?!.*\.\.)(?!.*//)[A-Za-z0-9._/+-]{1,100}(?<![./])")  # git ref, as the schema
# A pin's display label in a role (catalogue $defs/label): a car signal's name.
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+-]{0,31}")
BACKPLANE_DOC = re.compile(r"docs/backplanes/[a-z0-9-]+\.md")  # a role's backplane page
# A string device param without a declared format (catalogue param_formats).
SAFE_PARAM = re.compile(r"[A-Za-z0-9_.+-]{0,128}")
CARD_DIR_ENV = "VHIL_CARD_DIR"
# A board's pin model, in catalog/pin-models/ (catalogue $defs/board pin_model).
PIN_MODEL_FILE = re.compile(r"[a-z0-9][a-z0-9-]*\.pins\.yaml")
# The board sections whose connectors and pins the emulator wires.
WIRED = ("can", "spi", "uart", "sdmmc", "i2c", "gpio", "analog_in")


def card_dirs() -> list[Path]:
    """Directories an sd-card `image` param may point into: $VHIL_CARD_DIR
    (os.pathsep-separated), else <repo>/build/cards. A relative image is
    looked up in the first."""
    env = os.environ.get(CARD_DIR_ENV)
    if env:
        return [Path(d) for d in env.split(os.pathsep) if d]
    return [REPO / "build" / "cards"]


class SystemError(ValueError):
    pass


def board_pin_model(board: dict, catalog: Path = CATALOG) -> pm.PinModel | None:
    """The board's pin model (catalog/pin-models/<pin_model>), or None if it
    names none."""
    name = board.get("pin_model")
    if name is None:
        return None
    if not isinstance(name, str) or not PIN_MODEL_FILE.fullmatch(name):
        raise SystemError(f"board {board['id']}: pin model {name!r} is not a "
                          f"catalog/pin-models/ file")
    try:
        return pm.load(Path(catalog) / "pin-models" / name)
    except (pm.PinModelError, OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise SystemError(f"board {board['id']}: pin model {name}: {e}") from None


# Where `vhil.system build --workdir <dir>` puts a firmware source at a ref,
# and the list of what it built (scripts/vhil-docker.sh fw and the worker
# append its board=elf lines to <dir>/built.txt).

def source_dir(workdir: Path, fw: dict, ref: str) -> Path:
    return Path(workdir) / f"{fw['id']}@{ref.replace('/', '_')}"


def image_path(workdir: Path, fw: dict, ref: str) -> Path:
    """The ELF a build of catalogue firmware `fw` at `ref` gives."""
    return source_dir(workdir, fw, ref) / fw["build"]["elf"]


def built_images(workdir: Path) -> dict[Path, str]:
    """ELF (resolved) -> image key, for each line of <workdir>/built.txt."""
    listing = Path(workdir) / "built.txt"
    out = {}
    if listing.is_file():
        for line in listing.read_text().splitlines():
            key, sep, path = line.partition("=")
            if sep:
                out[Path(path.strip()).resolve()] = key
    return out


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
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise SystemError(f"{kind} id {ident!r} is not a catalogue id")
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
    bootloader: dict | None = None   # catalogue firmware in sector 0: the board's
    node_id: int | None = None       # the bootloader's node ID, for the board's role
    write_protect: list[int] = field(default_factory=list)   # WRP'd flash sectors at power-on
    firmware_ref: str | None = None     # this system's ref for the firmware, over the catalogue's
    bootloader_ref: str | None = None   # likewise for the bootloader
    role: str | None = None             # the board's role (catalogue board roles)
    flash_bus: str | None = None        # CAN connector the app is flashed over: the role's
    role_spec: dict | None = None       # the role's catalogue entry (firmware, pins, gpio)
    pin_model: pm.PinModel | None = None   # the board's pin model, if it names one

    def ref(self, image: str = "firmware") -> str:
        """The branch or tag to build: the system's, else the catalogue's."""
        fw = self.firmware if image == "firmware" else self.bootloader
        return (self.firmware_ref if image == "firmware" else self.bootloader_ref) or fw["ref"]

    def endpoint(self, connector: str) -> tuple[str, object]:
        """(kind, target) for a connector or pin: ('can'|'spi'|'uart'|'sdmmc'|'i2c',
        peripheral), ('analog', {adc, channel}) or ('gpio', {port, pin}). A
        pin the role re-kinds (its `gpio`) is a GPIO in that role."""
        override = (self.role_spec or {}).get("gpio") or {}
        if connector in override:
            return "gpio", override[connector]
        for kind, section in (("can", "can"), ("spi", "spi"), ("uart", "uart"),
                              ("sdmmc", "sdmmc"), ("i2c", "i2c"), ("analog", "analog_in"),
                              ("gpio", "gpio")):
            if connector in self.board.get(section, {}):
                return kind, self.board[section][connector]
        where = f"board '{self.name}' ({self.board['id']})"
        model = self.pin_model
        status = model.status(connector) if model is not None else None
        if connector in (self.board.get("unwired") or {}):
            at = f" ({status.where})" if status is not None else ""
            raise SystemError(f"{where}: {connector}{at} leaves the module but is not "
                              f"emulated yet: the catalogue lists it as unwired, with no "
                              f"port or ADC channel to wire it to")
        if status is not None:
            raise SystemError(f"{where}: {connector} is not routed off the "
                              f"{model.board_name} ({model.label}: class {status.cls}"
                              f"{', ' + status.where if status.cls == 'onboard' else ''})")
        raise SystemError(f"{where} has no connector or pin '{connector}'"
                          + (f" (nor has its {model.label})" if model is not None else ""))

    def routed(self, connector: str) -> bool | None:
        """Whether the role's backplane connects a connector or pin: one of
        its `pins`, or on the board itself (`onboard`). None when the role
        says nothing of its routing (or the board has no roles)."""
        pins = (self.role_spec or {}).get("pins")
        if pins is None:
            return None
        return connector in pins or connector in (self.board.get("onboard") or {})


class System:
    def __init__(self, path: Path, catalog: Path = CATALOG,
                 extra_card_dirs: Iterable[Path | str] = ()):
        """extra_card_dirs: directories an sd-card image may also come from,
        besides card_dirs() (vhil.sim.Sim's card_dirs, for a test's tmp_path)."""
        self.path = Path(path)
        self.catalog = Path(catalog)
        self.card_dirs = [*card_dirs(), *(Path(d) for d in extra_card_dirs)]
        self.doc = _load(self.path, "system")
        self.id = self.doc["id"]
        self.boards: dict[str, Board] = {}
        for name, spec in self.doc["boards"].items():
            # The bootloader comes with the board (every MainLite carries
            # one); its node ID, flash bus and firmware with the role the
            # system gives it, from the catalogue board's roles.
            board = _entry("board", spec["board"], catalog)
            role = spec.get("role")
            role_spec = (board.get("roles") or {}).get(role) if isinstance(role, str) else None
            b = Board(
                name, board, _entry("platform", board["platform"], catalog), {},
                _entry("firmware", board["bootloader"], catalog) if "bootloader" in board else None,
                (role_spec or {}).get("node_id"),
                list(spec.get("write_protect", [])),
                spec.get("firmware_ref"), spec.get("bootloader_ref"),
                role, (role_spec or {}).get("flash_bus"), role_spec,
                board_pin_model(board, catalog))
            self._check_role(b)
            b.firmware = self._firmware(b, spec, catalog)
            self.boards[name] = b
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

    def images(self) -> list[str]:
        """The firmware images a run needs: one per board, plus
        "<board>.bootloader" for each board with a bootloader."""
        out = []
        for b in self.boards.values():
            out.append(b.name)
            if b.bootloader is not None:
                out.append(f"{b.name}.bootloader")
        return out

    def _check_names(self) -> None:
        """Every name, id, endpoint, ref and netdev of the system file has the
        shape the schema gives it."""
        def need(pattern, value, what):
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise SystemError(f"{what} {value!r} is not allowed here")
        need(ID, self.id, "system id")
        for name, spec in self.doc["boards"].items():
            need(NAME, name, "board name")
            for key, pattern in (("board", ID), ("firmware", ID),
                                 ("firmware_ref", REF), ("bootloader_ref", REF)):
                if key in spec:
                    need(pattern, spec[key], f"board '{name}': {key}")
            if "role" in spec and spec["role"] not in ROLES:
                raise SystemError(f"board '{name}': role {spec['role']!r} is not allowed here "
                                  f"({', '.join(ROLES)})")
        for bus, spec in self.buses.items():
            need(NAME, bus, "bus name")
            for node in spec["nodes"]:
                need(ENDPOINT, node, f"bus '{bus}': node")
            if "host_netdev" in spec:
                need(NETDEV, spec["host_netdev"], f"bus '{bus}': host_netdev")
        for name, dev in self.devices.items():
            need(NAME, name, "device name")
            need(ID, dev["model"], f"device '{name}': model")
            for port in ("spi", "cs", "sdmmc", "i2c"):
                if port in dev:
                    need(ENDPOINT, dev[port], f"device '{name}': {port}")
            if "attach" in dev:
                need(NAME, dev["attach"], f"device '{name}': attach")
            for out, endpoint in dev.get("outputs", {}).items():
                need(NAME, out, f"device '{name}': output")
                need(ENDPOINT, endpoint, f"device '{name}': output {out}")
            for key in dev.get("params", {}):
                need(NAME, key, f"device '{name}': param")
        for name, spec in self.doc.get("port", {}).get("signals", {}).items():
            need(NAME, name, "port signal name")
            for kind, value in spec.items():
                if kind in ("can_rx", "can_tx"):
                    need(NAME, value["bus"], f"port signal '{name}': bus")
                else:
                    need(ENDPOINT, value, f"port signal '{name}'")
        for c in self.bench.get("power", []):
            need(NAME, c["board"], "bench power board")
        for r in self.bench.get("dac_routes", []):
            need(ENDPOINT, r["to"], "DAC route")
        for r in self.bench.get("gpio_routes", []):
            need(ENDPOINT, r["to"], "GPIO route")
        for r in self.bench.get("adc_routes", []):
            need(ENDPOINT, r["from"], "ADC route")

    # -- device params --------------------------------------------------------

    def card_image(self, value: str, where: str = "image") -> Path:
        """The real path of a card image, which must lie inside one of the
        card-image directories (card_dirs, extra_card_dirs): no '..', and no
        absolute path or symlink that leads out of them. A relative one is in
        the first directory."""
        if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
            raise SystemError(f"{where}: {value!r} is not a card image file name")
        if ".." in PurePosixPath(value).parts:
            raise SystemError(f"{where}: {value!r} climbs out with '..'")
        roots = [d.resolve() for d in self.card_dirs]
        if not roots:
            raise SystemError(f"{where}: no card-image directory is configured "
                              f"(${CARD_DIR_ENV})")
        candidate = Path(value) if Path(value).is_absolute() else roots[0] / value
        real = candidate.resolve()
        if not any(root in real.parents for root in roots):
            raise SystemError(f"{where}: {value!r} is not inside a card-image directory "
                              f"({', '.join(r.as_posix() for r in roots)}; ${CARD_DIR_ENV})")
        try:
            rn.quote(real.as_posix())   # it goes into the platform description as a string
        except rn.UnsafeText:
            raise SystemError(f"{where}: {value!r} has characters a card image path can't have "
                              f"(letters, digits, space and _ . , : / + = @ -)") from None
        return real

    def _check_params(self, name: str, dev: dict, values: dict) -> None:
        """Device params against their model: only the params it declares,
        each of its default's type (bool, integer, real) or, for a string, of
        its declared format (param_formats) or else a short plain word."""
        doc = dev["model_doc"]
        defaults, formats = doc.get("params", {}), doc.get("param_formats", {})
        unknown = set(values) - set(defaults)
        if unknown:
            raise SystemError(f"device '{name}': unknown params {sorted(unknown)}")
        for key, value in values.items():
            where = f"device '{name}': param {key}"
            default = defaults[key]
            if isinstance(default, bool):
                ok, kind = isinstance(value, bool), "true or false"
            elif isinstance(default, int):
                ok, kind = isinstance(value, int) and not isinstance(value, bool), "an integer"
            elif isinstance(default, float):
                ok, kind = (isinstance(value, (int, float)) and not isinstance(value, bool)
                            and math.isfinite(value)), "a finite number"
            elif formats.get(key) == "card-image":
                if not isinstance(value, str):
                    raise SystemError(f"{where} must be a card image file name, got {value!r}")
                if value:
                    self.card_image(value, where)
                continue
            elif key in formats:
                raise SystemError(f"model '{doc['id']}': unknown param format {formats[key]!r}")
            else:
                ok = isinstance(value, str) and bool(SAFE_PARAM.fullmatch(value))
                kind = "letters, digits and _ . + - (at most 128)"
            if not ok:
                raise SystemError(f"{where} must be {kind}, got {value!r}")

    def set_params(self, name: str, values: dict) -> None:
        """Override a device's params (vhil.sim.Sim's params), checked as the
        system file's are."""
        dev = self.devices[name]
        self._check_params(name, dev, values)
        dev["params"] = {**dev.get("params", {}), **values}

    @staticmethod
    def _firmware(b: Board, spec: dict, catalog: Path) -> dict:
        """The catalogue firmware a board runs: its role's, for a board with
        roles (a system names none: an ECU runs the ECU firmware), else the
        one the system names."""
        if b.role_spec is not None:
            ident = b.role_spec.get("firmware")
            if "firmware" in spec:
                raise SystemError(f"board '{b.name}': the {b.role} role sets the firmware "
                                  f"({ident}); a system names none, so drop `firmware:`")
            if not (catalog / "firmware" / f"{ident}.yaml").is_file():
                raise SystemError(f"board '{b.name}': role {b.role} has no firmware in the "
                                  f"catalogue yet (catalog/firmware/{ident}.yaml)")
            return _entry("firmware", ident, catalog)
        if "firmware" not in spec:
            raise SystemError(f"board '{b.name}': {b.board['id']} has no roles, so it needs "
                              f"a firmware")
        return _entry("firmware", spec["firmware"], catalog)

    @staticmethod
    def check_board_pins(board: dict, model: pm.PinModel | None) -> None:
        """The board against its pin model: every connector and pin it has
        (wired or `unwired`) is in the model and leaves the module, or is an
        on-board peripheral it lists in `onboard`; and every pin the model
        routes to the backplane is one of them, or one of a peripheral's."""
        bid = board["id"]
        unwired = board.get("unwired") or {}
        if model is None:
            if unwired:
                raise SystemError(f"board {bid}: its unwired pins need a pin_model")
            return
        onboard = board.get("onboard") or {}
        names = [c for s in WIRED for c in (board.get(s) or {})] + list(unwired)
        covered = set()
        for name in dict.fromkeys(names):
            status = model.status(name)
            if status is None:
                raise SystemError(f"board {bid}: {name} is not a pin or peripheral of its "
                                  f"{model.label}")
            if name in onboard:
                if status.cls != "onboard":
                    raise SystemError(f"board {bid}: onboard {name} is not on the board only "
                                      f"({model.label}: class {status.cls}, {status.where})")
            elif status.cls != "backplane":
                raise SystemError(f"board {bid}: {name} is not routed off the board "
                                  f"({model.label}: class {status.cls}, {status.where})")
            covered |= {p["port"] for p in model.pins_of(name)}
        missing = [p for p in model.backplane_pins() if p["port"] not in covered]
        if missing:
            raise SystemError(
                f"board {bid}: its {model.label} routes "
                + ", ".join(f"{p['port']} ({p['backplane'][0]['connector']}."
                            f"{p['backplane'][0]['pin']})" for p in missing)
                + " to the backplane, but the board lacks them: list each in `unwired` "
                  "with its kind")

    @staticmethod
    def check_board_roles(board: dict, catalog: Path = CATALOG) -> None:
        """The catalogue board's roles table: each role a known one, its flash
        bus a CAN connector, its firmware a catalogue id, its pin labels on
        connectors the board has (and, with a pin model, ones that leave the
        module), and its GPIO re-kinds on pins the board models as analog
        inputs only. Labels are display text: plain words. The board's own
        connectors and pins are checked against its pin model first."""
        model = board_pin_model(board, catalog)
        System.check_board_pins(board, model)
        connectors = ({c for s in WIRED for c in (board.get(s) or {})}
                      | set(board.get("unwired") or {}))
        bid = board["id"]

        def label(where: str, pin, text) -> None:
            if model is not None and where != "onboard" and isinstance(pin, str):
                # A role names what its backplane routes: only what leaves
                # the module can be.
                status = model.status(pin)
                if status is None:
                    raise SystemError(f"board {bid}: {where} labels {pin!r}, which its "
                                      f"{model.label} lacks")
                if status.cls != "backplane":
                    raise SystemError(f"board {bid}: {where} routes {pin}, which does not "
                                      f"leave the board ({model.label}: class {status.cls}, "
                                      f"{status.where})")
            if pin not in connectors:
                raise SystemError(f"board {bid}: {where} labels {pin!r}, which it lacks")
            if not isinstance(text, str) or not LABEL.fullmatch(text):
                raise SystemError(f"board {bid}: {where} label {text!r} of {pin} is not a "
                                  f"plain word (letters, digits and _ + -, at most 32)")
        for pin, text in (board.get("onboard") or {}).items():
            label("onboard", pin, text)
        for name, role in (board.get("roles") or {}).items():
            if name not in ROLES:
                raise SystemError(f"board {bid}: role {name!r} is not one of "
                                  f"{', '.join(ROLES)}")
            if role.get("flash_bus") not in board.get("can", {}):
                raise SystemError(f"board {bid}: role {name}'s flash_bus "
                                  f"{role.get('flash_bus')!r} is not one of its CAN connectors")
            if not isinstance(role.get("firmware"), str) or not ID.fullmatch(role["firmware"]):
                raise SystemError(f"board {bid}: role {name}'s firmware "
                                  f"{role.get('firmware')!r} is not a catalogue id")
            doc = (role.get("backplane") or {}).get("doc")
            if doc is not None and (not isinstance(doc, str) or not BACKPLANE_DOC.fullmatch(doc)):
                raise SystemError(f"board {bid}: role {name}'s backplane page {doc!r} is not "
                                  f"a docs/backplanes/ page")
            for pin, text in (role.get("pins") or {}).items():
                label(f"role {name}", pin, text)
            for pin, target in (role.get("gpio") or {}).items():
                # Only a kind for a pin the board models, and only analog ->
                # GPIO: the board says where the pin is, the role what it is.
                if pin not in (board.get("analog_in") or {}) or pin in (board.get("gpio") or {}):
                    raise SystemError(f"board {bid}: role {name} makes {pin!r} a GPIO, but "
                                      f"the board models no analog input {pin!r} to re-kind")
                if (not isinstance(target, dict) or not isinstance(target.get("port"), str)
                        or not isinstance(target.get("pin"), int) or isinstance(target["pin"], bool)):
                    raise SystemError(f"board {bid}: role {name}'s GPIO {pin} needs a port "
                                      f"and a pin number")

    def _check_role(self, b: Board) -> None:
        """A board with roles (the MainLite: ECU, AMS, uDV) is placed in one of
        them, which gives its bootloader's node ID, flash bus and firmware.
        A board without roles takes none."""
        roles = b.board.get("roles") or {}
        self.check_board_roles(b.board, self.catalog)
        if not roles:
            if b.role is not None:
                raise SystemError(f"board '{b.name}': {b.board['id']} has no roles, "
                                  f"so no role {b.role!r}")
            return
        if b.role is None:
            raise SystemError(f"board '{b.name}': a {b.board['id']} needs a role "
                              f"({', '.join(roles)})")
        if b.role not in roles:
            raise SystemError(f"board '{b.name}': role {b.role!r} is not one of "
                              f"{b.board['id']}'s ({', '.join(roles)})")

    def _check(self) -> None:
        self._check_names()
        for b in self.boards.values():
            self._check_role(b)
            if b.bootloader_ref is not None and b.bootloader is None:
                raise SystemError(f"board '{b.name}': a bootloader_ref needs a bootloader, "
                                  f"and {b.board['id']} carries none")
            if b.write_protect and "write_protect" not in b.platform.get("renode", {}):
                raise SystemError(f"board '{b.name}': platform {b.platform['id']} has no "
                                  f"option bytes to write-protect sectors in")
            for connector in b.firmware.get("can", {}).get("contract", []):
                if connector not in b.board.get("can", {}):
                    raise SystemError(f"board '{b.name}': firmware {b.firmware['id']}'s CAN "
                                      f"contract rides {connector}, which {b.board['id']} lacks")
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
            for port, want in (("spi", "spi"), ("cs", "gpio"), ("sdmmc", "sdmmc"),
                               ("i2c", "i2c")):
                if interface.get(port) and port not in dev:
                    raise SystemError(f"device '{name}' ({dev['model']}) needs '{port}'")
                if port in dev:
                    if not interface.get(port):
                        raise SystemError(f"device '{name}' ({dev['model']}) takes no '{port}'")
                    board, kind, _ = self.resolve(dev[port])
                    if kind != want:
                        raise SystemError(f"device '{name}': {port} '{dev[port]}' is {kind}, not {want}")
                    boards.add(board.name)
            doc = dev["model_doc"]
            if doc["backend"] == "analog":
                an = doc.get("analog")
                if (an is None or set(an["outputs"]) != set(interface.get("analog_out", []))
                        or an["input"] not in doc.get("params", {})):
                    raise SystemError(f"model '{doc['id']}': its analog section must define "
                                      f"every analog_out and follow one of its params")
            elif "renode" not in doc:
                raise SystemError(f"model '{doc['id']}': backend renode needs a renode section")
            elif bool(interface.get("i2c")) != ("targets" in doc["renode"]):
                # An I2C device is one or more targets, each at its address
                # (a BMI088's two dies); anything else is one peripheral.
                raise SystemError(f"model '{doc['id']}': an i2c model gives renode targets "
                                  f"(a type and an address each), any other model a renode type")
            want_out = set(interface.get("analog_out", []))
            if set(dev.get("outputs", {})) != want_out:
                raise SystemError(f"device '{name}' ({dev['model']}) drives outputs "
                                  f"{sorted(want_out)}, got {sorted(dev.get('outputs', {}))}")
            for out, endpoint in dev.get("outputs", {}).items():
                board, kind, _ = self.resolve(endpoint)
                if kind != "analog":
                    raise SystemError(f"device '{name}': output {out} to '{endpoint}', "
                                      f"which is {kind}, not an analog input")
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
            self._check_params(name, dev, dev.get("params", {}))
        for name in self.devices:
            self.board_of_device(name)   # every device reaches a board, no cycles
        for name, spec in self.doc.get("port", {}).get("signals", {}).items():
            (kind, value), = spec.items()
            if kind in ("can_rx", "can_tx"):
                if value["bus"] not in self.buses:
                    raise SystemError(f"port signal '{name}': no bus '{value['bus']}'")
                continue
            _, got, _ = self.resolve(value)
            want = "analog" if kind == "analog" else "gpio"
            if got != want:
                raise SystemError(f"port signal '{name}': '{value}' is {got}, not {want}")
        for c in self.bench.get("power", []):
            if c["board"] not in self.boards:
                raise SystemError(f"bench power entry '{c['board']}' is not a board in this system")
        for r in self.bench.get("dac_routes", []):
            _, kind, _ = self.resolve(r["to"])
            if kind != "analog":
                raise SystemError(f"DAC route to '{r['to']}': not an analog input")
        # A TCA pin either powers a board or drives one GPIO; an ADC channel
        # reads one GPIO.
        relays = {(c["relay"]["addr"], c["relay"]["port"], c["relay"]["pin"])
                  for c in self.bench.get("power", [])}
        seen: set = set()
        for r in self.bench.get("gpio_routes", []):
            key = (r["tca"], r["port"], r["pin"])
            where = f"GPIO route from TCA {r['tca']:#04x} port {r['port']} pin {r['pin']}"
            if key in relays:
                raise SystemError(f"{where}: that pin is a board's power relay")
            if key in seen:
                raise SystemError(f"{where}: routed twice")
            seen.add(key)
            _, kind, _ = self.resolve(r["to"])
            if kind != "gpio":
                raise SystemError(f"GPIO route to '{r['to']}': {kind}, not a GPIO")
        seen = set()
        for r in self.bench.get("adc_routes", []):
            key = (r["adc"], r["channel"])
            if key in seen:
                raise SystemError(f"ADC route on ADC {r['adc']} channel {r['channel']}: "
                                  f"routed twice")
            seen.add(key)
            _, kind, _ = self.resolve(r["from"])
            if kind != "gpio":
                raise SystemError(f"ADC route from '{r['from']}': {kind}, not a GPIO")
        self.warnings = self._routing_warnings()

    def endpoints(self) -> list[str]:
        """Every board endpoint the system wires, once each, in file order."""
        used = [node for spec in self.buses.values() for node in spec["nodes"]]
        for dev in self.devices.values():
            used += [dev[p] for p in ("spi", "cs", "sdmmc", "i2c") if p in dev]
            used += list(dev.get("outputs", {}).values())
        for spec in self.doc.get("port", {}).get("signals", {}).values():
            used += [v for k, v in spec.items() if k not in ("can_rx", "can_tx")]
        used += [r["to"] for r in self.bench.get("dac_routes", [])]
        used += [r["to"] for r in self.bench.get("gpio_routes", [])]
        used += [r["from"] for r in self.bench.get("adc_routes", [])]
        return list(dict.fromkeys(used))

    def _routing_warnings(self) -> list[str]:
        """What the system wires that its board's role leaves unconnected on
        the backplane (the catalogue role's `pins`). A warning, not an error:
        the emulated MCU has the pin either way, and a test may mean to."""
        out = []
        for endpoint in self.endpoints():
            name, connector = endpoint.split(".", 1)
            b = self.boards[name]
            if b.routed(connector) is False:
                bp = b.role_spec.get("backplane")
                where = (f"the {bp['name']} backplane ({bp['doc']})" if bp
                         else f"the {b.role} role's backplane")
                out.append(f"{b.name}: {connector} is not connected on {where}")
        return out

    def can_of(self, board: str) -> dict[str, str]:
        """CAN controller -> bus name, for one board instance."""
        out = {}
        for bus, spec in self.buses.items():
            for node in spec["nodes"]:
                b, _, controller = self.resolve(node)
                if b.name == board:
                    out[controller] = bus
        return out

    def flash_bus(self, board: str) -> str | None:
        """The bus a board is flashed over (its flash_bus connector's), or None
        if it names none or that connector is on no bus of this system."""
        b = self.boards[board]
        if b.flash_bus is None:
            return None
        return self.can_of(board).get(b.board["can"][b.flash_bus])

    def contract_buses(self, board: str) -> list[str]:
        """The buses a board's firmware CAN contract rides: the buses of the
        connectors its catalogue firmware names (`can.contract`), or every
        bus the board is on if it names none."""
        wanted = self.boards[board].firmware.get("can", {}).get("contract")
        out = []
        for bus, spec in self.buses.items():
            if spec["kind"] != "can":
                continue
            for node in spec["nodes"]:
                name, connector = node.split(".", 1)
                if name == board and (wanted is None or connector in wanted):
                    out.append(bus)
        return out

    def board_of_device(self, name: str, _seen: tuple = ()) -> str:
        if name in _seen:
            raise SystemError(f"devices attach in a cycle: {' -> '.join(_seen + (name,))}")
        dev = self.devices[name]
        for port in ("spi", "cs", "sdmmc", "i2c"):
            if port in dev:
                return self.resolve(dev[port])[0].name
        for endpoint in dev.get("outputs", {}).values():
            return self.resolve(endpoint)[0].name
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

    @staticmethod
    def _params(dev: dict) -> dict:
        return dict(dev["model_doc"].get("params", {}), **dev.get("params", {}))

    def analog_levels(self, name: str) -> dict[str, float]:
        """An analog device's output voltages at its params, by endpoint."""
        dev = self.devices[name]
        an = dev["model_doc"]["analog"]
        params = self._params(dev)
        x = float(params[an["input"]])
        levels = {}
        for out, endpoint in dev["outputs"].items():
            o = an["outputs"][out]
            v = o["offset_V"] + o["gain_V"] * x
            levels[endpoint] = min(max(v, o.get("min_V", v)), o.get("max_V", v))
        return levels

    def _renode_device(self, name: str, dev: dict) -> str:
        """Platform-description text attaching one device (or a counted chain).
        Every value goes through vhil.renode's encoders, so even a param that
        got past _check_params can't end its line or add an entry."""
        self._check_params(name, dev, dev.get("params", {}))
        model = dev["model_doc"]
        renode = model["renode"]
        local = lambda p: rn.path(p.split(".", 1)[1] if p.startswith("sysbus.") else p)
        params = self._params(dev)
        formats = model.get("param_formats", {})
        # An empty string is an unset param: left out, so Renode picks the
        # constructor without it. A set one may bring arguments with it.
        args = {}
        for k, v in params.items():
            if v == "":
                continue
            if formats.get(k) == "card-image":
                v = self.card_image(v, f"device '{name}': param {k}").as_posix()
            args[rn.ident(renode.get("params", {}).get(k, k))] = v
            args.update({rn.ident(a): x for a, x in renode.get("set_also", {}).get(k, {}).items()})
        def value(v):
            if isinstance(v, bool):
                return str(v).lower()
            return rn.quote(v) if isinstance(v, str) else rn.number(v)
        body = [f"    {k}: {value(v)}" for k, v in args.items()]
        name = rn.ident(name)
        lines = []
        if "attach" in dev:
            for i in range(int(dev.get("count", 1))):
                lines += [f"{name}{i}: {rn.path(renode['type'])} @ {rn.ident(dev['attach'])} {i}"]
                lines += body
        elif "targets" in renode:
            # One peripheral per I2C target, named <device>_<target>.
            bus = local(self.resolve(dev["i2c"])[2])
            for part, t in renode["targets"].items():
                lines += [f"{name}_{rn.ident(part)}: {rn.path(t['type'])} @ {bus} "
                          f"0x{int(t['address']):02X}"] + body
        else:
            port = dev.get("spi") or dev.get("sdmmc")
            parent = local(self.resolve(port)[2]) if port else "sysbus"
            lines += [f"{name}: {rn.path(renode['type'])} @ {parent}"] + body
        if "cs" in dev:
            gpio = self.resolve(dev["cs"])[2]
            lines += ["", f"{local(gpio['port'])}:", f"    {int(gpio['pin'])} -> {name}@0"]
        return "\n".join(lines)

    # -- outputs ------------------------------------------------------------

    def render_renode(self, firmware: dict[str, Path] | None = None,
                      socketcan: bool = False) -> str:
        """The Renode script for this system. Without a path for a board, the
        script expects `$elf_<board>` to be set before it is included."""
        firmware = firmware or {}
        self._check_names()
        out = [rn.comment(f":name: {self.id} (generated by vhil.system from {self.path.name})"),
               ":description: Generated. Edit the system file, not this script.", "",
               "using sysbus", ""]
        quantum = self.doc.get("time", {}).get("quantum_s")
        if quantum:
            rn.number(quantum)
            out += [f"emulation SetGlobalQuantum {rn.quote(format(quantum, 'g'))}", ""]
        for bus in self.buses:
            out.append(f"emulation CreateCANHub {rn.quote(rn.ident(bus))}")
        out.append("")
        # Platform models first, in catalogue order: a device model may build
        # on one (an I2C target on the I2C controller's interface).
        sources = list(dict.fromkeys(
            (REPO / src).as_posix()
            for b in self.boards.values() for src in b.platform["renode"].get("sources", [])))
        sources += sorted({(REPO / d["model_doc"]["renode"]["source"]).as_posix()
                           for d in self.devices.values()
                           if "source" in d["model_doc"].get("renode", {})} - set(sources))
        if sources:
            out += [f"include {rn.file_arg(src)}" for src in sources] + [""]
        for b in self.boards.values():
            if b.platform["backend"] != "renode":
                raise SystemError(f"board '{b.name}': backend {b.platform['backend']} is not supported")
            platform = b.platform["renode"]
            cpu = rn.ident(platform.get("cpu", "cpu"))
            var = f"elf_{rn.ident(b.name)}"
            out.append(rn.comment(f"# --- {b.name}: {b.board['id']} running {b.firmware['id']} "
                                  f"({b.firmware['repo']})"))
            out.append(f"mach create {rn.quote(b.name)}")
            if b.name in firmware:
                # After `mach create`: a variable set while a machine is
                # selected is local to it, so the next board would not see it.
                out.append(f"${var}={rn.file_arg(firmware[b.name])}")
            out.append(f"machine LoadPlatformDescription {rn.file_arg(REPO / platform['repl'])}")
            out += [line.format(board=b.name) for line in platform.get("setup", [])]
            if b.write_protect:
                # Option bytes are flash: burned once, before the first boot,
                # they survive every reset and power cycle (not in the macro).
                mask = sum(1 << int(s) for s in b.write_protect)
                out.append(platform["write_protect"].format(mask=mask))
            for name in self.devices_on(b.name):
                dev = self.devices[name]
                if dev["model_doc"]["backend"] == "analog":
                    # Pin voltages live outside the MCU: they survive machine
                    # Reset, so they are set once, not in the reset macro.
                    self._check_params(name, dev, dev.get("params", {}))
                    out.append(rn.comment(f"# device {name}: {dev['model']} "
                                          f"({dev['model_doc']['analog']['input']} = "
                                          f"{self._params(dev)[dev['model_doc']['analog']['input']]})"))
                    for endpoint, volts in self.analog_levels(name).items():
                        target = self.resolve(endpoint)[2]
                        out.append(f"{rn.path(target['adc'])} SetVoltage {round(volts * 1e6)} "
                                   f"{int(target['channel'])}")
                    continue
                out += [rn.comment(f"# device {name}: {dev['model']}"
                                   + (f" x{dev['count']} on {dev['attach']}"
                                      if "count" in dev else "")),
                        'machine LoadPlatformDescriptionFromString """',
                        self._renode_device(name, dev), '"""']
            for controller, bus in self.can_of(b.name).items():
                out.append(f"connector Connect {rn.path(controller)} {rn.ident(bus)}")
            if b.bootloader is not None:
                # Flash as a provisioned board holds it, loaded once: flash
                # survives a reset (and a power cut), so what the bootloader
                # programs stays. A reset starts the bootloader, as on the chip,
                # and the bootloader sets VTOR to the app when it jumps.
                # Without the images, the script expects $flash_<board> and
                # $elf_<board>_bootloader, as it expects $elf_<board>.
                if b.name in firmware and f"{b.name}.bootloader" in firmware:
                    flash = rn.file_arg(self._flash_image(b, firmware))
                    bl_elf = rn.file_arg(self._image(firmware, b.name + ".bootloader"))
                else:
                    flash, bl_elf = f"$flash_{b.name}", f"$elf_{b.name}_bootloader"
                out += [f"sysbus LoadBinary {flash} 0x{FLASH_BASE:08X}",
                        f"sysbus LoadSymbolsFrom {bl_elf}",
                        f"sysbus LoadSymbolsFrom ${var}",
                        "macro reset", '"""']
                vtor = b.bootloader.get("load", {}).get("vector_table")
            else:
                # A board that carries no bootloader boots its image from where
                # the image's vector table is linked.
                out += ["macro reset", '"""', f"    sysbus LoadELF ${var}"]
                vtor = b.firmware.get("load", {}).get("vector_table")
            if vtor is not None:
                out.append(f"    {cpu} VectorTableOffset 0x{vtor:08X}")
            # Renode runs the macro on every reset, the CPU's own (watchdog,
            # SYSRESETREQ) as well as `machine Reset`.
            out += [f"    {line.format(cpu=cpu)}" for line in platform.get("reset", [])]
            out += ['"""', "runMacro $reset", ""]
        if socketcan:
            bridged = [(bus, spec) for bus, spec in self.buses.items() if spec.get("host_netdev")]
            for bus, spec in bridged:
                owner = self.resolve(spec["nodes"][0])[0].name
                netdev = spec["host_netdev"]
                if not NETDEV.fullmatch(netdev):
                    raise SystemError(f"bus '{bus}': host_netdev {netdev!r} is not allowed here")
                out += [f"mach set {rn.quote(rn.ident(owner))}",
                        f"machine CreateSocketCANBridge {rn.quote('br_' + rn.ident(bus))} "
                        f"{rn.quote(netdev)}",
                        f"connector Connect br_{bus} {bus}"]
        return "\n".join(out).rstrip() + "\n"

    @staticmethod
    def _image(firmware: dict, key: str) -> Path:
        if key not in firmware:
            raise SystemError(f"no firmware image for '{key}'")
        return Path(firmware[key]).resolve()

    def _flash_image(self, b: Board, firmware: dict) -> Path:
        """The board's provisioned flash (vhil.flash_image), from the flat
        .bin next to each ELF (build_firmware writes them)."""
        def flat(elf: Path) -> bytes:
            binary = elf.with_suffix(".bin")
            if not binary.exists():
                subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(binary)],
                               check=True)
            return binary.read_bytes()
        data = flash_image.build(flat(self._image(firmware, b.name + ".bootloader")),
                                 flat(self._image(firmware, b.name)), b.node_id)
        path = Path(tempfile.mkdtemp(prefix=f"vhil-flash-{b.name}-")) / "flash.bin"
        path.write_bytes(data)
        return path

    def bench_config(self) -> dict:
        """The virtual broker's wiring (see vhil/broker.py)."""
        power = [{"machine": c["board"], "slot": c.get("slot"), "relay": c["relay"],
                     "ina_addr": c["ina_addr"], "current_A": c["current_A"],
                     "can": self.can_of(c["board"]),
                     "vbat": self.boards[c["board"]].board.get("vbat", True)}
                    for c in self.bench.get("power", [])]
        routes = []
        for r in self.bench.get("dac_routes", []):
            board, _, target = self.resolve(r["to"])
            routes.append({"dac": r["dac"], "channel": r["channel"], "machine": board.name,
                           "adc": target["adc"], "adc_channel": target["channel"]})
        config = {"power": power, "dac_routes": routes}
        # Present only in a system that routes them, so the others' wiring
        # stays as it was.
        if "gpio_routes" in self.bench:
            config["gpio_routes"] = []
            for r in self.bench["gpio_routes"]:
                board, _, target = self.resolve(r["to"])
                config["gpio_routes"].append(
                    {"tca": r["tca"], "port": r["port"], "pin": r["pin"],
                     "machine": board.name, "gpio_port": target["port"],
                     "gpio_pin": target["pin"]})
        if "adc_routes" in self.bench:
            config["adc_routes"] = []
            for r in self.bench["adc_routes"]:
                board, _, target = self.resolve(r["from"])
                config["adc_routes"].append(
                    {"adc": r["adc"], "channel": r["channel"], "machine": board.name,
                     "gpio_port": target["port"], "gpio_pin": target["pin"]})
        return config

    def build_firmware(self, workdir: Path, refs: dict[str, str] | None = None,
                       log=print) -> dict[str, Path]:
        """Clone each firmware source at its ref and build it with its recipe.
        The ref is `refs[image]` if given, else the board's firmware_ref /
        bootloader_ref in the system file, else the catalogue entry's.
        Returns image key (System.images: "<board>", "<board>.bootloader")
        -> ELF; a flat .bin is written next to it."""
        refs, built, workdir = refs or {}, {}, Path(workdir)
        by_source: dict[tuple, Path] = {}
        sources = [(b.name, b.firmware, b.ref("firmware")) for b in self.boards.values()]
        sources += [(f"{b.name}.bootloader", b.bootloader, b.ref("bootloader"))
                    for b in self.boards.values() if b.bootloader is not None]
        for name, fw, default_ref in sources:
            ref = refs.get(name, default_ref)
            key = (fw["id"], fw["repo"], ref)
            if key not in by_source:
                src = source_dir(workdir, fw, ref)
                if src.exists():
                    shutil.rmtree(src)
                log(f"[{name}] cloning {fw['repo']}@{ref}")
                cmd = ["git", "clone", "-q", "--depth", "1", "-b", ref,
                       f"https://github.com/{fw['repo']}", str(src)]
                if fw.get("submodules"):
                    cmd[2:2] = ["--recurse-submodules"]
                subprocess.run(cmd, check=True)
                # Build output goes to stderr: stdout carries only the
                # board=elf results, so callers can parse it.
                for step in ("configure", "build"):
                    log(f"[{name}] {fw['build'][step]}")
                    subprocess.run(fw["build"][step], shell=True, cwd=src, check=True,
                                   stdout=sys.stderr)
                elf = src / fw["build"]["elf"]
                subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(elf),
                                str(elf.with_suffix(".bin"))], check=True)
                by_source[key] = elf
            built[name] = by_source[key]
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
            # Warnings don't fail validation: the system runs as written.
            for warning in system.warnings:
                print(f"warning: {args.system}: {warning}", file=sys.stderr)
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
