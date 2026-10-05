"""Host-only checks of the editor backend: catalogue -> node types, and system
files <-> Pipeline Manager graphs."""
import base64
import copy
import os
from pathlib import Path

import pytest
import yaml

from vhil.editor import (BUS_NODE, ERROR, OK, EditorMethods, dump_system, from_dataflow,
                         specification, to_dataflow, validate, write_system)
from vhil.editor import main as editor_main
from vhil.system import REPO

SYSTEMS = sorted((REPO / "systems").glob("*.yaml"))


@pytest.fixture(scope="module")
def spec():
    return specification()


def _types(spec):
    return {n["name"]: n for n in spec["nodes"]}


def test_spec_has_a_node_type_per_catalogue_entry(spec):
    names = set(_types(spec))
    for folder in ("boards", "models"):
        for path in (REPO / "catalog" / folder).glob("*.yaml"):
            assert yaml.safe_load(path.read_text())["id"] in names
    assert BUS_NODE in names


def test_board_connectors_are_typed(spec):
    board = {i["name"]: i["type"] for i in _types(spec)["mainlite"]["interfaces"]}
    assert board["FDCAN1"] == "can" and board["SPI1"] == "spi"
    assert board["PB9"] == "gpio" and board["PF7"] == "analog"
    bus = _types(spec)[BUS_NODE]["interfaces"][0]
    assert bus["type"] == "can" and "bus" in bus


def test_isospi_chain_is_typed_end_to_end(spec):
    bridge = {i["name"]: i for i in _types(spec)["ltc6820"]["interfaces"]}
    chip = {i["name"]: i for i in _types(spec)["ltc6811"]["interfaces"]}
    assert bridge["isospi"]["direction"] == "output"
    assert chip["isospi"]["direction"] == "input"
    assert bridge["isospi"]["type"] == chip["isospi"]["type"]


def test_a_board_on_a_backplane_shows_its_signals(spec):
    """A board on a backplane is its own node type, its routed pins named by
    the backplane's signals and the rest by the board's."""
    ams = {i["name"]: i["type"] for i in _types(spec)["mainlite on ams"]["interfaces"]}
    assert ams["CAN_ACU"] == "can" and ams["LTC6820_CS"] == "gpio"
    assert ams["S_CURRENT_P"] == "analog" and ams["SPI1"] == "spi"
    assert "FDCAN1" not in ams and "PB9" not in ams
    ecu = {i["name"] for i in _types(spec)["mainlite on ecu"]["interfaces"]}
    assert {"CAN_INV", "CAN_ACU", "CAN_DASH", "APPS_1", "START", "RTDS"} <= ecu
    assert _types(spec)["mainlite on udv"]["additionalData"]["vhil"] == {
        "kind": "board", "board": "mainlite", "backplane": "udv"}


def test_a_pin_named_by_the_mcu_lands_on_its_signal(spec):
    """ams.PB9 and ams.LTC6820_CS are one interface; the graph writes the
    backplane's name back."""
    doc = yaml.safe_load((REPO / "systems" / "ams.yaml").read_text())
    doc["devices"]["isospi"]["cs"] = "ams.PB9"
    doc["buses"]["can_acu"]["nodes"] = ["ams.FDCAN1"]
    back = from_dataflow(to_dataflow(doc, spec), spec)
    assert back["devices"]["isospi"]["cs"] == "ams.LTC6820_CS"
    assert back["buses"]["can_acu"]["nodes"] == ["ams.CAN_ACU"]


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
        if "bootloader" in b:
            b["bootloader_ref"] = "v1.6.2"
    assert from_dataflow(to_dataflow(doc, spec), spec) == doc
    assert validate(doc) == []


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
    instances, so a second bus wiped the first's stubs on load: docker/pm/.)"""
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
    doc["boards"]["ecu"] = {"board": "mainlite", "firmware": "ecu"}
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
    ecu = copy.deepcopy(next(n for n in g["nodes"] if n["instanceName"] == "ams"))
    ecu["id"], ecu["instanceName"], ecu["name"] = "n:ecu", "ecu", "mainlite on ecu"
    for p in ecu["properties"]:
        p["id"] = p["id"].replace(":ams:", ":ecu:")
        if p["name"] == "firmware":
            p["value"] = "ecu"
    ecu["interfaces"] = [{"id": f"i:ecu:{i['name']}", "name": i["name"],
                          "direction": i["direction"], "side": i["side"]}
                         for i in _types(spec)["mainlite on ecu"]["interfaces"]]
    g["nodes"].append(ecu)
    # As the UI does it: a new stub on the bus, the connection to the stub.
    bus = next(n for n in g["nodes"] if n["instanceName"] == "can_acu")["interfaces"][0]
    bus["bus"]["stubs"].append({"id": "3f6c-stub", "offset": 90, "side": "left"})
    g["connections"].append({"id": "c:new", "from": "i:ecu:CAN_ACU", "to": "3f6c-stub"})
    doc = from_dataflow(graph, spec)
    assert doc["buses"]["can_acu"]["nodes"] == ["ams.CAN_ACU", "ecu.CAN_ACU"]
    assert doc["boards"]["ecu"] == {"board": "mainlite", "backplane": "ecu", "firmware": "ecu"}
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
    for item in rpc.app_capabilities_get():
        assert set(item) <= {"name", "stopName", "iconName", "procedureName",
                             "allowToRunInParallelWith", "requireResponse"}
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
    doc["boards"]["ecu"] = {"board": "mainlite", "firmware": "ecu"}
    doc["buses"]["can_acu"]["nodes"].append("ecu.FDCAN2")
    del doc["devices"]["sd"]
    out = write_system(doc, text)
    assert yaml.safe_load(out) == doc
    assert "  ecu: {board: mainlite, firmware: ecu}\n" in out
    assert "nodes: [ams.CAN_ACU, ecu.FDCAN2]" in out
    assert "sd-card" not in out
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            assert line in out
