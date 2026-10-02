"""Host-only checks of the editor backend: catalogue -> node types, and system
files <-> Pipeline Manager graphs."""
import base64
import copy

import pytest
import yaml

from vhil.editor import (BUS_NODE, ERROR, OK, EditorMethods, dump_system, from_dataflow,
                         specification, to_dataflow, validate, write_system)
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
    carrier = {i["name"]: i["type"] for i in _types(spec)["mlc-carrier"]["interfaces"]}
    assert carrier["FDCAN1"] == "can" and carrier["SPI1"] == "spi"
    assert carrier["PB9"] == "gpio" and carrier["PF7"] == "analog"
    bus = _types(spec)[BUS_NODE]["interfaces"][0]
    assert bus["type"] == "can" and "bus" in bus


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


def test_connections_reference_interfaces_that_exist(spec):
    graph = to_dataflow(yaml.safe_load((REPO / "systems" / "ams.yaml").read_text()), spec)["graphs"][0]
    ifaces = {i["id"] for n in graph["nodes"] for i in n["interfaces"]}
    stubs = {s["id"] for n in graph["nodes"] for i in n["interfaces"]
             for s in (i.get("bus") or {}).get("stubs", [])}
    for c in graph["connections"]:
        assert c["from"] in ifaces and c["to"] in ifaces | stubs


def test_bus_connections_land_on_spread_stubs(spec):
    """Pipeline Manager draws a bus connection to its stub: one per
    connection, along the bus, not at the bus node's header."""
    doc = yaml.safe_load((REPO / "systems" / "ams.yaml").read_text())
    doc["boards"]["ecu"] = {"board": "mlc-carrier", "firmware": "ecu"}
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
    ecu["id"], ecu["instanceName"] = "n:ecu", "ecu"
    for p in ecu["properties"]:
        p["id"] = p["id"].replace(":ams:", ":ecu:")
        if p["name"] == "firmware":
            p["value"] = "ecu"
    for i in ecu["interfaces"]:
        i["id"] = i["id"].replace(":ams:", ":ecu:")
    g["nodes"].append(ecu)
    # As the UI does it: a new stub on the bus, the connection to the stub.
    bus = next(n for n in g["nodes"] if n["instanceName"] == "can_acu")["interfaces"][0]
    bus["bus"]["stubs"].append({"id": "3f6c-stub", "offset": 90, "side": "left"})
    g["connections"].append({"id": "c:new", "from": "i:ecu:FDCAN2", "to": "3f6c-stub"})
    doc = from_dataflow(graph, spec)
    assert doc["buses"]["can_acu"]["nodes"] == ["ams.FDCAN1", "ecu.FDCAN2"]
    assert doc["boards"]["ecu"] == {"board": "mlc-carrier", "firmware": "ecu"}
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
    doc["boards"]["ecu"] = {"board": "mlc-carrier", "firmware": "ecu"}
    doc["buses"]["can_acu"]["nodes"].append("ecu.FDCAN2")
    del doc["devices"]["sd"]
    out = write_system(doc, text)
    assert yaml.safe_load(out) == doc
    assert "  ecu: {board: mlc-carrier, firmware: ecu}\n" in out
    assert "nodes: [ams.FDCAN1, ecu.FDCAN2]" in out
    assert "sd-card" not in out
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            assert line in out
