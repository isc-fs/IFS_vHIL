"""Scenario files (vhil/server/scenarios.py): the canonical YAML, the checks
a scenario passes before it is saved or run (schema and injection, the
system, the firmware's contract), saves to a branch, listings with their
last runs, and the system's contract without a run.

Every API test works on a throwaway clone (tests/unit/test_systems_write.py's
fixtures); the committed scenarios of this repo are checked as CI's unit job
would (test_every_committed_scenario_validates)."""
import shutil

import pytest
import yaml

fastapi = pytest.importorskip("fastapi")

from vhil.candef import MESSAGES  # noqa: E402
from vhil.server import scenarios as sc  # noqa: E402
from vhil.server.runs import Limits, RunStore  # noqa: E402
from vhil.system import REPO, System  # noqa: E402
from vhil.worker import FirmwareResolver  # noqa: E402

from tests.unit.test_systems_write import env, git, remote, snapshot  # noqa: E402,F401

PERIODIC = {"kind": "can_periodic", "name": "vcu", "at_ms": 2500, "bus": "can_acu", "id": 0x100,
            "data": "000002", "period_ms": 10}
SCENARIO = {
    "description": "TSMS on, a press: Precharge",
    "virtual_ms": 6000,
    "stimuli": [PERIODIC,
                {"kind": "gpio", "name": "tsms", "at_ms": 5000, "board": "ams", "pin": "PF9",
                 "level": True},
                {"kind": "stop_periodic", "at_ms": 5500, "periodic": "vcu"}],
    "watch": [{"kind": "symbol", "board": "ams", "name": "g_state_telemetry", "size": 1,
               "period_ms": 10}],
    "expect": [{"check": "eventually", "name": "precharge", "at_ms": 5000, "until_ms": 6000,
                "signal": "frame:can_acu.AMS_status.fsm_state", "value": "Precharge"},
               {"check": "period", "signal": "frame:can_acu.0x4A0", "max_ms": 600}],
}
STATUS_DEF = '''CAN_MSG(AMS_status, 0x4A0, 8, "AMS", 500)
    FIELD_LE (fsm_state, uint8_t, 0, 8, 1, 0, "enum")
    FIELD_BE (min_cell_mV, uint16_t, 4, 16, 1, 0, "mV")
CAN_MSG_END(AMS_status)
CAN_VAL(AMS_status, fsm_state, 0, "Start")
CAN_VAL(AMS_status, fsm_state, 1, "Precharge")
CAN_MSG(VCU_heartbeat, 0x100, 3, "VCU", 10)
    FIELD_LE_BITS (dc_bus_voltage, uint16_t, 0, 16, 1, 0, "V")
CAN_MSG_END(VCU_heartbeat)
'''


def parsed(doc, name="tsms"):
    return sc.parse(doc, "ams", name)


# -- the file -------------------------------------------------------------------------

def test_a_scenario_is_written_one_row_a_line_and_reads_back_the_same():
    run, description, errors = parsed(SCENARIO)
    assert errors == [] and run.name == "tsms"
    text = sc.dump_scenario(run, "ams", description)
    assert text.splitlines()[:5] == [
        "kind: scenario", "system: ams", 'description: "TSMS on, a press: Precharge"',
        "virtual_ms: 6000", "stimuli:"]
    assert ("  - {kind: can_periodic, name: vcu, at_ms: 2500, bus: can_acu, id: 0x100, "
            'data: "000002", period_ms: 10}') in text
    assert "  - {kind: stop_periodic, at_ms: 5500, periodic: vcu}" in text
    assert ("  - {check: eventually, name: precharge, at_ms: 5000, until_ms: 6000, "
            'signal: "frame:can_acu.AMS_status.fsm_state", value: Precharge}') in text
    again, desc2, errors = sc.load_text(text, "ams", "tsms")
    assert errors == [] and again == run and desc2 == description
    # Defaults are filled in: what the editor shows.
    assert sc.as_data(again, desc2) == {
        **SCENARIO, "slice_ms": 100,
        "stimuli": [{**PERIODIC, "ext": False}, *SCENARIO["stimuli"][1:]],
        "expect": [SCENARIO["expect"][0], {**SCENARIO["expect"][1], "at_ms": 0}]}


@pytest.mark.parametrize("value", ["yes", "no", "on", "null", "1", "0x10", "a: b", "a b"])
def test_text_values_that_yaml_would_retype_are_quoted(value):
    run, _, errors = parsed({**SCENARIO, "expect": [
        {"check": "eventually", "signal": "frame:can_acu.AMS_status.fsm_state", "value": value}]})
    assert errors == [], errors
    again, _, errors = sc.load_text(sc.dump_scenario(run, "ams"), "ams", "tsms")
    assert errors == [] and again.expect[0].value == value


@pytest.mark.parametrize("doc, message", [
    ({**SCENARIO, "kind": "system"}, "kind: 'system' is not 'scenario'"),
    ({**SCENARIO, "system": "ecu"}, "is not this file's system 'ams'"),
    ({**SCENARIO, "description": "x" * 3000}, "description: text, at most"),
    ({**SCENARIO, "virtual_ms": 0}, "virtual_ms: Input should be greater than or equal to 1"),
    ({**SCENARIO, "stimuli": [{**PERIODIC, "bus": "can acu"}]}, "stimuli[0].bus: String should"),
    ({**SCENARIO, "stimuli": [{**PERIODIC, "data": "0g"}]}, "stimuli[0].data: data must be hex"),
    ({**SCENARIO, "stimuli": [PERIODIC, {"kind": "stop_periodic", "periodic": "x"}]},
     "stimuli[1]: no can_periodic named 'x'"),
    ({**SCENARIO, "expect": [{"check": "eventually", "signal": "pin:ams.PB5;quit", "value": 1}]},
     "expect[0].signal: 'pin:ams.PB5;quit' is not a signal"),
    ({**SCENARIO, "extra": 1}, "extra: Extra inputs are not permitted"),
    ([1, 2], "a scenario is a YAML mapping"),
])
def test_a_bad_scenario_says_where(doc, message):
    run, _, errors = parsed(doc)
    assert run is None and any(message in e for e in errors), errors


def test_a_file_too_big_or_not_yaml_is_refused():
    assert "at most 512 KiB" in sc.load_text("#" * (600 << 10), "ams", "x")[2][0]
    assert "not YAML" in sc.load_text("a: [", "ams", "x")[2][0]


# -- the checks against the system and the firmware ---------------------------------------

def ams_contract(tmp_path):
    """The contract a built AMS image's .def files give (a stand-in source)."""
    system = System(REPO / "systems" / "ams.yaml")
    elf = FirmwareResolver(tmp_path, build=False).expected(system, {})["ams"][1]
    messages = elf.parent.parent / MESSAGES
    messages.mkdir(parents=True)
    (messages / "status.def").write_text(STATUS_DEF)
    from vhil.server.decode import system_contract
    return system, system_contract(system, {"ams": elf}), {"ams": elf}


def test_rows_are_checked_against_the_system_and_the_contract(tmp_path):
    system, contract, elfs = ams_contract(tmp_path)
    run, _, _ = parsed(SCENARIO)
    assert sc.check_rows(run, system, contract, elfs, Limits()) == ([], [])
    bad, _, errors = parsed({**SCENARIO, "stimuli": [
        {**PERIODIC, "bus": "can_inv", "data": "00"},
        {"kind": "gpio", "board": "ams", "pin": "PF7", "level": True},
        {"kind": "analog", "board": "bms", "pin": "PF7", "volts": 1}],
        "expect": [
            {"check": "eventually", "signal": "frame:can_acu.AMS_status.fsm_state",
             "value": "Charging"},
            {"check": "eventually", "signal": "frame:can_acu.AMS_status.soc", "value": 1},
            {"check": "eventually", "signal": "frame:can_acu.Nope.x", "value": 1},
            {"check": "always", "signal": "frame:can_acu.0x4A0.min_cell_mV", "op": ">",
             "value": 70000},
            {"check": "never", "signal": "pin:ams.PF7", "value": 1},
            {"check": "count", "signal": "frame:can_dash.0x10", "max": 0},
            {"check": "count", "signal": "frame:can_acu.0x7FF", "max": 0}]})
    assert errors == []
    errors, warnings = sc.check_rows(bad, system, contract, elfs, Limits())
    assert errors == [
        "stimuli[0]: no bus 'can_inv' in ams",
        "stimuli[1]: ams.PF7 is analog, not gpio",
        "stimuli[2]: no board 'bms' in ams",
        "expect[4]: ams.PF7 is analog, not gpio",
        "expect[5]: no bus 'can_dash' in ams"]
    # Only once the system's names hold are the firmware's checked.
    fixed = bad.model_copy(update={"stimuli": [bad.stimuli[0].model_copy(update={"bus": "can_acu"})],
                                   "expect": bad.expect[:4] + bad.expect[6:]})
    errors, warnings = sc.check_rows(fixed, system, contract, elfs, Limits())
    assert errors == [
        "expect[0]: 'Charging' is not a label of AMS_status.fsm_state (Start, Precharge)",
        "expect[1]: no field soc in AMS_status (fsm_state, min_cell_mV)",
        "expect[2]: no message Nope on can_acu in the firmware's contract"]
    assert warnings == ["stimuli[0]: 1 bytes, but VCU_heartbeat (0x100) is 3",
                        "expect[3]: 70000 is outside AMS_status.min_cell_mV's range 0..65535"]


def test_a_symbol_takes_a_label_of_its_enum(tmp_path, monkeypatch):
    """`== Precharge` on a symbol: a label of the enum its state view names,
    from the image's DWARF (vhil/stateview.py); numbers still do."""
    from tests.unit.test_elf import _dwarf_elf
    system, _, elfs = ams_contract(tmp_path)
    elfs["ams"].parent.mkdir(parents=True)
    _dwarf_elf(elfs["ams"])     # g_state is an ams::fsm::State, g_int an int
    monkeypatch.setattr(sc.velf, "symbol", lambda path, name: (0x20000000, 1))  # no symtab
    from vhil.server.decode import system_contract
    contract = system_contract(system, elfs)

    def check(signal, value):
        run, _, errors = parsed({**SCENARIO, "expect": [
            {"check": "eventually", "signal": signal, "value": value}]})
        assert errors == []
        return sc.check_rows(run, system, contract, elfs, Limits())

    assert check("symbol:ams.g_state_telemetry", "Precharge")[0] == []
    assert check("symbol:ams.g_state_telemetry", 1)[0] == []
    assert check("symbol:ams.g_state", "Error")[0] == []         # its own DWARF type
    assert check("symbol:ams.g_state_telemetry", "Charging")[0] == [
        "expect[0]: 'Charging' is not a label of g_state_telemetry (Start, Precharge, Error)"]
    assert check("symbol:ams.g_int", "On")[0] == [
        "expect[0]: 'On' is not a label of g_int: it has no enum (give a number, or name its "
        "enum in the state view)"]
    # Not built here: the run checks it.
    unbuilt = {"ams": tmp_path / "nowhere" / "AMS.elf"}
    run, _, _ = parsed({**SCENARIO, "expect": [
        {"check": "eventually", "signal": "symbol:ams.g_state_telemetry", "value": "Run"}]})
    errors, warnings = sc.check_rows(run, system, system_contract(system, unbuilt), unbuilt,
                                     Limits())
    assert errors == [] and warnings == [
        "expect[0]: 'Run' is checked against ams's enums when the run builds its firmware"]


def test_a_watch_op_is_written_and_checked_as_a_row(tmp_path):
    system, contract, elfs = ams_contract(tmp_path)
    doc = {**SCENARIO, "stimuli": [{"kind": "watch", "at_ms": 100, "board": "ams",
                                    "symbol": "g_x", "period_ms": 20},
                                   {"kind": "watch", "at_ms": 200, "board": "ams", "pin": "PF7"}]}
    run, description, errors = parsed(doc)
    assert errors == []
    text = sc.dump_scenario(run, "ams", description)
    assert "  - {kind: watch, at_ms: 100, board: ams, symbol: g_x, period_ms: 20}\n" in text
    assert sc.load_text(text, "ams", "tsms")[0] == run
    assert sc.check_rows(run, system, contract, elfs, Limits())[0] == [
        "stimuli[1]: ams.PF7 is analog, not gpio"]


def test_without_a_built_contract_the_firmwares_names_are_warnings(tmp_path):
    system = System(REPO / "systems" / "ams.yaml")
    from vhil.server.decode import system_contract
    elfs = {"ams": tmp_path / "nowhere" / "AMS.elf"}
    run, _, _ = parsed(SCENARIO)
    errors, warnings = sc.check_rows(run, system, system_contract(system, elfs), elfs, Limits())
    assert errors == [] and len(warnings) == 1
    assert warnings[0].startswith("expect[0]: no contract for can_acu here (firmware of ams not built)")


def test_limits_hold_for_files_too(tmp_path):
    system, contract, elfs = ams_contract(tmp_path)
    run, _, _ = parsed(SCENARIO)
    errors, _ = sc.check_rows(run, system, contract, elfs,
                              Limits(max_stimuli=2, max_expect=1, max_virtual_ms=5000))
    assert errors == ["stimuli: 3 is over this server's limit of 2",
                      "expect: 2 is over this server's limit of 1",
                      "virtual_ms: 6000 is over this server's limit of 5000"]


def test_rows_after_the_end_warn():
    run, _, _ = parsed({**SCENARIO, "virtual_ms": 5200, "expect": []})
    _, warnings = sc.check_rows(run, System(REPO / "systems" / "ams.yaml"), {}, {}, Limits())
    assert warnings == ["stimuli[2]: at 5500 ms, after the run's end (5200 ms)"]


def test_a_stimulus_between_sync_points_warns_where_it_runs():
    """docs/scenarios.md, "Times": the AMS system syncs every 500 us, so
    5600.2 ms runs at 5600.5 ms (the worker moves it); an expect's window
    is the trace's, not a stop of the run, and is left alone."""
    run, _, _ = parsed({**SCENARIO, "stimuli": [
        {"kind": "gpio", "at_ms": 5600.2, "board": "ams", "pin": "PF9", "level": True},
        {"kind": "can_periodic", "name": "v", "at_ms": 2500, "until_ms": 2600.75,
         "bus": "can_acu", "id": 0x100, "data": "00", "period_ms": 10},
        {"kind": "analog", "at_ms": 3000.5, "board": "ams", "pin": "PF7", "volts": 1.0}],
        "expect": [{"check": "never", "at_ms": 0.1, "signal": "symbol:ams.g_x", "value": 5}]})
    _, warnings = sc.check_rows(run, System(REPO / "systems" / "ams.yaml"), {}, {}, Limits())
    assert warnings == [
        "stimuli[0].at_ms: 5600.2 ms is between sync points (every 0.5 ms): it runs at 5600.5 ms",
        "stimuli[1].until_ms: 2600.75 ms is between sync points (every 0.5 ms): it runs at "
        "2601 ms"]


def test_every_committed_scenario_validates():
    """What CI's unit job checks of systems/*.scenarios/: schema and system
    (the contract is the scenario suite's, tests/scenarios/, with firmware)."""
    for path in sorted(REPO.glob("systems/*.scenarios/*.yaml")):
        system_id = path.parent.name.removesuffix(".scenarios")
        run, _, errors = sc.load_text(path.read_text(), system_id, path.stem)
        assert errors == [], (path, errors)
        assert sc.check_rows(run, System(REPO / "systems" / f"{system_id}.yaml"), {}, {},
                             Limits())[0] == [], path


# -- the API --------------------------------------------------------------------------

def put(env, name, system="ams", **body):
    body.setdefault("branch", "feat/scen")
    body.setdefault("message", "test(scenarios): save")
    if "yaml" not in body:
        body.setdefault("scenario", SCENARIO)
    return env.client.put(f"/api/systems/{system}/scenarios/{name}", json=body)


def test_a_save_commits_the_file_on_a_branch_and_leaves_the_checkout_alone(env):
    before = snapshot(env.ws)
    r = put(env, "tsms")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["path"] == "systems/ams.scenarios/tsms.yaml" and out["changed"] and out["created"]
    assert snapshot(env.ws) == before
    text = git(env.ws, "show", "feat/scen:systems/ams.scenarios/tsms.yaml")
    assert yaml.safe_load(text)["stimuli"][0]["id"] == 0x100
    assert git(env.ws, "log", "-1", "--format=%s", "feat/scen") == "test(scenarios): save"
    # The same scenario again: nothing to commit.
    again = put(env, "tsms").json()
    assert not again["changed"] and again["ref"] == out["ref"]


def test_an_unchanged_scenario_keeps_its_text(env):
    text = "# hand-written\n" + sc.dump_scenario(parsed(SCENARIO)[0], "ams", SCENARIO["description"])
    first = put(env, "tsms", yaml=text).json()
    assert first["changed"]
    r = put(env, "tsms", message="test: no-op")
    assert r.status_code == 200 and not r.json()["changed"]
    assert git(env.ws, "show", "feat/scen:systems/ams.scenarios/tsms.yaml").startswith("# hand")


def test_an_invalid_scenario_is_422_and_saves_nothing(env):
    r = put(env, "tsms", scenario={**SCENARIO, "stimuli": [{**PERIODIC, "bus": "can_inv"}]})
    assert r.status_code == 422 and r.json()["detail"]["errors"] == [
        "stimuli[0]: no bus 'can_inv' in ams"]
    assert git(env.ws, "branch", "--list", "feat/scen") == ""


@pytest.mark.parametrize("name", ["TSMS", "a/b", "..", "a b", "-x", "x" * 65])
def test_bad_names_are_refused(env, name):
    r = put(env, name)
    assert r.status_code in (404, 405, 422), r.text
    assert git(env.ws, "branch", "--list", "feat/scen") == ""


@pytest.mark.parametrize("branch", ["dev", "main", "-x", "a..b", "feat/x y"])
def test_protected_and_malformed_branches_are_refused(env, branch):
    assert put(env, "tsms", branch=branch).status_code == 422


def test_listing_and_reading_on_a_branch_with_the_last_run(env):
    # The repo's own (systems/*.scenarios/, seeded into the clone) are there too.
    seeds = [s["name"] for s in env.client.get("/api/systems/ams/scenarios").json()]
    assert "tsms" not in seeds
    put(env, "tsms")
    listed = env.client.get("/api/systems/ams/scenarios?branch=feat/scen").json()
    assert [(s["name"], s["expects"], s["valid"], s["last_run"]) for s in listed
            if s["name"] not in seeds] == [("tsms", 2, True, None)]
    assert all(s["valid"] for s in listed)
    store = RunStore(env.app.state.settings.db)
    run_id = store.create("ams", "", {}, {"kind": "run", "virtual_ms": 10, "name": "tsms"})
    store.claim("w")
    store.finish(run_id, "failed", 10, {"expects_passed": 1, "expects_failed": 1})
    last = next(s for s in env.client.get("/api/systems/ams/scenarios?branch=feat/scen").json()
                if s["name"] == "tsms")["last_run"]
    assert last["id"] == run_id and last["state"] == "failed" and last["expects_failed"] == 1
    one = env.client.get("/api/systems/ams/scenarios/tsms?branch=feat/scen").json()
    assert one["exists"] and one["errors"] == [] and one["scenario"]["virtual_ms"] == 6000
    assert env.client.get("/api/systems/ams/scenarios/nope?branch=feat/scen").status_code == 404


def test_every_systems_scenarios_as_checked_out(env):
    folder = env.ws / "systems" / "ecu-ams.scenarios"
    folder.mkdir()
    (folder / "hb.yaml").write_text("kind: scenario\nsystem: ecu-ams\nvirtual_ms: 100\n")
    listed = [(s["system"], s["name"]) for s in env.client.get("/api/scenarios").json()]
    assert ("ecu-ams", "hb") in listed
    seeds = sorted((p.parent.name.removesuffix(".scenarios"), p.stem)
                   for p in REPO.glob("systems/*.scenarios/*.yaml"))
    assert sorted(listed) == sorted(seeds + [("ecu-ams", "hb")])


def test_preview_checks_without_saving(env):
    r = env.client.post("/api/systems/ams/scenarios/tsms/preview",
                        json={"scenario": {**SCENARIO, "virtual_ms": 5200}})
    out = r.json()
    assert r.status_code == 200 and out["errors"] == [
        "expect[0]: until_ms 6000 is past the run's 5200 ms"] and out["scenario"] is None
    out = env.client.post("/api/systems/ams/scenarios/tsms/preview",
                          json={"scenario": SCENARIO}).json()
    assert out["errors"] == [] and out["yaml"].startswith("kind: scenario\nsystem: ams\n")
    assert out["warnings"] and "no contract for can_acu" in out["warnings"][0]
    assert git(env.ws, "branch", "--list", "feat/scen") == ""


@pytest.mark.parametrize("query", ["branch=-x", "branch=a..b", "fw=nope=dev", "fw=ams=-x"])
def test_hostile_queries_are_refused(env, query):
    assert env.client.get(f"/api/systems/ams/contract?{query}").status_code == 422


def test_the_contract_without_a_run(env, tmp_path):
    fw = env.app.state.fw_dir
    system = System(REPO / "systems" / "ams.yaml")
    elf = FirmwareResolver(fw, build=False).expected(system, {"ams": "feat/x"})["ams"][1]
    (elf.parent.parent / MESSAGES).mkdir(parents=True)
    (elf.parent.parent / MESSAGES / "status.def").write_text(STATUS_DEF)
    out = env.client.get("/api/systems/ams/contract").json()
    assert out["buses"]["can_acu"] == {} and out["built"] == {"ams": False}
    out = env.client.get("/api/systems/ams/contract?fw=ams=feat/x").json()
    assert out["buses"]["can_acu"][str(0x4A0)]["name"] == "AMS_status"
    assert env.client.get("/api/systems/nope/contract").status_code == 404
    shutil.rmtree(elf.parent.parent)


def test_the_contract_carries_each_boards_state_view(env):
    out = env.client.get("/api/systems/ams/contract").json()
    view = out["state"]["ams"]
    assert view["firmware"] == "ams" and view["errors"] == []
    assert view["items"][0]["signal"] == "symbol:ams.g_state_telemetry"
    assert view["items"][0]["note"] == "enum ams::fsm::State: firmware not built here"
    assert out["labels"]["symbol:ams.g_fault_reason_telemetry"] == {"12": "FsmError"}


def test_a_firmwares_enums(env):
    from tests.unit.test_elf import _dwarf_elf
    fw = env.app.state.fw_dir
    system = System(REPO / "systems" / "ams.yaml")
    out = env.client.get("/api/firmware/ams/enums").json()
    assert out == {"id": "ams", "ref": "dev", "built": False, "source": "none",
                   "note": "firmware not built here", "enums": {}, "variables": {}}
    elf = FirmwareResolver(fw, build=False).expected(system, {"ams": "feat/x"})["ams"][1]
    elf.parent.mkdir(parents=True)
    _dwarf_elf(elf)
    out = env.client.get("/api/firmware/ams/enums?ref=feat/x").json()
    assert out["built"] and out["source"] == "dwarf"
    assert out["enums"]["ams::fsm::State"] == {"0": "Start", "1": "Precharge", "5": "Error"}
    assert out["variables"]["g_state"] == "ams::fsm::State"
    assert env.client.get("/api/firmware/nope/enums").status_code == 404
    for bad in ("/api/firmware/AMS;x/enums", "/api/firmware/ams/enums?ref=-x",
                "/api/firmware/ams/enums?ref=a..b"):
        assert env.client.get(bad).status_code == 422, bad
    shutil.rmtree(elf.parent.parent)
