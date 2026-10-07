"""Host-only checks of the editor backend: catalogue -> node types, and system
files <-> Pipeline Manager graphs."""
import asyncio
import base64
import copy
import os
from pathlib import Path

import pytest
import yaml

from vhil.editor import (BOARD_WIDTH, BUS_NODE, ERROR, OK, WARNING, EditorMethods,
                         check_system, dump_system, from_dataflow, pin_of, relabel,
                         role_choice, role_interfaces, specification, switch_role,
                         to_dataflow, validate, write_system)
from vhil.editor import main as editor_main
from vhil.system import REPO

SYSTEMS = sorted((REPO / "systems").glob("*.yaml"))
BOARD = yaml.safe_load((REPO / "catalog" / "boards" / "mainlite.yaml").read_text())
FIRMWARE = {p.stem for p in (REPO / "catalog" / "firmware").glob("*.yaml")}


@pytest.fixture(scope="module")
def spec():
    return specification()


def _types(spec):
    return {n["name"]: n for n in spec["nodes"]}


def _kinds(spec):
    """Every way a MainLite pin can read, with its interface type."""
    t = _types(spec)["mainlite"]
    return {i["name"]: i["type"] for i in t["interfaces"] + t["interfaceGroups"]}


def _ifaces(spec, role, wired=()):
    """What a MainLite node in `role` shows: {interface name: type}."""
    kinds = _kinds(spec)
    return {i["name"]: kinds[i["name"]] for i in role_interfaces(BOARD, role, wired)[0]}


def _node(graph, name):
    return next(n for n in graph["graphs"][0]["nodes"] if n.get("instanceName") == name)


def test_spec_has_a_node_type_per_catalogue_entry(spec):
    """A model is a node type, and so is a board: the MainLite is one, whose
    role is a property."""
    names = set(_types(spec))
    for path in (REPO / "catalog" / "models").glob("*.yaml"):
        assert yaml.safe_load(path.read_text())["id"] in names
    for path in (REPO / "catalog" / "boards").glob("*.yaml"):
        assert yaml.safe_load(path.read_text())["id"] in names
    assert not [n for n in names if n.startswith("mainlite ")]
    assert BUS_NODE in names


def test_the_role_is_a_select_showing_the_node_id(spec):
    props = {p["name"]: p for p in _types(spec)["mainlite"]["properties"]}
    assert props["role"]["type"] == "select"
    assert props["role"]["values"] == ["ecu (node 0x1)", "ams (node 0x2)", "udv (node 0x3)"]
    assert props["role"]["default"] == "ecu (node 0x1)"
    assert _types(spec)["mainlite"]["additionalData"]["vhil"]["roles"] == {
        "ecu (node 0x1)": "ecu", "ams (node 0x2)": "ams", "udv (node 0x3)": "udv"}


def test_the_node_shapes_read_the_role_and_the_bitrate(spec):
    """A board's sub-line reads its role's node ID and flash bus ("node 0x2
    · FDCAN1"), and a bus's rail its bitrate: both from the specification
    (src/vhil/shapes.js in the vendored Pipeline Manager)."""
    info = _types(spec)["mainlite"]["additionalData"]["vhil"]["role_info"]
    assert info == {name: {"node_id": r["node_id"], "flash_bus": r["flash_bus"]}
                    for name, r in BOARD["roles"].items()}
    assert info["ams"] == {"node_id": 2, "flash_bus": "FDCAN1"}
    assert _types(spec)[BUS_NODE]["additionalData"]["vhil"] == {"kind": "bus", "bitrate": 500_000}


def test_board_connectors_are_typed(spec):
    board = _ifaces(spec, "ams")
    assert board["FDCAN1 · CAN_ACU"] == "can" and board["SPI1 · LTC6820"] == "spi"
    assert board["PB9 · LTC6820_CS"] == "gpio" and board["PF7 · S_CURRENT_P"] == "analog"
    bus = _types(spec)[BUS_NODE]["interfaces"][0]
    assert bus["type"] == "can" and "bus" in bus


@pytest.mark.parametrize("role", ["ecu", "ams", "udv"])
def test_pins_read_with_their_car_signal_in_each_role(spec, role):
    """In a role, a node shows the pins its backplane routes, each reading
    "PF8 · APPS_1", and the MainLite's own SDMMC1/I2C2. The label is display
    only: the interface is still the pin."""
    from .test_system import PIN_LABELS
    names = list(_ifaces(spec, role))
    want = {**PIN_LABELS[role], "SDMMC1": "microSD", "I2C2": "BMI088"}
    assert {pin_of(n): n.split(" · ")[1] for n in names} == want


def test_every_pin_has_a_reading_in_the_node_type(spec):
    """Every connector and pin of the MainLite is in its node type, in each
    reading some role gives it; a pin that reads differently by role is
    hidden (Pipeline Manager draws its readings, interface groups made of
    it, never the pin)."""
    t = _types(spec)["mainlite"]
    every = {c for s in ("can", "spi", "uart", "sdmmc", "i2c", "gpio", "analog_in")
             for c in BOARD[s]}
    assert {pin_of(n) for n in _kinds(spec)} == every
    for g in t["interfaceGroups"]:
        assert g["interfaces"] == [{"name": pin_of(g["name"]), "direction": "inout"}]
    hidden = {i["name"] for i in t["interfaces"] if " · " not in i["name"]}
    assert hidden == {pin_of(g["name"]) for g in t["interfaceGroups"]}
    assert {i["name"] for i in t["interfaces"] if " · " in i["name"]} == {
        "SDMMC1 · microSD", "I2C2 · BMI088"}


def test_unrouted_pins_show_only_when_wired(spec):
    """What a role's backplane leaves unconnected reads "n.c.": offered (an
    interface group), hidden, and shown last when a system wires it."""
    kinds = _kinds(spec)
    for name in ("FDCAN2 · n.c.", "FDCAN3 · n.c.", "SPI1 · n.c.", "USART10 · n.c."):
        assert name in kinds
    assert not [n for n in _ifaces(spec, "ams") if n.endswith("n.c.")]
    assert list(_ifaces(spec, "ams", {"FDCAN3"}))[-1] == "FDCAN3 · n.c."
    assert "SPI1 · n.c." in _ifaces(spec, "udv", {"SPI1"})
    assert not [n for n in _ifaces(spec, "ecu", {"FDCAN3"}) if n.endswith("n.c.")]


def test_a_role_rekinds_its_pins_in_the_editor(spec):
    """AMS PF9 is TSMS and PF10 DASH_CHG, digital inputs; uDV PC1 and PC2_C
    debug-LED outputs: GPIO interfaces there, analog on the ECU."""
    assert _ifaces(spec, "ams")["PF9 · TSMS"] == "gpio"
    assert _ifaces(spec, "ams")["PF10 · RST_PIL"] == "gpio"
    assert _ifaces(spec, "udv")["PC1 · DEBUG_LED2"] == "gpio"
    assert _ifaces(spec, "udv")["PC2_C · DEBUG_LED3"] == "gpio"
    assert _ifaces(spec, "ecu")["PF9 · APPS_2"] == "analog"
    assert _ifaces(spec, "ecu")["PC1 · SPARE_J3"] == "analog"
    assert _ifaces(spec, "ecu")["PF10 · SPARE_J3"] == "analog"
    assert _ifaces(spec, "ecu")["USART10 · GPS"] == "uart"


@pytest.mark.parametrize("role", ["ecu", "ams", "udv"])
def test_a_node_is_two_columns_with_no_empty_rows(spec, role):
    """Pins in two columns, about as many on each side (left: what devices
    attach to; right: CAN and digital lines), numbered from the top with no
    gap, on a node wide enough for a label on each side."""
    ifaces = role_interfaces(BOARD, role)[0]
    kinds = _kinds(spec)
    sides = {s: sorted(i["sidePosition"] for i in ifaces if i["side"] == s)
             for s in ("left", "right")}
    assert sides["left"] == list(range(len(sides["left"])))
    assert sides["right"] == list(range(len(sides["right"])))
    assert 0.5 <= len(sides["left"]) / len(sides["right"]) <= 2
    for i in ifaces:
        want = "right" if kinds[i["name"]] in ("can", "gpio") else "left"
        assert i["side"] == want, i["name"]
    t = _types(spec)["mainlite"]
    assert t["width"] == BOARD_WIDTH >= 400 and t["twoColumn"] is True
    # A loaded node takes its layout from the graph (Pipeline Manager
    # defaults a node to 200 px, one column), so each board node carries it.
    for path in SYSTEMS:
        for n in to_dataflow(yaml.safe_load(path.read_text()), spec)["graphs"][0]["nodes"]:
            if n["name"] == "mainlite":
                assert n["width"] == BOARD_WIDTH and n["twoColumn"] is True


def test_a_new_node_shows_the_first_role(spec):
    """A node dragged from the palette is an ECU: its role, firmware and
    interface groups, in rows 0..n-1 of the type's numbering."""
    t = _types(spec)["mainlite"]
    shown, groups = role_interfaces(BOARD, "ecu")
    assert t["defaultInterfaceGroups"] == groups
    rows = {(i["side"], i["sidePosition"]) for i in t["interfaces"] + t["interfaceGroups"]
            if "side" in i}
    assert len(rows) == len([i for i in t["interfaces"] + t["interfaceGroups"] if "side" in i])
    typed = {i["name"]: i for i in t["interfaces"] + t["interfaceGroups"]}
    for i in shown:
        assert typed[i["name"]]["sidePosition"] == i["sidePosition"], i["name"]


def test_the_role_sets_firmware_and_bootloader(spec):
    """The role sets the firmware (read-only); the bootloader every MainLite
    carries is read-only too. Only refs are free."""
    props = {p["name"]: p for p in _types(spec)["mainlite"]["properties"]}
    assert props["firmware"]["type"] == "constant" and props["firmware"]["default"] == "ecu"
    assert props["bootloader"] == {**props["bootloader"], "type": "constant",
                                   "default": "can-bootloader"}
    assert {"firmware_ref", "bootloader_ref"} <= set(props) and "node_id" not in props
    for path, name, fw in ((SYSTEMS[0], "ams", "ams"), (REPO / "systems" / "ecu.yaml", "ecu", "ecu")):
        node = _node(to_dataflow(yaml.safe_load(path.read_text()), spec), name)
        values = {p["name"]: p["value"] for p in node["properties"]}
        assert values["firmware"] == fw and values["role"].startswith(f"{fw} (node ")


def test_a_role_without_catalogue_firmware_carries_a_note(spec):
    assert "udv" not in FIRMWARE
    assert "| udv | 0x3 | FDCAN2 | udv (not in the catalogue yet) |" in \
        _types(spec)["mainlite"]["description"]


def test_write_protect_is_not_on_the_node(spec):
    """write_protect is test-only option-byte state: a hidden property, so a
    system that has it keeps it, and the node doesn't show it."""
    props = {p["name"]: p for p in _types(spec)["mainlite"]["properties"]}
    assert props["write_protect"]["hidden"] is True


PM_METADATA_SCHEMA = Path(os.environ.get("PM_DIR", "/opt/pm")) / "pipeline_manager" / \
    "resources" / "schemas" / "metadata_schema.json"


def test_every_interface_type_and_category_is_styled(spec):
    """A wire with no entry would fall back to Pipeline Manager's white,
    and a node type with no style to its default header."""
    meta = spec["metadata"]
    types = {i["type"] for n in spec["nodes"] for i in n["interfaces"] + n.get("interfaceGroups", [])
             if "type" in i}
    assert types <= set(meta["interfaces"]), types - set(meta["interfaces"])
    for n in spec["nodes"]:
        assert n["style"] == n["category"] and n["style"] in meta["styles"], n["name"]
    assert meta["welcome"] is False and meta["newNodeType"] is False


@pytest.mark.skipif(not PM_METADATA_SCHEMA.exists(),
                    reason="Pipeline Manager not installed (the ifs-vhil-editor image has it)")
def test_the_metadata_is_what_pipeline_manager_takes(spec):
    """Every key against the pinned release's own schema: an unknown key
    fails its load."""
    import json

    import jsonschema
    schema = json.loads(PM_METADATA_SCHEMA.read_text())
    jsonschema.validate(spec["metadata"], schema)


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_write_protect_survives_the_round_trip(spec, path):
    doc = yaml.safe_load(path.read_text())
    for b in doc["boards"].values():
        b["write_protect"] = [0, 3]
    assert from_dataflow(to_dataflow(doc, spec), spec) == doc


@pytest.mark.parametrize("role", ["ecu", "ams"])
def test_a_role_chosen_in_the_graph_is_saved(spec, role):
    """The role select is the role: a node set to another saves in it, with
    no firmware field."""
    doc = yaml.safe_load((REPO / "systems" / "ams.yaml").read_text())
    graph = to_dataflow(doc, spec)
    node = _node(graph, "ams")
    assert node["name"] == "mainlite"
    next(p for p in node["properties"] if p["name"] == "role")["value"] = \
        role_choice(role, BOARD["roles"][role])
    assert from_dataflow(graph, spec)["boards"]["ams"] == {"board": "mainlite", "role": role}


# -- a role change -----------------------------------------------------------------------

def _set_role(node, role):
    node = copy.deepcopy(node)
    next(p for p in node["properties"] if p["name"] == "role")["value"] = \
        role_choice(role, BOARD["roles"][role])
    return node


def test_a_role_change_relabels_the_pins_and_keeps_their_wires(spec):
    """AMS -> ECU: every wire of systems/ams.yaml lands on a pin the ECU
    routes, of the same kind; each keeps its interface ID, so its wire, and
    the pins read as the ECU's."""
    graph = to_dataflow(yaml.safe_load((REPO / "systems" / "ams.yaml").read_text()), spec)
    g = graph["graphs"][0]
    node = _set_role(_node(graph, "ams"), "ecu")
    new, keep, drop = switch_role(node, g["connections"], _types(spec)["mainlite"], BOARD,
                                  FIRMWARE)
    assert drop == [] and keep == [c for c in g["connections"]
                                   if c["from"].startswith("i:ams:") or c["to"].startswith("i:ams:")]
    names = {i["id"]: i["name"] for i in new["interfaces"]}
    assert names["i:ams:FDCAN1"] == "FDCAN1 · CAN_INV" and names["i:ams:PF8"] == "PF8 · APPS_1"
    assert [i["name"] for i in new["interfaces"]] == [
        i["name"] for i in role_interfaces(BOARD, "ecu")[0]]
    assert new["enabledInterfaceGroups"] == role_interfaces(BOARD, "ecu")[1]
    values = {p["name"]: p["value"] for p in new["properties"]}
    assert values["firmware"] == "ecu" and values["role"] == "ecu (node 0x1)"
    assert new["width"] == BOARD_WIDTH and new["twoColumn"] is True
    assert {k: new[k] for k in ("id", "name", "instanceName", "position")} == \
        {k: node[k] for k in ("id", "name", "instanceName", "position")}


def test_a_role_change_keeps_unrouted_wires_and_drops_rekinded_ones(spec):
    """AMS -> uDV: the uDV routes neither SPI1 nor FDCAN1's ACU bus the same
    way. SPI1 (the LTC6820) stays wired, as "SPI1 · n.c.", which validate
    warns of; PC1 becomes a debug-LED GPIO, so the DC-DC current sensor's
    analog wire to it can't stay."""
    graph = to_dataflow(yaml.safe_load((REPO / "systems" / "ams.yaml").read_text()), spec)
    g = graph["graphs"][0]
    new, keep, drop = switch_role(_set_role(_node(graph, "ams"), "udv"), g["connections"],
                                  _types(spec)["mainlite"], BOARD, FIRMWARE)
    assert drop == [c for c in g["connections"] if c["to"] == "i:ams:PC1"]
    names = [i["name"] for i in new["interfaces"]]
    assert "SPI1 · n.c." in names and "PC1 · DEBUG_LED2" in names
    assert names[-1] == "SPI1 · n.c."
    # Put in the graph as the frontend would: the system it saves is the uDV,
    # with every wire but the dropped one, and validate warns of SPI1.
    g["nodes"] = [new if n["id"] == new["id"] else n for n in g["nodes"]]
    g["connections"] = [c for c in g["connections"] if c not in drop]
    doc = from_dataflow(graph, spec)
    assert doc["boards"]["ams"] == {"board": "mainlite", "role": "udv"}
    assert "outputs" not in doc["devices"]["dcdc_current"]
    assert doc["devices"]["isospi"] == {"model": "ltc6820", "spi": "ams.SPI1", "cs": "ams.PB9"}


def test_a_node_already_in_its_role_is_left_alone(spec):
    graph = to_dataflow(yaml.safe_load((REPO / "systems" / "ams.yaml").read_text()), spec)
    assert switch_role(_node(graph, "ams"), graph["graphs"][0]["connections"],
                       _types(spec)["mainlite"], BOARD, FIRMWARE) == (None, [], [])


class _Frontend:
    """Pipeline Manager's frontend API as relabel() uses it, on a graph."""

    def __init__(self, graph):
        self.graph, self.calls, self.notes = graph, [], []

    async def request(self, method, params):
        self.calls.append((method, params))
        g = self.graph["graphs"][0]
        if method == "node_get":
            return {"result": {"node": next(n for n in g["nodes"] if n["id"] == params["node_id"])}}
        if method == "graph_get":
            return {"result": {"dataflow": copy.deepcopy(self.graph)}}
        if method == "graph_change":
            self.graph = params["dataflow"]
        return {"result": None}

    async def notify(self, method, params):
        self.notes.append((method, params))


def test_relabel_puts_the_node_back_in_its_new_role(spec):
    """properties_on_change on a role: the backend reads the node and graph,
    replaces the node, restores the wires it keeps and says what it did."""
    graph = to_dataflow(yaml.safe_load((REPO / "systems" / "ams.yaml").read_text()), spec)
    node = _node(graph, "ams")
    graph["graphs"][0]["nodes"] = [_set_role(n, "udv") if n is node else n
                                   for n in graph["graphs"][0]["nodes"]]
    front = _Frontend(graph)
    done = asyncio.run(relabel(front, spec, "system", "n:ams",
                               [{"id": "p:ams:role", "new_value": "udv (node 0x3)"}]))
    assert [m for m, _ in front.calls] == ["node_get", "graph_get", "graph_change"]
    assert front.calls[2][1]["loadingScreen"] is False
    graph = front.graph
    (note,) = front.notes
    assert note[0] == "notification_send" and note[1]["type"] == "warning"
    assert "SPI1" in done and "Removed 1 wire(s)" in done and "PC1" in done
    doc = from_dataflow(graph, spec)
    assert doc["boards"]["ams"]["role"] == "udv"
    assert doc["buses"]["can_acu"]["nodes"] == ["ams.FDCAN1"]
    # Changing it back gives the AMS's node, all but the dropped wire.
    graph["graphs"][0]["nodes"] = [_set_role(n, "ams") if n["id"] == "n:ams" else n
                                   for n in graph["graphs"][0]["nodes"]]
    asyncio.run(relabel(front, spec, "system", "n:ams",
                        [{"id": "p:ams:role", "new_value": "ams (node 0x2)"}]))
    names = [i["name"] for i in _node(front.graph, "ams")["interfaces"]]
    assert names == [i["name"] for i in role_interfaces(BOARD, "ams")[0]]


def test_relabel_ignores_other_properties(spec):
    graph = to_dataflow(yaml.safe_load((REPO / "systems" / "ams.yaml").read_text()), spec)
    front = _Frontend(graph)
    assert asyncio.run(relabel(front, spec, "system", "n:ams",
                               [{"id": "p:ams:firmware_ref", "new_value": "feat/x"}])) is None
    assert asyncio.run(relabel(front, spec, "system", "n:isospi",
                               [{"id": "p:isospi:x", "new_value": 1}])) is None
    assert [m for m, _ in front.calls] == ["node_get", "node_get"]


def test_change_notifications_are_acknowledged():
    """notifyWhenChanged: the frontend tells of every edit; each gets an
    empty reply (null_or_empty), not a method-not-found warning."""
    rpc = EditorMethods()
    for name in ("properties_on_change", "interfaces_on_change", "name_on_change",
                 "position_on_change", "nodes_on_change", "connections_on_change",
                 "graph_on_change", "specification_on_change", "metadata_on_change",
                 "viewport_on_center", "nodes_on_highlight"):
        assert getattr(rpc, name)(graph_id="g", node_id="n") == {}
    assert rpc.specification_get()["content"]["metadata"]["notifyWhenChanged"] is True


def test_validate_warns_of_an_unrouted_pin(spec):
    doc = yaml.safe_load((REPO / "systems" / "ams.yaml").read_text())
    doc["buses"]["can_x"] = {"kind": "can", "nodes": ["ams.FDCAN3"]}
    reply = EditorMethods().dataflow_validate(dataflow=to_dataflow(doc, spec))
    assert reply["type"] == WARNING
    assert "ams: FDCAN3 is not connected on the AMS backplane (docs/backplanes/ams.md)" \
        in reply["content"]
    assert check_system(doc) == ([], [
        "ams: FDCAN3 is not connected on the AMS backplane (docs/backplanes/ams.md)"])


def test_isospi_chain_is_typed_end_to_end(spec):
    bridge = {i["name"]: i for i in _types(spec)["ltc6820"]["interfaces"]}
    chip = {i["name"]: i for i in _types(spec)["ltc6811"]["interfaces"]}
    assert bridge["isospi"]["direction"] == "output"
    assert chip["isospi"]["direction"] == "input"
    assert bridge["isospi"]["type"] == chip["isospi"]["type"]


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_every_system_survives_the_round_trip(spec, path):
    doc = yaml.safe_load(path.read_text())
    graph = to_dataflow(doc, spec)
    assert from_dataflow(graph, spec) == doc
    assert validate(yaml.safe_load(dump_system(doc))) == []


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_firmware_refs_survive_the_round_trip(spec, path):
    """The picker's per-board refs (#116) are board properties in the graph."""
    doc = yaml.safe_load(path.read_text())
    for b in doc["boards"].values():
        b["firmware_ref"] = "feat/x"
        b["bootloader_ref"] = "v1.6.2"     # every MainLite carries the bootloader
    assert from_dataflow(to_dataflow(doc, spec), spec) == doc
    assert validate(doc) == []


def test_bus_arbitration_survives_the_round_trip(spec):
    """#174: arbitration is a property of a bus node, on by default. Only the
    opt-out is written back: a bus without the key stays without it, and
    `arbitration: false` round-trips as itself."""
    doc = yaml.safe_load((REPO / "systems" / "ecu-ams.yaml").read_text())
    graph = to_dataflow(doc, spec)
    bus = next(n for n in graph["graphs"][0]["nodes"] if n["instanceName"] == "can_acu")
    assert {p["name"]: p["value"] for p in bus["properties"]}["arbitration"] is True
    assert from_dataflow(graph, spec) == doc
    assert all("arbitration" not in b for b in from_dataflow(graph, spec)["buses"].values())
    doc["buses"]["can_acu"]["arbitration"] = False
    graph = to_dataflow(doc, spec)
    bus = next(n for n in graph["graphs"][0]["nodes"] if n["instanceName"] == "can_acu")
    assert {p["name"]: p["value"] for p in bus["properties"]}["arbitration"] is False
    assert from_dataflow(graph, spec) == doc
    assert validate(doc) == []


def test_an_explicit_arbitration_true_round_trips_as_the_default(spec):
    """`arbitration: true` says what the default says: the editor writes the
    bus back without it, the same bus."""
    doc = yaml.safe_load((REPO / "systems" / "ecu-ams.yaml").read_text())
    explicit = copy.deepcopy(doc)
    explicit["buses"]["can_acu"]["arbitration"] = True
    assert from_dataflow(to_dataflow(explicit, spec), spec) == doc


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_connections_reference_interfaces_that_exist(spec, path):
    graph = to_dataflow(yaml.safe_load(path.read_text()), spec)["graphs"][0]
    ifaces = {i["id"] for n in graph["nodes"] for i in n["interfaces"]}
    stubs = {s["id"] for n in graph["nodes"] for i in n["interfaces"]
             for s in (i.get("bus") or {}).get("stubs", [])}
    for c in graph["connections"]:
        assert c["from"] in ifaces and c["to"] in ifaces | stubs


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_every_bus_carries_its_own_stubs(spec, path):
    """Each bus node holds exactly the stubs its connections end on, under
    IDs no other bus uses, and the node type holds none: stubs belong to an
    instance. (Pipeline Manager v0.5.2 shared the type's `bus` object between
    instances, so a second bus wiped the first's stubs on load: bus-per-instance
    in editor/pipeline-manager/CHANGELOG-VHIL.md.)"""
    doc = yaml.safe_load(path.read_text())
    g = to_dataflow(doc, spec)["graphs"][0]
    assert "stubs" not in _types(spec)[BUS_NODE]["interfaces"][0]["bus"]
    buses = {n["instanceName"]: n for n in g["nodes"] if n["name"] == BUS_NODE}
    assert set(buses) == set(doc.get("buses", {}))
    seen = set()
    for name, n in buses.items():
        (iface,) = n["interfaces"]
        bus = iface["bus"]
        ids = [s["id"] for s in bus["stubs"]]
        assert len(ids) == len(doc["buses"][name]["nodes"]) == len(set(ids))
        assert not seen & set(ids), f"{name} reuses another bus's stub IDs"
        seen |= set(ids)
        assert bus["type"] == "twoSided" and all(s["side"] in ("left", "right")
                                                 for s in bus["stubs"])
        ends = sorted(c["to"] for c in g["connections"] if c["to"] in ids)
        assert ends == sorted(ids), f"{name}: a stub with no connection, or two on one"
        assert sorted(c["from"] for c in g["connections"] if c["to"] in ids) == sorted(
            "i:{}:{}".format(*e.split(".", 1)) for e in doc["buses"][name]["nodes"])


@pytest.mark.skipif(not (Path(os.environ.get("PM_DIR", "/nonexistent")) / "validate").exists(),
                    reason="needs a Pipeline Manager checkout in $PM_DIR (the editor image)")
def test_pipeline_manager_loads_every_system():
    """Pipeline Manager's own ./validate, which loads each dataflow through
    the frontend: the check that caught the shared-bus-stub bug."""
    assert editor_main(["check", *map(str, SYSTEMS)]) == 0


def test_bus_connections_land_on_spread_stubs(spec):
    """Pipeline Manager draws a bus connection to its stub: one per
    connection, along the bus, not at the bus node's header."""
    doc = yaml.safe_load((REPO / "systems" / "ams.yaml").read_text())
    doc["boards"]["ecu"] = {"board": "mainlite", "role": "ecu"}
    doc["buses"]["can_acu"]["nodes"].append("ecu.FDCAN2")
    g = to_dataflow(doc, spec)["graphs"][0]
    bus = next(n for n in g["nodes"] if n["instanceName"] == "can_acu")["interfaces"][0]["bus"]
    offsets = [s["offset"] for s in bus["stubs"]]
    assert len(offsets) == 2 == len(set(offsets))
    assert all(0 < o < bus["size"] for o in offsets) and bus["size"] >= 100
    to_bus = [c["to"] for c in g["connections"] if c["to"].startswith("s:can_acu:")]
    assert sorted(to_bus) == sorted(s["id"] for s in bus["stubs"])


def test_an_edit_in_the_graph_is_a_valid_system(spec):
    """Put the ECU on the AMS's graph and on its ACU bus: M3's shape."""
    graph = to_dataflow(yaml.safe_load((REPO / "systems" / "ams.yaml").read_text()), spec)
    g = graph["graphs"][0]
    # As the UI does it: a copy of the AMS, set to the ECU role (the backend
    # relabels it), a new stub on the bus, the connection to the stub.
    ecu = copy.deepcopy(next(n for n in g["nodes"] if n["instanceName"] == "ams"))
    ecu["id"], ecu["instanceName"] = "n:ecu", "ecu"
    for p in ecu["properties"]:
        p["id"] = p["id"].replace(":ams:", ":ecu:")
    for i in ecu["interfaces"]:
        i["id"] = i["id"].replace(":ams:", ":ecu:")
    ecu, _, _ = switch_role(_set_role(ecu, "ecu"), [], _types(spec)["mainlite"], BOARD, FIRMWARE)
    g["nodes"].append(ecu)
    fdcan2 = next(i["id"] for i in ecu["interfaces"] if i["name"] == "FDCAN2 · CAN_ACU")
    bus = next(n for n in g["nodes"] if n["instanceName"] == "can_acu")["interfaces"][0]
    bus["bus"]["stubs"].append({"id": "3f6c-stub", "offset": 90, "side": "left"})
    g["connections"].append({"id": "c:new", "from": fdcan2, "to": "3f6c-stub"})
    doc = from_dataflow(graph, spec)
    assert doc["buses"]["can_acu"]["nodes"] == ["ams.FDCAN1", "ecu.FDCAN2"]
    assert doc["boards"]["ecu"] == {"board": "mainlite", "role": "ecu"}
    assert validate(doc) == []


def test_validate_reports_a_broken_graph(spec):
    graph = to_dataflow(yaml.safe_load((REPO / "systems" / "ams.yaml").read_text()), spec)
    g = graph["graphs"][0]
    g["connections"] = [c for c in g["connections"] if c["to"] != "i:isospi:spi"]
    reply = EditorMethods().dataflow_validate(dataflow=graph)
    assert reply["type"] == ERROR and "spi" in reply["content"]


def test_rpc_replies_match_pipeline_managers_api():
    """Shapes from its api_specification/common_types.json."""
    rpc = EditorMethods()
    assert rpc.frontend_on_connect() == {}
    # No navbar buttons: the workspace has no navbar, and its Check
    # validates (editor/pipeline-manager/CHANGELOG-VHIL.md).
    assert rpc.app_capabilities_get() == []
    assert set(rpc.specification_get()) == {"type", "content"}


def test_rpc_import_export_round_trip():
    rpc = EditorMethods()
    text = (REPO / "systems" / "ecu.yaml").read_text()
    imported = rpc.dataflow_import(external_application_dataflow=base64.b64encode(text.encode()).decode(),
                                   mime="application/yaml", base64=True)
    assert imported["type"] == OK
    exported = rpc.dataflow_export(dataflow=imported["content"])
    assert exported["type"] == OK and exported["filename"] == "ecu.yaml"
    # Unchanged, the file comes back byte for byte, comments included.
    assert base64.b64decode(exported["content"]).decode() == text


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_an_unedited_system_writes_back_identical(spec, path):
    text = path.read_text()
    assert write_system(yaml.safe_load(text), text) == text


def test_an_edit_keeps_the_files_comments():
    """M3's shape again, through write_system: comments and untouched lines
    stay; the new board and bus member appear in the file's style."""
    text = (REPO / "systems" / "ams.yaml").read_text()
    doc = yaml.safe_load(text)
    doc["boards"]["ecu"] = {"board": "mainlite", "role": "ecu"}
    doc["buses"]["can_acu"]["nodes"].append("ecu.FDCAN2")
    del doc["devices"]["sd"]
    out = write_system(doc, text)
    assert yaml.safe_load(out) == doc
    assert "  ecu: {board: mainlite, role: ecu}\n" in out
    assert "nodes: [ams.FDCAN1, ecu.FDCAN2]" in out
    assert "sd-card" not in out
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            assert line in out
