"""The system editor's backend: Antmicro's Pipeline Manager as the UI (M5, #13).

    python -m vhil.editor spec -o build/spec.json           # node types from catalog/
    python -m vhil.editor to-graph systems/ams.yaml -o build/ams.json
    python -m vhil.editor to-system build/ams.json -o build/ams.yaml
    python -m vhil.editor serve [--port 9000]               # JSON-RPC backend
    python -m vhil.editor check [systems/*.yaml]            # Pipeline Manager's ./validate

The system file stays the source of truth (vision principle 1): the editor's
graph is a view of it, translated both ways here. Catalogue entries become
node types:

  board     a node with one typed connector per CAN/SPI/SDMMC/I2C/GPIO/analog pin,
            and per pin its pin model has leave the module that the emulator
            doesn't wire yet (`unwired`: offered, refused by validate), in two
            columns; a board with roles (the MainLite) has a role select, which
            sets its firmware and labels each pin with what the role's
            backplane carries on it ("PF8 · APPS_1"), hiding what it leaves
            unconnected ("FDCAN3 · n.c.") unless that is wired: the backend
            relabels a node when its role changes (properties_on_change). A
            board without roles has a firmware select
  CAN bus   a node with a BUS interface: connect any number of CAN connectors
  model     a node per chip model: its host-side ports (spi, cs, sdmmc, i2c), the
            port it provides (an LTC6820 provides `isospi`) and the port it
            attaches to (an LTC6811 attaches to `isospi`), with count/params

What is not a graph (id, description, time, port, bench wiring) travels in the
graph's additionalData, unchanged by the editor. Formats:
https://antmicro.github.io/kenning-pipeline-manager/specification-format.html
https://antmicro.github.io/kenning-pipeline-manager/dataflow-format.html
"""
from __future__ import annotations

import argparse
import base64 as b64
import io
import json
import logging
import os
import re
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path

import yaml

from vhil.system import CATALOG, ID, REPO, System, SystemError

# Pipeline Manager's specification/dataflow format version these were written
# against (the docs' examples).
FORMAT_VERSION = "20250623.14"

BUS_NODE = "CAN bus"
GRAPH_ID = "system"
# Between a pin and its label in an interface's name ("PF8 · APPS_1").
ROLE_SEP = " · "
# The label of a pin the role's backplane leaves unconnected.
NOT_CONNECTED = "n.c."
# A bus interface's length in pixels: each connection lands on a stub along
# it. Pipeline Manager places a new stub at size / 2, so a tiny bus stacks
# every connection on the node header.
BUS_SIZE, BUS_PITCH = 120, 40
# A bus node's height above its bus (header, host_netdev, margin), in pixels.
BUS_NODE_HEIGHT = 200
# How far apart boards stack in a graph made from a system: a board node's
# height and a margin, in pixels.
BOARD_PITCH = 900
# A board node's width in pixels: a pin and its label fit on each side
# ("PC2_C · DEBUG_LED3"), so its pins sit in two columns and the node is
# about as wide as it is tall.
BOARD_WIDTH = 520

# The canvas look (docs/architecture/editor-workspace.md, "Tokens"), in
# what Pipeline Manager v0.5.2's metadata takes (its
# resources/schemas/metadata_schema.json): a port's and its wire's colour and
# pattern per interface type, and a header colour, icon and pill per node
# category. Wire colours follow the type, never status: CAN is the amber
# backbone, analog teal so it isn't read as "ok" green, GPIO a dotted grey.
_WIRES = {"can": ("#f59f00", "solid"), "spi": ("#4dabf7", "solid"),
          "isospi": ("#4dabf7", "solid"), "i2c": ("#3bc9db", "solid"),
          "sdmmc": ("#9775fa", "solid"), "uart": ("#e599f7", "solid"),
          "gpio": ("#adb5bd", "dotted"), "analog": ("#20c997", "dashed")}
# Category -> (header colour, built-in icon, pill). Headers are the raised
# surface (--bg-3); the pill names the kind in its wire colour.
_CATEGORY_STYLES = {"Boards": ("#262c36", "Cube", {"text": "board", "color": "#4c8dff"}),
                    "Buses": ("#262c36", "Backend", {"text": "CAN", "color": "#f59f00"}),
                    "Models": ("#1d222a", "Cogwheel", {"text": "device", "color": "#adb5bd"})}
CANVAS_METADATA = {
    "interfaces": {t: {"interfaceColor": c, "interfaceConnectionColor": c,
                       "interfaceConnectionPattern": p} for t, (c, p) in _WIRES.items()},
    "styles": {cat: {"color": c, "icon": i, "pill": pill}
               for cat, (c, i, pill) in _CATEGORY_STYLES.items()},
    # --bg-0, a 24 px grid and an 8 px snap (the 4 px spacing grid, doubled).
    "backgroundColor": "#0f1115", "backgroundSize": 24, "movementStep": 8,
    # A system is opened by the shell, never dropped in as a file, and node
    # types come from the catalogue: no welcome panel, no "new node type" or
    # "new graph" entries in the palette.
    "welcome": False, "newGraphNode": False, "newNodeType": False,
}

# Board catalogue section -> interface type. A board's pins are two columns:
# on the left what devices attach to (they sit to its left in a graph: SPI,
# UART, SDMMC, I2C, analog inputs), on the right the CAN connectors (the
# buses are to its right) and the digital lines.
_BOARD_PORTS = (("can", "can"), ("spi", "spi"), ("uart", "uart"), ("sdmmc", "sdmmc"),
                ("i2c", "i2c"), ("gpio", "gpio"), ("analog_in", "analog"))
_RIGHT = ("can", "gpio")
# Model host-side ports: system device field -> interface type.
_MODEL_HOST_PORTS = (("spi", "spi"), ("cs", "gpio"), ("sdmmc", "sdmmc"), ("i2c", "i2c"))
# Field order in a written system file.
_DEVICE_FIELDS = ("model", "spi", "cs", "sdmmc", "i2c", "outputs", "attach", "count", "params")
_SYSTEM_FIELDS = ("kind", "id", "description", "time", "boards", "buses", "devices", "port",
                  "bench")


def _catalog(kind: str, catalog: Path = CATALOG) -> dict[str, dict]:
    folder = {"board": "boards", "firmware": "firmware", "model": "models"}[kind]
    docs = (yaml.safe_load(p.read_text()) for p in sorted((catalog / folder).glob("*.yaml")))
    return {d["id"]: d for d in docs}


def pin_of(interface_name: str) -> str:
    """The board pin an interface stands for: "PF8 · APPS_1" -> PF8. The
    label is display only; a system names the pin."""
    return interface_name.split(ROLE_SEP, 1)[0]


def role_choice(name: str, role: dict) -> str:
    """A role as the node's role select offers it: "ecu (node 0x1)"."""
    return f"{name} (node 0x{role['node_id']:X})"


def role_of(choice) -> str:
    """The role a role select value names: "ecu (node 0x1)" -> ecu."""
    return str(choice).split(" ", 1)[0]


def role_firmware(role: dict, firmware: dict) -> str:
    """A role's firmware property: an ECU runs the ECU firmware."""
    fw = role["firmware"]
    return fw if fw in firmware else f"{fw} (not in the catalogue yet)"


def _board_pins(board: dict) -> list[tuple[str, str]]:
    """(pin, kind) of a board's connectors and pins, in catalogue order, then
    those its pin model routes off the module that the emulator doesn't wire
    yet (`unwired`: offered, refused by validate)."""
    return ([(pin, kind) for section, kind in _BOARD_PORTS for pin in board.get(section, {})]
            + list((board.get("unwired") or {}).items()))


def _readings(board: dict, role: dict | None) -> list[tuple[str, str, str]]:
    """(pin, interface name, kind) of a board's pins. In a role, each reads
    its car signal ("PF8 · APPS_1") and takes the role's GPIO re-kinds (AMS
    PF9 = TSMS is GPIO); what the role's backplane leaves unconnected reads
    "FDCAN3 · n.c." and comes last. Without a role, the pin's name."""
    if role is None:
        return [(pin, pin, kind) for pin, kind in _board_pins(board)]
    routes, onboard = role.get("pins") or {}, board.get("onboard") or {}
    override = role.get("gpio") or {}
    routed, unrouted = [], []
    for pin, kind in _board_pins(board):
        label = routes.get(pin) or onboard.get(pin)
        if label is None:
            unrouted.append((pin, f"{pin}{ROLE_SEP}{NOT_CONNECTED}", kind))
        else:
            routed.append((pin, f"{pin}{ROLE_SEP}{label}",
                           "gpio" if kind == "analog" and pin in override else kind))
    return routed + unrouted


def _variants(board: dict) -> dict[str, dict[str, str]]:
    """Per pin, every way it reads in some role: {interface name: kind}."""
    out: dict[str, dict[str, str]] = {pin: {} for pin, _ in _board_pins(board)}
    for role in board["roles"].values():
        for pin, name, kind in _readings(board, role):
            out[pin][name] = kind
    return out


def _side(kind: str) -> str:
    return "right" if kind in _RIGHT else "left"


def _rows(ifaces: list[dict]) -> list[dict]:
    """Number each side's interfaces from the top, in list order (their
    sidePosition). Pipeline Manager keeps an empty row for a position no
    shown interface takes."""
    rows = {"left": 0, "right": 0}
    for iface in ifaces:
        iface["sidePosition"] = rows[iface["side"]]
        rows[iface["side"]] += 1
    return ifaces


def role_interfaces(board: dict, role: str, wired=(), ids=None) -> tuple[list[dict], list[dict]]:
    """What a node of a board in `role` shows, as a dataflow node's
    (interfaces, enabledInterfaceGroups): the pins the role's backplane
    routes, and of the rest those in `wired` (a pin a system wires shows,
    and validate warns of it), top to bottom on each side. `ids(pin)` names
    each interface's ID (default: the pin)."""
    variants = _variants(board)
    shown = [(pin, name, kind) for pin, name, kind in _readings(board, board["roles"][role])
             if not name.endswith(ROLE_SEP + NOT_CONNECTED) or pin in wired]
    ifaces = _rows([{"id": ids(pin) if ids else pin, "name": name, "direction": "inout",
                     "side": _side(kind)} for pin, name, kind in shown])
    groups = [{"name": name, "direction": "inout"} for pin, name, _ in shown
              if len(variants[pin]) > 1]
    return ifaces, groups


# -- specification -------------------------------------------------------------

def _property(name: str, value) -> dict:
    if isinstance(value, bool):
        return {"name": name, "type": "bool", "default": value}
    if isinstance(value, int):
        return {"name": name, "type": "integer", "default": value}
    if isinstance(value, float):
        return {"name": name, "type": "number", "default": value}
    return {"name": name, "type": "text", "default": str(value)}


def _iface(name: str, kind: str) -> dict:
    return {"name": name, "type": kind, "direction": "inout", "side": _side(kind),
            "maxConnectionsCount": 1}


def _role_interfaces(board: dict) -> tuple[list[dict], list[dict]]:
    """A board with roles as one node type whose pins follow its role:
    (interfaces, interfaceGroups). A pin that reads the same in every role
    is an interface ("SDMMC1 · microSD"). Any other pin is an interface
    group per reading ("PF8 · APPS_1", "PF8 · S_CURRENT_N", "PF8 · n.c."),
    each made of the pin itself, an interface Pipeline Manager never draws:
    groups that share it can't be enabled together, so a pin shows at most
    one reading. A node shows a role's readings (role_interfaces); a new
    one, the first role's. Rows: the first role's readings in its order,
    then the others, so a new node has no empty rows."""
    variants = _variants(board)
    plain = {name: kind for names in variants.values() if len(names) == 1
             for name, kind in names.items()}
    grouped = {name: (pin, kind) for pin, names in variants.items() if len(names) > 1
               for name, kind in names.items()}
    first = next(iter(board["roles"]))
    order = [i["name"] for i in role_interfaces(board, first)[0]]
    order += [n for n in [*plain, *grouped] if n not in order]
    readings = _rows([_iface(n, plain[n]) if n in plain else
                      {**_iface(n, grouped[n][1]),
                       "interfaces": [{"name": grouped[n][0], "direction": "inout"}]}
                      for n in order])
    pins = [{"name": pin, "type": kind, "direction": "inout"} for pin, kind in _board_pins(board)
            if len(variants[pin]) > 1]
    return (pins + [i for i in readings if "interfaces" not in i],
            [i for i in readings if "interfaces" in i])


def _role_description(board: dict, firmware: dict) -> str:
    rows = ["| role | node | flash bus | firmware | backplane |", "|---|---|---|---|---|"]
    for name, role in board["roles"].items():
        bp = role.get("backplane") or {}
        rows.append(f"| {name} | 0x{role['node_id']:X} | {role['flash_bus']} | "
                    f"{role_firmware(role, firmware)} | "
                    + (f"[{bp['name']}]({bp['doc']})" if bp else "") + " |")
    return "\n\n".join([
        board.get("description", "").strip(),
        "The **role** this unit is provisioned for sets its bootloader's node ID, the bus "
        "its app is flashed over, its firmware and what its pins carry:",
        "\n".join(rows),
        f"Pins read *pin · car signal*. A pin the role's backplane leaves unconnected "
        f"(*{NOT_CONNECTED}*) is hidden unless it is wired, which validate warns of; "
        f"*Interface Groups* below shows one. Changing the role relabels the pins and "
        f"keeps their wires; one whose pin changes kind (AMS PF9 is digital, ECU PF9 "
        f"analog) is removed, with a notice."])


def specification(catalog: Path = CATALOG) -> dict:
    """Pipeline Manager node types for everything in the catalogue."""
    firmware_docs = _catalog("firmware", catalog)
    firmware = sorted(firmware_docs)
    nodes = []
    for board_id, board in _catalog("board", catalog).items():
        roles = board.get("roles") or {}
        tail = [
            # The bootloader comes with the board; only its ref is the
            # system's (the shell's firmware panel picks it).
            *([{"name": "bootloader", "type": "constant", "default": board["bootloader"],
                "description": "Firmware in sector 0: every unit of this board carries it."},
               {"name": "bootloader_ref", "type": "text", "default": "",
                "description": "Tag (or branch) of the bootloader to build (empty: the "
                               "catalogue's)."}]
              if "bootloader" in board else []),
            # A system's write_protect is test-only option-byte state
            # (tests/sim/test_flash_option_bytes.py): carried so a system
            # keeps it, never shown on the node.
            {"name": "write_protect", "type": "text", "default": "", "hidden": True,
             "description": "Test only: flash sectors write-protected in the option bytes "
                            "from the first power-on, comma-separated."}]
        ref = {"name": "firmware_ref", "type": "text", "default": "",
               "description": "Branch or tag of the firmware to build (empty: the catalogue's)."}
        if not roles:
            nodes.append({
                "name": board_id, "category": "Boards", "style": "Boards", "layer": "board",
                "width": BOARD_WIDTH, "twoColumn": True,
                "description": board.get("description", ""),
                "interfaces": _rows([_iface(name, kind)
                                     for _, name, kind in _readings(board, None)]),
                "properties": [{"name": "firmware", "type": "select", "values": firmware,
                                "default": firmware[0]}, ref, *tail],
                "additionalData": {"vhil": {"kind": "board", "board": board_id}},
            })
            continue
        # A board with roles (the MainLite): one node type, whose role select
        # sets its firmware, node ID, flash bus and what its pins carry.
        first, role = next(iter(roles.items()))
        interfaces, groups = _role_interfaces(board)
        nodes.append({
            "name": board_id, "category": "Boards", "style": "Boards", "layer": "board",
            "width": BOARD_WIDTH, "twoColumn": True,
            "description": _role_description(board, firmware_docs),
            "interfaces": interfaces, "interfaceGroups": groups,
            "defaultInterfaceGroups": role_interfaces(board, first)[1],
            "properties": [
                {"name": "role", "type": "select",
                 "values": [role_choice(n, r) for n, r in roles.items()],
                 "default": role_choice(first, role),
                 "description": "The role this unit is provisioned for: its bootloader's "
                                "node ID and flash bus, its firmware, and what its pins "
                                "carry on that role's backplane."},
                {"name": "firmware", "type": "constant",
                 "default": role_firmware(role, firmware_docs),
                 "description": "The role's firmware: an ECU runs the ECU firmware."},
                ref, *tail],
            "additionalData": {"vhil": {"kind": "board", "board": board_id,
                                        "roles": {role_choice(n, r): n
                                                  for n, r in roles.items()}}},
        })
    nodes.append({
        "name": BUS_NODE, "category": "Buses", "style": "Buses", "layer": "bus",
        "description": "A CAN bus. Connect every node's CAN connector to it.",
        "interfaces": [{"name": "bus", "type": "can", "direction": "inout",
                        "maxConnectionsCount": -1,
                        "bus": {"type": "twoSided", "size": BUS_SIZE}}],
        "properties": [{"name": "host_netdev", "type": "text", "default": "",
                        "description": "SocketCAN interface to bridge to (optional)."}],
        "additionalData": {"vhil": {"kind": "bus"}},
    })
    for model_id, model in _catalog("model", catalog).items():
        iface = model.get("interface", {})
        interfaces = [{"name": field, "type": itype, "direction": "inout", "side": "left",
                       "maxConnectionsCount": 1}
                      for field, itype in _MODEL_HOST_PORTS if iface.get(field)]
        if iface.get("attach"):
            interfaces.append({"name": iface["attach"], "type": iface["attach"],
                               "direction": "input", "side": "left", "maxConnectionsCount": 1})
        if iface.get("provides"):
            interfaces.append({"name": iface["provides"], "type": iface["provides"],
                               "direction": "output", "side": "right"})
        # Analog outputs drive a board's analog inputs.
        interfaces += [{"name": out, "type": "analog", "direction": "output", "side": "right",
                        "maxConnectionsCount": 1} for out in iface.get("analog_out", [])]
        properties = [_property(k, v) for k, v in (model.get("params") or {}).items()]
        if iface.get("attach"):
            properties.insert(0, {"name": "count", "type": "integer", "default": 1, "min": 1,
                                  "description": "Identical chips in a row on the port."})
        nodes.append({
            "name": model_id, "category": "Models", "style": "Models", "layer": "model",
            "description": model.get("description", ""),
            "interfaces": interfaces, "properties": properties,
            "additionalData": {"vhil": {"kind": "model"}},
        })
    return {"version": FORMAT_VERSION,
            # notifyWhenChanged: the frontend tells the backend of each edit,
            # so it can relabel a board whose role changes (properties_on_change).
            "metadata": {"connectionStyle": "orthogonal", "twoColumn": True,
                         "notifyWhenChanged": True, **CANVAS_METADATA},
            "nodes": nodes}


# -- system -> graph -------------------------------------------------------------

def to_dataflow(doc: dict, spec: dict | None = None, source: str | None = None) -> dict:
    """A system document as a Pipeline Manager dataflow. IDs derive from names,
    so the same system always gives the same graph. `source`, the file's
    text, rides along so an export can keep its comments (write_system)."""
    spec = spec or specification()
    types = {n["name"]: n for n in spec["nodes"]}
    boards_catalog, firmware = _catalog("board"), _catalog("firmware")
    nodes, connections = [], []

    def node(type_name: str, name: str, values: dict, x: int, y: int,
             shown: tuple[list, list] | None = None) -> dict:
        """`shown`: a board in a role's (interfaces, enabledInterfaceGroups);
        otherwise every interface of the type."""
        t = types[type_name]
        props = [{"id": f"p:{name}:{p['name']}", "name": p["name"],
                  "value": values.get(p["name"], p.get("default"))} for p in t["properties"]]
        ifaces, groups = shown or ([{"name": i["name"], "direction": i["direction"],
                                     **{k: i[k] for k in ("side", "sidePosition") if k in i}}
                                    for i in t["interfaces"]], None)
        # An interface's ID is its pin's: a label never reaches the system.
        n = {"id": f"n:{name}", "name": type_name, "instanceName": name,
             "position": {"x": x, "y": y}, "properties": props,
             "interfaces": [{**i, "id": f"i:{name}:{pin_of(i['name'])}"} for i in ifaces]}
        # A loaded node takes its layout from the graph, not its type
        # (Pipeline Manager v0.5.2 defaults it to 200 px, one column).
        n.update({k: t[k] for k in ("width", "twoColumn") if k in t})
        if groups is not None:
            n["enabledInterfaceGroups"] = groups
        nodes.append(n)
        return n

    def connect(a: str, b: str) -> None:
        connections.append({"id": f"c:{len(connections)}", "from": a, "to": b})

    # Each board's pins the system wires: a pin its role's backplane leaves
    # unconnected shows when wired.
    wired: dict[str, set[str]] = {}
    devices = doc.get("devices", {})
    for endpoint in [*(e for bus in doc.get("buses", {}).values() for e in bus["nodes"]),
                     *(dev[f] for dev in devices.values() for f, _ in _MODEL_HOST_PORTS
                       if f in dev),
                     *(e for dev in devices.values() for e in (dev.get("outputs") or {}).values())]:
        board, pin = endpoint.split(".", 1)
        wired.setdefault(board, set()).add(pin)
    for row, (name, b) in enumerate(doc["boards"].items()):
        values = {"firmware_ref": b.get("firmware_ref", ""),
                  "bootloader_ref": b.get("bootloader_ref", ""),
                  "write_protect": ",".join(str(s) for s in b.get("write_protect", []))}
        board, shown = boards_catalog.get(b["board"]) or {}, None
        if b.get("role") in (board.get("roles") or {}):
            # The role select sets the firmware and what the pins read.
            role = board["roles"][b["role"]]
            values["role"] = role_choice(b["role"], role)
            values["firmware"] = role_firmware(role, firmware)
            shown = role_interfaces(board, b["role"], wired.get(name, ()))
        elif "firmware" in b:
            values["firmware"] = b["firmware"]
        node(b["board"], name, values, 0, BOARD_PITCH * row, shown)
    y = 0
    for name, bus in doc.get("buses", {}).items():
        # One stub per connection, spread along the bus, facing the boards
        # (their CAN connectors are on the right; the bus is to their right).
        size = max(BUS_SIZE, BUS_PITCH * (len(bus["nodes"]) + 1))
        n = node(BUS_NODE, name, {"host_netdev": bus.get("host_netdev", "")},
                 BOARD_WIDTH + 200, y)
        # A bus node is its header and property plus the bus: stack the next
        # one below it, not on top.
        y += size + BUS_NODE_HEIGHT
        stubs = [{"id": f"s:{name}:{k}", "offset": size * (k + 1) // (len(bus["nodes"]) + 1),
                  "side": "left"} for k in range(len(bus["nodes"]))]
        n["interfaces"][0]["bus"] = {"type": "twoSided", "size": size, "stubs": stubs}
        for endpoint, stub in zip(bus["nodes"], stubs):
            board, pin = endpoint.split(".", 1)
            connect(f"i:{board}:{pin}", stub["id"])
    for row, (name, dev) in enumerate(devices.items()):
        values = dict(dev.get("params", {}))
        if "count" in dev:
            values["count"] = dev["count"]
        node(dev["model"], name, values, -560, 200 * row)
        for field, _ in _MODEL_HOST_PORTS:
            if field in dev:
                board, pin = dev[field].split(".", 1)
                connect(f"i:{board}:{pin}", f"i:{name}:{field}")
        for out, endpoint in dev.get("outputs", {}).items():
            board, pin = endpoint.split(".", 1)
            connect(f"i:{name}:{out}", f"i:{board}:{pin}")
    models = _catalog("model")
    for name, dev in devices.items():
        if "attach" in dev:
            port = models[dev["model"]]["interface"]["attach"]
            connect(f"i:{dev['attach']}:{port}", f"i:{name}:{port}")

    extra = {k: doc[k] for k in ("id", "description", "time", "port", "bench") if k in doc}
    if source is not None:
        extra["source"] = source
    return {"version": FORMAT_VERSION, "entryGraph": GRAPH_ID,
            "graphs": [{"id": GRAPH_ID, "name": doc["id"], "nodes": nodes,
                        "connections": connections, "additionalData": {"vhil": extra}}]}


# -- graph -> system -------------------------------------------------------------

def from_dataflow(dataflow: dict, spec: dict | None = None) -> dict:
    """The system document a dataflow describes. Raises SystemError on a graph
    that is not a system (unknown node type, a connection the catalogue has no
    meaning for). Schema and catalogue checks are validate()'s job."""
    spec = spec or specification()
    kinds = {n["name"]: n["additionalData"]["vhil"]["kind"] for n in spec["nodes"]}
    # A board node type's board, and the roles its role select offers.
    placed = {n["name"]: n["additionalData"]["vhil"] for n in spec["nodes"]
              if n["additionalData"]["vhil"]["kind"] == "board"}
    graphs = {g["id"]: g for g in dataflow["graphs"]}
    graph = graphs[dataflow.get("entryGraph") or dataflow["graphs"][0]["id"]]

    by_iface, names = {}, {}
    for n in graph["nodes"]:
        if n["name"] not in kinds:
            raise SystemError(f"node type '{n['name']}' is not in the catalogue")
        name = n.get("instanceName") or n["id"]
        names[n["id"]] = name
        for i in n["interfaces"]:
            # A board interface stands for its pin, whatever its label.
            by_iface[i["id"]] = (n, name, pin_of(i["name"]) if kinds[n["name"]] == "board"
                                 else i["name"])
            # A connection to a bus ends on one of its stubs.
            for stub in (i.get("bus") or {}).get("stubs") or []:
                by_iface[stub["id"]] = (n, name, i["name"])

    extra = dict((graph.get("additionalData") or {}).get("vhil", {}))
    doc = {"kind": "system", "id": extra.pop("id", graph.get("name") or "system")}
    if "description" in extra:
        doc["description"] = extra.pop("description")
    if "time" in extra:
        doc["time"] = extra.pop("time")
    boards, buses, devices = {}, {}, {}
    models = _catalog("model")
    for n in graph["nodes"]:
        name, props = names[n["id"]], {p["name"]: p["value"] for p in n.get("properties", [])}
        kind = kinds[n["name"]]
        if kind == "board":
            # A board with roles is in the one its role select names, which
            # sets the firmware; a board without roles names its own.
            where = placed[n["name"]]
            boards[name] = {"board": where["board"]}
            if where.get("roles"):
                choice = props.get("role")
                boards[name]["role"] = where["roles"].get(choice) or role_of(choice)
            else:
                boards[name]["firmware"] = props["firmware"]
            if str(props.get("firmware_ref") or "").strip():
                boards[name]["firmware_ref"] = str(props["firmware_ref"]).strip()
            if str(props.get("bootloader_ref") or "").strip():
                boards[name]["bootloader_ref"] = str(props["bootloader_ref"]).strip()
            if str(props.get("write_protect", "")).strip():
                boards[name]["write_protect"] = [int(s) for s in str(props["write_protect"]).split(",")]
        elif kind == "bus":
            buses[name] = {"kind": "can", "nodes": []}
            if props.get("host_netdev"):
                buses[name]["host_netdev"] = props["host_netdev"]
        else:
            dev = {"model": n["name"]}
            if props.get("count", 1) != 1:
                dev["count"] = props["count"]
            defaults = models[n["name"]].get("params") or {}
            params = {k: v for k, v in props.items() if k in defaults and v != defaults[k]}
            if params:
                dev["params"] = params
            devices[name] = dev

    for c in graph["connections"]:
        (na, a_name, a_if), (nb, b_name, b_if) = by_iface[c["from"]], by_iface[c["to"]]
        ka, kb = kinds[na["name"]], kinds[nb["name"]]
        if kb == "bus" or ka == "bus":
            (bus, _), (board, pin) = ((b_name, b_if), (a_name, a_if)) if kb == "bus" \
                else ((a_name, a_if), (b_name, b_if))
            buses[bus]["nodes"].append(f"{board}.{pin}")
        elif {ka, kb} == {"board", "model"}:
            (dev, field), (board, pin) = ((b_name, b_if), (a_name, a_if)) if kb == "model" \
                else ((a_name, a_if), (b_name, b_if))
            model = models[devices[dev]["model"]]
            if field in model.get("interface", {}).get("analog_out", []):
                devices[dev].setdefault("outputs", {})[field] = f"{board}.{pin}"
            else:
                devices[dev][field] = f"{board}.{pin}"
        elif ka == kb == "model":
            provider, child = (a_name, b_name) if a_if == models[na["name"]]["interface"].get(
                "provides") else (b_name, a_name)
            devices[child]["attach"] = provider
        else:
            raise SystemError(f"connection {a_name}.{a_if} - {b_name}.{b_if} means nothing")

    doc["boards"] = boards
    if buses:
        doc["buses"] = buses
    if devices:
        doc["devices"] = {k: {f: v[f] for f in _DEVICE_FIELDS if f in v}
                          for k, v in devices.items()}
    if "port" in extra:
        doc["port"] = extra.pop("port")
    if "bench" in extra:
        doc["bench"] = extra.pop("bench")
    return {k: doc[k] for k in _SYSTEM_FIELDS if k in doc}


# -- system files ---------------------------------------------------------------------

def dump_system(doc: dict) -> str:
    """A system document in this repo's style: one flow-style line per board,
    bus and device. Comments are not kept: the editor writes a normalised file."""
    def flow(v) -> str:
        return yaml.safe_dump(v, default_flow_style=True, sort_keys=False, width=1000).strip()

    out = []
    for key in _SYSTEM_FIELDS:
        if key not in doc:
            continue
        value = doc[key]
        if key in ("boards", "buses", "devices"):
            out.append(f"{key}:")
            out += [f"  {name}: {flow(entry)}" for name, entry in value.items()]
        elif key == "description":
            out.append("description: >-")
            out += textwrap.wrap(value, width=76, initial_indent="  ", subsequent_indent="  ")
        else:
            out.append(yaml.safe_dump({key: value}, sort_keys=False, width=78).rstrip())
    return "\n".join(out) + "\n"


def _merge(node, new, depth: int = 0):
    """Apply `new` onto ruamel's round-trip `node` in place, touching only
    what changed, so comments and flow style stay where they were."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    def fresh(v, d):
        # New entries of boards/buses/devices are one flow-style line each.
        if isinstance(v, dict):
            m = CommentedMap((k, fresh(x, d + 1)) for k, x in v.items())
            if d >= 2:
                m.fa.set_flow_style()
            return m
        if isinstance(v, list):
            q = CommentedSeq(fresh(x, d + 1) for x in v)
            if d >= 2:
                q.fa.set_flow_style()
            return q
        return v

    for key in [k for k in node if k not in new]:
        del node[key]
    for key, value in new.items():
        old = node.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            _merge(old, value, depth + 1)
        elif isinstance(old, list) and isinstance(value, list):
            if list(old) != value:
                old[:] = [fresh(x, depth + 2) for x in value]
        elif key not in node or old != value:
            node[key] = fresh(value, depth + 1)


def _source(dataflow: dict) -> str | None:
    """The system file text a dataflow was made from, if it carries one."""
    graphs = {g["id"]: g for g in dataflow["graphs"]}
    graph = graphs[dataflow.get("entryGraph") or dataflow["graphs"][0]["id"]]
    return (graph.get("additionalData") or {}).get("vhil", {}).get("source")


def write_system(doc: dict, source: str | None = None) -> str:
    """The system file for `doc`. Given the file it came from, edits are
    applied onto that text, keeping its comments and layout; otherwise the
    normalised dump_system form."""
    if source is None:
        return dump_system(doc)
    from ruamel.yaml import YAML
    y = YAML()
    y.width, y.preserve_quotes = 1000, True
    y.indent(mapping=2, sequence=4, offset=2)
    root = y.load(source)
    _merge(root, doc)
    out = io.StringIO()
    y.dump(root, out)
    # ruamel drops alignment padding after a key (`can_inv:  {...}`) and keeps
    # a trailing comment's column, so it moves the padding before the `#`: an
    # unchanged line is the original line.
    norm = lambda line: re.sub(r"\s+#", " #", re.sub(r":\s+", ": ", line))
    original = {norm(line): line for line in source.splitlines()}
    return "\n".join(original.get(norm(line), line) for line in out.getvalue().splitlines()) + "\n"


def check_system(doc: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) of a system document: schema and catalogue errors,
    or, for a valid one, what `vhil.system validate` warns of (a pin its
    role's backplane leaves unconnected)."""
    with tempfile.TemporaryDirectory() as tmp:
        # The file is named by its id only once the id is one: an id with a
        # path in it would write outside tmp.
        ident = doc.get("id") if isinstance(doc, dict) else None
        name = ident if isinstance(ident, str) and ID.fullmatch(ident) else "system"
        path = Path(tmp) / f"{name}.yaml"
        path.write_text(dump_system(doc))
        try:
            system = System(path)
        except SystemError as e:
            return [str(e).replace(str(path) + ": ", "")], []
    return [], list(system.warnings)


def validate(doc: dict) -> list[str]:
    """Schema and catalogue errors of a system document ([] if it is valid)."""
    return check_system(doc)[0]


# -- a role change --------------------------------------------------------------------

def switch_role(node: dict, connections: list[dict], spec_type: dict, board: dict,
                firmware: dict) -> tuple[dict | None, list[dict], list[dict]]:
    """A board node, as Pipeline Manager's node_get gives it, shown in the
    role its role select names: (the node to put in its place, or None if it
    shows that role already; the connections to restore; those dropped).

    Each pin keeps its interface ID, so its wires stay. A wire on a pin the
    new role leaves unconnected stays, and the pin shows as "n.c." (validate
    warns of it, as of any such wire); a wire on a pin whose kind changes
    (AMS PF9 is a GPIO, ECU PF9 an analog input) can't, and is dropped."""
    props = {p["name"]: p for p in node.get("properties", [])}
    choice = props["role"]["value"]
    role = spec_type["additionalData"]["vhil"]["roles"].get(choice) or role_of(choice)
    if role not in board["roles"]:
        return None, [], []
    kinds = {i["name"]: i.get("type") for i in
             [*spec_type["interfaces"], *spec_type.get("interfaceGroups", [])]}
    old = {pin_of(i["name"]): i for i in node.get("interfaces", []) if not i.get("hidden")}
    pin_by_id = {i["id"]: pin for pin, i in old.items()}
    new_kind = {pin: kind for pin, _, kind in _readings(board, board["roles"][role])}
    keep, drop = [], []
    for c in connections:
        pin = pin_by_id.get(c["from"]) or pin_by_id.get(c["to"])
        if pin is not None:
            (keep if new_kind.get(pin) == kinds.get(old[pin]["name"]) else drop).append(c)
    wired = {pin_by_id.get(c["from"]) or pin_by_id.get(c["to"]) for c in keep}
    ifaces, groups = role_interfaces(
        board, role, wired, lambda pin: old[pin]["id"] if pin in old else str(uuid.uuid4()))
    fw = role_firmware(board["roles"][role], firmware)
    if (sorted(i["name"] for i in ifaces) == sorted(i["name"] for i in old.values())
            and props.get("firmware", {}).get("value") == fw):
        return None, [], []
    new = {k: v for k, v in node.items()
           if k not in ("interfaces", "enabledInterfaceGroups", "properties")}
    new["properties"] = [{**p, "value": fw} if p["name"] == "firmware" else p
                         for p in node["properties"]]
    new["interfaces"], new["enabledInterfaceGroups"] = ifaces, groups
    # Two columns, wide enough for them (a node keeps a width it was given).
    new["width"] = max(node.get("width") or 0, spec_type.get("width", 0))
    new["twoColumn"] = spec_type.get("twoColumn", False)
    return new, keep, drop


async def _call(client, method: str, params: dict) -> dict:
    reply = await client.request(method, params)
    if reply.get("error"):
        raise RuntimeError(f"{method}: {reply['error'].get('message', reply['error'])}")
    return reply.get("result") or {}


async def relabel(client, spec: dict, graph_id: str, node_id: str, changed: list[dict],
                  catalog: Path = CATALOG) -> str | None:
    """On a property change (properties_on_change): if it is a board's role,
    put the node back as switch_role shows it, through Pipeline Manager's
    frontend API (node_get, graph_get, graph_change), and tell the user what
    happened to its wires. Returns what was done."""
    node = (await _call(client, "node_get", {"graph_id": graph_id, "node_id": node_id}))["node"]
    spec_type = next((t for t in spec["nodes"] if t["name"] == node.get("name")), None)
    if not spec_type or not spec_type["additionalData"]["vhil"].get("roles"):
        return None
    role_prop = next((p for p in node.get("properties", []) if p["name"] == "role"), None)
    if role_prop is None or role_prop["id"] not in {p.get("id") for p in changed}:
        return None
    dataflow = (await _call(client, "graph_get", {}))["dataflow"]
    graph = next(g for g in dataflow["graphs"] if g["id"] == graph_id)
    board = _catalog("board", catalog)[spec_type["additionalData"]["vhil"]["board"]]
    new, _, drop = switch_role(node, graph.get("connections", []), spec_type, board,
                               _catalog("firmware", catalog))
    if new is None:
        return None
    # The graph back with the node replaced: Pipeline Manager can't change a
    # node's interfaces in place, and a node deleted and added again
    # (nodes_change) loses its wires' bus stubs, their IDs and offsets. The
    # graph keeps its view (panning, scaling).
    graph["nodes"] = [new if n["id"] == node_id else n for n in graph["nodes"]]
    graph["connections"] = [c for c in graph.get("connections", []) if c not in drop]
    await _call(client, "graph_change", {"dataflow": dataflow, "loadingScreen": False})
    name = node.get("instanceName") or node["name"]
    role = role_of(role_prop["value"])
    unrouted = sorted(pin_of(i["name"]) for i in new["interfaces"]
                      if i["name"].endswith(ROLE_SEP + NOT_CONNECTED))
    lines = [f"{name} is now in the {role} role ({role_prop['value']})."]
    if unrouted:
        lines.append(f"Kept the wires on {', '.join(unrouted)}, which the {role} backplane "
                     f"leaves unconnected: validate warns of them.")
    if drop:
        lines.append(f"Removed {len(drop)} wire(s) on pins that change kind in the {role} "
                     f"role: " + ", ".join(sorted({pin_of(i['name']) for i in node['interfaces']
                                                   if i['id'] in {c['from'] for c in drop}
                                                   | {c['to'] for c in drop}})) + ".")
    await client.notify("notification_send", {
        "type": "warning" if drop or unrouted else "info",
        "title": f"{name}: {role} role", "details": " ".join(lines)})
    return " ".join(lines)


# -- JSON-RPC methods (Pipeline Manager external app) -------------------------------

OK, ERROR, PROGRESS, WARNING = 0, 1, 2, 3


class EditorMethods:
    """Methods Pipeline Manager calls on its external application. Names and
    parameters follow its external-app API; each returns {type, content}."""

    def __init__(self, run_ms: int = 2000):
        self.run_ms = run_ms
        self.spec = specification()

    def specification_get(self, **_):
        return {"type": OK, "content": self.spec}

    def app_capabilities_get(self, **_):
        # Navbar buttons (common_types navbar_items): returned bare, not wrapped.
        return [{"name": "Validate system", "iconName": "Validate",
                 "procedureName": "dataflow_validate"},
                {"name": "Run 2 s of virtual time", "iconName": "Run",
                 "procedureName": "dataflow_run"}]

    def frontend_on_connect(self, **_):
        return {}   # null_or_empty

    # What the frontend tells of each edit (the specification's
    # notifyWhenChanged): nothing to do but for a role change, which the
    # served backend handles (_ServedMethods.properties_on_change).
    def _changed(self, **_):
        return {}   # null_or_empty

    properties_on_change = interfaces_on_change = name_on_change = position_on_change = \
        nodes_on_change = connections_on_change = graph_on_change = specification_on_change = \
        metadata_on_change = viewport_on_center = nodes_on_highlight = _changed

    def dataflow_validate(self, dataflow, **_):
        warnings = []
        try:
            errors, warnings = check_system(from_dataflow(dataflow, self.spec))
        except (SystemError, KeyError) as e:
            errors = [str(e)]
        if errors:
            return {"type": ERROR, "content": "; ".join(errors)}
        if warnings:
            return {"type": WARNING, "content": "valid system, with warnings: " + "; ".join(warnings)}
        return {"type": OK, "content": "valid system"}

    def dataflow_export(self, dataflow, **_):
        try:
            doc = from_dataflow(dataflow, self.spec)
        except (SystemError, KeyError) as e:
            return {"type": ERROR, "content": str(e)}
        source = _source(dataflow)
        text = write_system(doc, source)
        # content is a JSON object or a base64 string; a YAML file is the latter.
        return {"type": OK, "content": b64.b64encode(text.encode()).decode(),
                "filename": f"{doc['id']}.yaml"}

    def dataflow_import(self, external_application_dataflow, mime="", base64=False, **_):
        text = external_application_dataflow
        if base64:
            text = b64.b64decode(text).decode()
        try:
            doc = yaml.safe_load(text)
            errors = validate(doc)
            if errors:
                return {"type": ERROR, "content": "; ".join(errors)}
            return {"type": OK, "content": to_dataflow(doc, self.spec, source=text)}
        except (yaml.YAMLError, SystemError, KeyError, TypeError) as e:
            return {"type": ERROR, "content": str(e)}

    def dataflow_run(self, dataflow, **_):
        """Run the system for run_ms of virtual time; report each bus's traffic.
        Images come from $VHIL_<FIRMWARE>_ELF (e.g. VHIL_AMS_ELF)."""
        from vhil.sim import Sim
        try:
            doc = from_dataflow(dataflow, self.spec)
            errors = validate(doc)
            if errors:
                return {"type": ERROR, "content": "; ".join(errors)}
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{doc['id']}.yaml"
                path.write_text(dump_system(doc))
                # Each board's firmware: its role's, or the one it names.
                firmware = {}
                for board, b in System(path).boards.items():
                    var = f"VHIL_{b.firmware['id'].upper().replace('-', '_')}_ELF"
                    elf = os.environ.get(var)
                    if not elf:
                        return {"type": ERROR, "content": f"no image for {board}: set {var}"}
                    firmware[board] = elf
                with Sim(path, firmware) as sim:
                    sim.run_for(ms=self.run_ms)
                    lines = []
                    for bus in doc.get("buses", {}):
                        frames = sim.can(bus).frames()
                        ids = sorted({f.id for f in frames})
                        lines.append(f"{bus}: {len(frames)} frames, ids "
                                     + " ".join(f"0x{i:X}" for i in ids))
            return {"type": OK, "content": f"{self.run_ms} ms of virtual time\n" + "\n".join(lines)}
        except Exception as e:  # report, don't kill the backend
            return {"type": ERROR, "content": f"{type(e).__name__}: {e}"}


class _Logged:
    """Logs every call the editor makes and what it got back."""

    def __init__(self, methods: EditorMethods):
        self._methods = methods

    def __dir__(self):
        return [n for n in dir(self._methods) if not n.startswith("_")]

    def __getattribute__(self, name):
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        fn = getattr(object.__getattribute__(self, "_methods"), name)
        if not callable(fn):
            return fn

        async def call(**kwargs):
            try:
                result = fn(**kwargs)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception:
                logging.exception("%s(%s) raised", name, ", ".join(kwargs))
                raise
            if name.endswith(("_on_change", "_on_center", "_on_highlight")):
                return result   # every drag and keystroke: not worth a line
            summary = result.get("type") if isinstance(result, dict) else type(result).__name__
            logging.info("%s(%s) -> %s", name, ", ".join(kwargs), summary)
            if isinstance(result, dict) and result.get("type") == ERROR:
                logging.info("  %s", result.get("content"))
            return result
        return call


class _ServedMethods(EditorMethods):
    """EditorMethods with a run that doesn't block the connection and shows
    its result: Pipeline Manager only displays progress for a plain OK."""

    client = None

    async def dataflow_run(self, dataflow, **kwargs):
        import asyncio
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: EditorMethods.dataflow_run(self, dataflow, **kwargs))
        ok = result["type"] == OK
        text = str(result.get("content", ""))
        await self.client.notify("terminal_write", {
            "name": "Terminal", "message": text.replace("\n", "\r\n") + "\r\n"})
        await self.client.notify("notification_send", {
            "type": "info" if ok else "error",
            "title": "Run finished" if ok else "Run failed", "details": text})
        return result

    async def properties_on_change(self, graph_id, node_id, properties, **_):
        """A board's role changed: relabel it (relabel()). It asks the
        frontend for the node and graph, so it runs after this returns."""
        import asyncio

        async def run():
            try:
                done = await relabel(self.client, self.spec, graph_id, node_id, properties)
                if done:
                    logging.info("role change: %s", done)
            except Exception as e:  # report, don't kill the backend
                logging.exception("role change of %s failed", node_id)
                await self.client.notify("notification_send", {
                    "type": "error", "title": "Role change failed",
                    "details": f"{type(e).__name__}: {e}"})

        self._tasks = getattr(self, "_tasks", set())
        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)    # keep a reference until it is done
        task.add_done_callback(self._tasks.discard)
        return {}


async def _serve(host: str, port: int) -> None:
    from pipeline_manager_backend_communication.communication_backend import CommunicationBackend
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    client = CommunicationBackend(host, port)
    methods = _ServedMethods()
    methods.client = client
    await client.initialize_client(_Logged(methods))
    await client.start_json_rpc_client()


# -- CLI ------------------------------------------------------------------------------

def check(systems: list[Path], pm_dir: Path, pm_python: str = "python",
          node_dir: Path | None = None) -> int:
    """Pipeline Manager's own validator (`./validate <spec> <dataflow>...` in
    its checkout) on the specification and each system's dataflow. It loads
    them through the frontend's code, so it catches what a schema check
    can't: a connection that ends on a stub the loaded graph lost. Returns
    its exit status (0: all valid)."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        spec = specification()
        spec_path = Path(tmp) / "spec.json"
        spec_path.write_text(json.dumps(spec))
        flows = []
        for system in systems:
            source = system.read_text()
            flows.append(Path(tmp) / f"{system.stem}.json")
            flows[-1].write_text(json.dumps(to_dataflow(yaml.safe_load(source), spec, source)))
        env = dict(os.environ)
        if node_dir:
            env["PATH"] = f"{node_dir / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run([pm_python, "./validate", str(spec_path), *map(str, flows)],
                              cwd=pm_dir, env=env).returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vhil.editor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("spec", help="node types from the catalogue")
    p.add_argument("-o", "--output")
    p = sub.add_parser("to-graph", help="system file -> dataflow")
    p.add_argument("system")
    p.add_argument("-o", "--output")
    p = sub.add_parser("to-system", help="dataflow -> system file")
    p.add_argument("dataflow")
    p.add_argument("-o", "--output")
    p = sub.add_parser("serve", help="JSON-RPC backend for Pipeline Manager")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p = sub.add_parser("check", help="Pipeline Manager's ./validate on every system's dataflow")
    p.add_argument("systems", nargs="*", type=Path,
                   help="system files (default: systems/*.yaml)")
    args = ap.parse_args(argv)

    if args.cmd == "check":
        # The editor image sets these (docker/editor.Dockerfile); natively,
        # the defaults scripts/editor.sh uses.
        home = Path.home() / "vhil-tools"
        pm_dir = Path(os.environ.get("PM_DIR", home / "kenning-pipeline-manager"))
        pm_venv = Path(os.environ.get("PM_VENV", home / "pm-venv"))
        node_dir = Path(os.environ.get("NODE_DIR", home / "node"))
        systems = args.systems or sorted((REPO / "systems").glob("*.yaml"))
        return check(systems, pm_dir, str(pm_venv / "bin" / "python"), node_dir)
    if args.cmd == "serve":
        import asyncio
        asyncio.run(_serve(args.host, args.port))
        return 0
    if args.cmd == "spec":
        text = json.dumps(specification(), indent=2)
    elif args.cmd == "to-graph":
        source = Path(args.system).read_text()
        text = json.dumps(to_dataflow(yaml.safe_load(source), source=source), indent=2)
    else:
        dataflow = json.loads(Path(args.dataflow).read_text())
        source = _source(dataflow)
        text = write_system(from_dataflow(dataflow), source)
    if args.output:
        Path(args.output).write_text(text + ("" if text.endswith("\n") else "\n"))
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
