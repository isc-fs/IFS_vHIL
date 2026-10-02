"""Host-only checks of the editor backend: catalogue -> node types, and system
files <-> Pipeline Manager graphs."""
import base64
import copy

import pytest
import yaml

from vhil.editor import (BUS_NODE, ERROR, OK, EditorMethods, dump_system, from_dataflow,
                         specification, to_dataflow, validate)
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
    for c in graph["connections"]:
        assert c["from"] in ifaces and c["to"] in ifaces


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
    g["connections"].append({"id": "c:new", "from": "i:ecu:FDCAN2", "to": "i:can_acu:bus"})
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
    assert yaml.safe_load(base64.b64decode(exported["content"])) == yaml.safe_load(text)
