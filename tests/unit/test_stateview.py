"""A firmware's state view (vhil/stateview.py, the catalogue's `state_view`):
its sources placed on a system's boards, what the worker records for it, its
labels (DWARF enums, CAN_VAL, the catalogue's tables) and the schema's
hardening. Host-only: the DWARF is a hand-built ELF (tests/unit/test_elf.py)."""
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from tests.unit.test_elf import _dwarf_elf
from vhil import stateview
from vhil.system import CATALOG, REPO, SCHEMA, System

VALIDATOR = Draft202012Validator(json.loads(SCHEMA.read_text()))
FIRMWARE = {"kind": "firmware", "id": "x", "repo": "a/b", "ref": "main",
            "build": {"configure": "c", "build": "b", "elf": "x.elf"}, "load": {}}


def ams():
    return System(REPO / "systems" / "ams.yaml")


@pytest.mark.parametrize("path", sorted(REPO.glob("systems/*.yaml")), ids=lambda p: p.stem)
def test_every_catalogue_view_resolves_on_every_system(path):
    system = System(path)
    for board in system.boards:
        items, errors = stateview.resolve(system, board)
        assert errors == [], errors
        firmware = system.boards[board].firmware
        assert len(items) == len(firmware.get("state_view") or [])
        if items:       # one FSM state, first; each kind known
            assert [i.kind for i in items].count("state") == 1 and items[0].kind == "state"
            assert {i.kind for i in items} <= set(stateview.KINDS)


def test_sources_become_the_systems_signals():
    items, _ = stateview.resolve(ams(), "ams")
    by = {i.label: i for i in items}
    assert by["State"].signal == "symbol:ams.g_state_telemetry"
    assert by["State"].enum == "ams::fsm::State" and by["State"].period_ms == 10
    assert by["AIR+"].signal == "pin:ams.PB5" and by["AIR+"].kind == "relay"
    assert by["Min cell"].signal == "frame:can_acu.AMS_status.min_cell_mV"
    assert by["Min cell"].unit == "mV" and by["Min cell"].period_ms == 50
    assert by["Fault"].values == {12: "FsmError"}


def test_the_worker_records_what_the_views_read():
    symbols, pins = stateview.watches(ams())
    assert symbols == [("ams", "g_state_telemetry", 10.0), ("ams", "g_fault_reason_telemetry", 10.0),
                       ("ams", "g_mode_locked_telemetry", 50.0)]
    assert pins == [("ams", "PB5"), ("ams", "PB6"), ("ams", "PB7"), ("ams", "PB4")]
    symbols, pins = stateview.watches(System(REPO / "systems" / "ecu-ams.yaml"))
    assert ("ecu", "g_last_ctrl_state", 10.0) in symbols and ("ecu", "PB4") in pins
    assert ("ams", "g_state_telemetry", 10.0) in symbols and ("ams", "PB4") in pins


def test_a_source_the_board_cannot_give_is_an_error():
    system = ams()
    fw = copy.deepcopy(system.boards["ams"].firmware)
    fw["state_view"] = [{"label": "Inv", "source": "frame:FDCAN3.X.y"},
                        {"label": "Current", "source": "pin:PF7"},
                        {"label": "Nope", "source": "pin:PZ99"},
                        {"label": "Trailing", "source": "pin:PB5\n"},
                        {"label": "Ok", "source": "pin:PB5", "kind": "relay"}]
    system.boards["ams"].firmware = fw
    items, errors = stateview.resolve(system, "ams")
    assert [i.label for i in items] == ["Ok"]
    assert errors[0] == "ams state_view[0] (Inv): ams.FDCAN3 is on no CAN bus of ams"
    assert errors[1] == "ams state_view[1] (Current): ams.PF7 is analog, not gpio"
    assert errors[2].startswith("ams state_view[2] (Nope): ")
    # The schema's `$` takes a trailing newline (re.search); resolve doesn't.
    assert errors[3].startswith("ams state_view[3] (Trailing): 'pin:PB5\\n' is not a source")


def item(**kw):
    base = dict(label="State", kind="state", source="symbol:g_state", signal="symbol:ams.g_state",
                unit="", enum="", values={}, period_ms=10.0)
    return stateview.Item(**{**base, **kw})


def test_labels_come_from_dwarf_with_the_catalogues_table_over_them(tmp_path):
    elf = _dwarf_elf(tmp_path / "d.elf")
    got = stateview.item_labels(item(enum="ams::fsm::State", values={9: "Nine"}), elf, {})
    assert got == stateview.Labels({0: "Start", 1: "Precharge", 5: "Error", 9: "Nine"}, "dwarf")
    # The symbol's own type, when the item names no enum.
    assert stateview.item_labels(item(), elf, {}).values[1] == "Precharge"
    # A short name that is unique is enough.
    assert stateview.item_labels(item(enum="State"), elf, {}).source == "dwarf"
    got = stateview.item_labels(item(enum="ams::Nope", values={12: "FsmError"}), elf, {})
    assert got == stateview.Labels({12: "FsmError"}, "table", "no enum ams::Nope in the image's DWARF")


def test_without_an_image_or_dwarf_the_table_is_all(tmp_path):
    got = stateview.item_labels(item(enum="ams::fsm::State"), tmp_path / "nope.elf", {})
    assert got == stateview.Labels({}, "none", "enum ams::fsm::State: firmware not built here")
    from tests.unit.test_elf import _elf
    bare = _elf(tmp_path / "a.elf", [("g_state", 0x2000129C, 1, 1)])
    got = stateview.item_labels(item(enum="ams::fsm::State", values={1: "Precharge"}), bare, {})
    assert got.values == {1: "Precharge"} and got.source == "table"
    assert "no DWARF debug info (built without -g)" in got.note


def test_a_frame_field_is_labelled_by_its_value_table():
    contract = {"buses": {"can_acu": {"1184": {"name": "AMS_status", "id": 0x4A0, "fields": [
        {"name": "fsm_state", "values": {"0": "Start", "3": "Run"}}]}}}}
    it = item(signal="frame:can_acu.AMS_status.fsm_state", source="frame:FDCAN1.AMS_status.fsm_state")
    assert stateview.item_labels(it, None, contract) == stateview.Labels({0: "Start", 3: "Run"},
                                                                         "contract")


def test_the_contract_carries_views_and_labels(tmp_path):
    from vhil.server.decode import system_contract
    elf = _dwarf_elf(tmp_path / "AMS.elf")
    out = system_contract(ams(), {"ams": elf})
    view = out["state"]["ams"]
    assert view["firmware"] == "ams" and view["errors"] == []
    state = view["items"][0]
    assert state == {"label": "State", "kind": "state", "source": "symbol:g_state_telemetry",
                     "signal": "symbol:ams.g_state_telemetry", "unit": "", "period_ms": 10.0,
                     "labels_from": "dwarf", "enum": "ams::fsm::State",
                     "values": {"0": "Start", "1": "Precharge", "5": "Error"}}
    labels = out["labels"]
    assert labels["symbol:ams.g_state_telemetry"]["1"] == "Precharge"
    assert labels["symbol:ams.g_fault_reason_telemetry"] == {"12": "FsmError"}
    assert labels["symbol:ams.g_mode"] == {"-1": "LOG_OFF", "1": "LOG_ON"}   # a typed global
    assert labels["frame:can_acu.ACU_soc.soc_percent"] == {"255": "Unknown"}


def test_a_live_sessions_inputs_are_the_roles_routed_inputs():
    """What a live session's switches and analog inputs drive
    (docs/live-session.md): the role's labelled GPIOs and analog inputs, but
    for the view's own pins (the relays), the spares and a chip select."""
    out = stateview.inputs(ams())
    assert out["ams"] == [
        {"pin": "PF7", "kind": "analog", "label": "S_CURRENT_P"},
        {"pin": "PF8", "kind": "analog", "label": "S_CURRENT_N"},
        {"pin": "PF9", "kind": "gpio", "label": "TSMS"},
        {"pin": "PC1", "kind": "analog", "label": "S_CURRENT_DCDC"},
        {"pin": "PF10", "kind": "gpio", "label": "RST_PIL"},
        {"pin": "PC0", "kind": "analog", "label": "S_TEMP_DCDC"}]
    ecu = stateview.inputs(System(REPO / "systems" / "ecu.yaml"))["ecu"]
    assert {p["pin"]: p["kind"] for p in ecu if p["label"] in ("START", "S_BRAKE", "APPS_1")} == {
        "PB5": "gpio", "PF7": "analog", "PF8": "analog"}
    assert not [p for p in ecu if p["pin"] in ("PB4", "PB6")]          # RTDS, discharge: outputs
    from vhil.server.decode import system_contract
    assert system_contract(ams(), {})["inputs"] == out


def test_enums_of_an_image(tmp_path):
    out = stateview.enums_of(_dwarf_elf(tmp_path / "d.elf"))
    assert out["source"] == "dwarf" and out["enums"]["ams::fsm::State"]["5"] == "Error"
    assert out["variables"]["g_state"] == "ams::fsm::State"
    assert stateview.enums_of(tmp_path / "nope.elf") == {
        "source": "none", "note": "firmware not built here", "enums": {}, "variables": {}}


# -- the schema ------------------------------------------------------------------------

def errors(view):
    return [e.message for e in VALIDATOR.iter_errors({**FIRMWARE, "state_view": view})]


def test_the_catalogue_views_validate():
    for path in sorted((CATALOG / "firmware").glob("*.yaml")):
        import yaml
        assert [e.message for e in VALIDATOR.iter_errors(yaml.safe_load(path.read_text()))] == []


@pytest.mark.parametrize("bad", [
    {"label": "<img src=x onerror=alert(1)>", "source": "symbol:g"},
    {"label": "State", "source": "symbol:g; sysbus Reset"},
    {"label": "State", "source": "symbol:ams.g"},            # no board in the catalogue
    {"label": "State", "source": "frame:FDCAN1.AMS_status"},  # a value needs a field
    {"label": "State", "source": "symbol:g", "kind": "led"},
    {"label": "State", "source": "symbol:g", "unit": "<b>"},
    {"label": "State", "source": "symbol:g", "enum": "a::b c"},
    {"label": "State", "source": "symbol:g", "values": {"x": "One"}},
    {"label": "State", "source": "symbol:g", "values": {"1": "<script>"}},
    {"label": "State", "source": "symbol:g", "period_ms": 0},
    {"label": "State", "source": "symbol:g", "style": "x"},
])
def test_the_schema_refuses_what_could_reach_the_page_or_the_monitor(bad):
    assert errors([bad])


def test_a_good_view_validates():
    assert errors([{"label": "AIR+", "kind": "relay", "source": "pin:PB5"},
                   {"label": "State", "kind": "state", "source": "symbol:g_state",
                    "enum": "ams::fsm::State", "values": {"12": "FsmError", "-1": "Off"}},
                   {"label": "Pack current", "source": "frame:FDCAN1.0x135.current_accu_dA",
                    "unit": "A", "period_ms": 20}]) == []
