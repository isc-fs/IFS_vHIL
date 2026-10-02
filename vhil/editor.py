"""The system editor's backend: Antmicro's Pipeline Manager as the UI (M5, #13).

    python -m vhil.editor spec -o build/spec.json           # node types from catalog/
    python -m vhil.editor to-graph systems/ams.yaml -o build/ams.json
    python -m vhil.editor to-system build/ams.json -o build/ams.yaml
    python -m vhil.editor serve [--port 9000]               # JSON-RPC backend

The system file stays the source of truth (vision principle 1): the editor's
graph is a view of it, translated both ways here. Catalogue entries become
node types:

  board     a node with one typed connector per CAN/SPI/SDMMC/GPIO/analog pin;
            its firmware is a select property
  CAN bus   a node with a BUS interface: connect any number of CAN connectors
  model     a node per chip model: its host-side ports (spi, cs, sdmmc), the
            port it provides (an LTC6820 provides `isospi`) and the port it
            attaches to (an LTC6811 attaches to `isospi`), with count/params

What is not a graph (id, description, time, bench wiring) travels in the
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
from pathlib import Path

import yaml

from vhil.system import CATALOG, System, SystemError

# Pipeline Manager's specification/dataflow format version these were written
# against (the docs' examples).
FORMAT_VERSION = "20250623.14"

BUS_NODE = "CAN bus"
GRAPH_ID = "system"
# A bus interface's length in pixels: each connection lands on a stub along
# it. Pipeline Manager places a new stub at size / 2, so a tiny bus stacks
# every connection on the node header.
BUS_SIZE, BUS_PITCH = 120, 40

# Board catalogue section -> interface type and side.
_BOARD_PORTS = (("can", "can", "right"), ("spi", "spi", "left"), ("sdmmc", "sdmmc", "left"),
                ("gpio", "gpio", "left"), ("analog_in", "analog", "left"))
# Model host-side ports: system device field -> interface type.
_MODEL_HOST_PORTS = (("spi", "spi"), ("cs", "gpio"), ("sdmmc", "sdmmc"))
# Field order in a written system file.
_DEVICE_FIELDS = ("model", "spi", "cs", "sdmmc", "attach", "count", "params")
_SYSTEM_FIELDS = ("kind", "id", "description", "time", "boards", "buses", "devices", "bench")


def _catalog(kind: str, catalog: Path = CATALOG) -> dict[str, dict]:
    folder = {"board": "boards", "firmware": "firmware", "model": "models"}[kind]
    docs = (yaml.safe_load(p.read_text()) for p in sorted((catalog / folder).glob("*.yaml")))
    return {d["id"]: d for d in docs}


# -- specification -------------------------------------------------------------

def _property(name: str, value) -> dict:
    if isinstance(value, bool):
        return {"name": name, "type": "bool", "default": value}
    if isinstance(value, int):
        return {"name": name, "type": "integer", "default": value}
    if isinstance(value, float):
        return {"name": name, "type": "number", "default": value}
    return {"name": name, "type": "text", "default": str(value)}


def specification(catalog: Path = CATALOG) -> dict:
    """Pipeline Manager node types for everything in the catalogue."""
    firmware = sorted(_catalog("firmware", catalog))
    nodes = []
    for board_id, board in _catalog("board", catalog).items():
        interfaces = [{"name": pin, "type": itype, "direction": "inout", "side": side,
                       "maxConnectionsCount": 1}
                      for section, itype, side in _BOARD_PORTS
                      for pin in board.get(section, {})]
        nodes.append({
            "name": board_id, "category": "Boards", "layer": "board",
            "description": board.get("description", ""),
            "interfaces": interfaces,
            "properties": [{"name": "firmware", "type": "select", "values": firmware,
                            "default": firmware[0]}],
            "additionalData": {"vhil": {"kind": "board"}},
        })
    nodes.append({
        "name": BUS_NODE, "category": "Buses", "layer": "bus",
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
        properties = [_property(k, v) for k, v in (model.get("params") or {}).items()]
        if iface.get("attach"):
            properties.insert(0, {"name": "count", "type": "integer", "default": 1, "min": 1,
                                  "description": "Identical chips in a row on the port."})
        nodes.append({
            "name": model_id, "category": "Models", "layer": "model",
            "description": model.get("description", ""),
            "interfaces": interfaces, "properties": properties,
            "additionalData": {"vhil": {"kind": "model"}},
        })
    return {"version": FORMAT_VERSION,
            "metadata": {"connectionStyle": "orthogonal", "twoColumn": True},
            "nodes": nodes}


# -- system -> graph -------------------------------------------------------------

def to_dataflow(doc: dict, spec: dict | None = None, source: str | None = None) -> dict:
    """A system document as a Pipeline Manager dataflow. IDs derive from names,
    so the same system always gives the same graph. `source`, the file's
    text, rides along so an export can keep its comments (write_system)."""
    spec = spec or specification()
    types = {n["name"]: n for n in spec["nodes"]}
    nodes, connections = [], []

    def node(type_name: str, name: str, values: dict, x: int, y: int) -> dict:
        t = types[type_name]
        props = [{"id": f"p:{name}:{p['name']}", "name": p["name"],
                  "value": values.get(p["name"], p.get("default"))} for p in t["properties"]]
        ifaces = [{"id": f"i:{name}:{i['name']}", "name": i["name"], "direction": i["direction"],
                   **({"side": i["side"]} if "side" in i else {})}
                  for i in t["interfaces"]]
        n = {"id": f"n:{name}", "name": type_name, "instanceName": name,
             "position": {"x": x, "y": y}, "properties": props, "interfaces": ifaces}
        nodes.append(n)
        return n

    def connect(a: str, b: str) -> None:
        connections.append({"id": f"c:{len(connections)}", "from": a, "to": b})

    for row, (name, b) in enumerate(doc["boards"].items()):
        node(b["board"], name, {"firmware": b["firmware"]}, 0, 420 * row)
    for row, (name, bus) in enumerate(doc.get("buses", {}).items()):
        n = node(BUS_NODE, name, {"host_netdev": bus.get("host_netdev", "")}, 520, 160 * row)
        # One stub per connection, spread along the bus, facing the boards
        # (their CAN connectors are on the right; the bus is to their right).
        size = max(BUS_SIZE, BUS_PITCH * (len(bus["nodes"]) + 1))
        stubs = [{"id": f"s:{name}:{k}", "offset": size * (k + 1) // (len(bus["nodes"]) + 1),
                  "side": "left"} for k in range(len(bus["nodes"]))]
        n["interfaces"][0]["bus"] = {"type": "twoSided", "size": size, "stubs": stubs}
        for endpoint, stub in zip(bus["nodes"], stubs):
            board, pin = endpoint.split(".", 1)
            connect(f"i:{board}:{pin}", stub["id"])
    devices = doc.get("devices", {})
    for row, (name, dev) in enumerate(devices.items()):
        values = dict(dev.get("params", {}))
        if "count" in dev:
            values["count"] = dev["count"]
        node(dev["model"], name, values, -560, 200 * row)
        for field, _ in _MODEL_HOST_PORTS:
            if field in dev:
                board, pin = dev[field].split(".", 1)
                connect(f"i:{board}:{pin}", f"i:{name}:{field}")
    models = _catalog("model")
    for name, dev in devices.items():
        if "attach" in dev:
            port = models[dev["model"]]["interface"]["attach"]
            connect(f"i:{dev['attach']}:{port}", f"i:{name}:{port}")

    extra = {k: doc[k] for k in ("id", "description", "time", "bench") if k in doc}
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
    graphs = {g["id"]: g for g in dataflow["graphs"]}
    graph = graphs[dataflow.get("entryGraph") or dataflow["graphs"][0]["id"]]

    by_iface, names = {}, {}
    for n in graph["nodes"]:
        if n["name"] not in kinds:
            raise SystemError(f"node type '{n['name']}' is not in the catalogue")
        name = n.get("instanceName") or n["id"]
        names[n["id"]] = name
        for i in n["interfaces"]:
            by_iface[i["id"]] = (n, name, i["name"])
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
            boards[name] = {"board": n["name"], "firmware": props["firmware"]}
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
    # ruamel drops alignment padding after a key (`can_inv:  {...}`): an
    # unchanged line is the original line.
    norm = lambda line: re.sub(r":\s+", ": ", line)
    original = {norm(line): line for line in source.splitlines()}
    return "\n".join(original.get(norm(line), line) for line in out.getvalue().splitlines()) + "\n"


def validate(doc: dict) -> list[str]:
    """Schema and catalogue errors of a system document ([] if it is valid)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{doc.get('id', 'system')}.yaml"
        path.write_text(dump_system(doc))
        try:
            System(path)
        except SystemError as e:
            return [str(e).replace(str(path) + ": ", "")]
    return []


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

    def dataflow_validate(self, dataflow, **_):
        try:
            errors = validate(from_dataflow(dataflow, self.spec))
        except (SystemError, KeyError) as e:
            errors = [str(e)]
        if errors:
            return {"type": ERROR, "content": "; ".join(errors)}
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
            firmware = {}
            for board, b in doc["boards"].items():
                elf = os.environ.get(f"VHIL_{b['firmware'].upper()}_ELF")
                if not elf:
                    return {"type": ERROR, "content": f"no image for {board}: set "
                                                      f"VHIL_{b['firmware'].upper()}_ELF"}
                firmware[board] = elf
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{doc['id']}.yaml"
                path.write_text(dump_system(doc))
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


async def _serve(host: str, port: int) -> None:
    from pipeline_manager_backend_communication.communication_backend import CommunicationBackend
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    client = CommunicationBackend(host, port)
    methods = _ServedMethods()
    methods.client = client
    await client.initialize_client(_Logged(methods))
    await client.start_json_rpc_client()


# -- CLI ------------------------------------------------------------------------------

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
    args = ap.parse_args(argv)

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
