"""Scenario expects (vhil/expect.py): the schema the run API takes for them
(vhil/server/runs.py Expect, RunScenario) and their evaluation against a
trace in virtual time. Host-only: traces are written by hand."""
import json

import pytest
from pydantic import ValidationError

from vhil import expect as vexpect
from vhil.server.runs import Expect, RunRequest, RunScenario

# A contract as vhil/server/decode.py system_contract gives it.
CONTRACT = {"buses": {"can_acu": {
    str(0x4A0): {"name": "AMS_status", "id": 0x4A0, "ext": False, "dlc": 8, "fields": [
        {"name": "fsm_state", "be": False, "signed": False, "start": 0, "length": 8,
         "factor": 1, "offset": 0, "unit": "enum",
         "values": {"0": "Start", "1": "Precharge", "3": "Run", "5": "Error"}},
        {"name": "min_cell_mV", "be": True, "signed": False, "start": 39, "length": 16,
         "factor": 1, "offset": 0, "unit": "mV"}]},
    str(0x100): {"name": "VCU_heartbeat", "id": 0x100, "ext": False, "dlc": 3, "fields": [
        {"name": "dc_bus_voltage", "be": False, "signed": False, "start": 0, "length": 16,
         "factor": 1, "offset": 0, "unit": "V"}]}}}}


def frame(t_ms, can_id, data, bus="can_acu", **kw):
    return {"kind": "frame", "t_us": int(t_ms * 1000), "bus": bus, "id": can_id, "ext": False,
            "data": data, **kw}


def state(t_ms, s):
    return frame(t_ms, 0x4A0, f"{s:02x}000000" "0e100e74")


def edge(t_ms, level, pin="PB5", **kw):
    return {"kind": "edge", "t_us": int(t_ms * 1000), "board": "ams", "pin": pin, "level": level,
            **kw}


def sample(t_ms, value, name="g_state"):
    return {"kind": "sample", "t_us": int(t_ms * 1000), "board": "ams", "name": name,
            "value": value}


def one(expect, records, end_ms=1000):
    return vexpect.evaluate([expect], records, CONTRACT, end_ms * 1000)[0]


# -- signals --------------------------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("frame:can_acu.AMS_status.fsm_state", ("frame", "can_acu", "AMS_status", "fsm_state")),
    ("frame:can_acu.0x4A0.fsm_state", ("frame", "can_acu", "0x4A0", "fsm_state")),
    ("frame:can_acu.0x100", ("frame", "can_acu", "0x100", "")),
    ("symbol:ams.g_state_telemetry", ("symbol", "ams", "g_state_telemetry", "")),
    ("pin:ams.PB5", ("pin", "ams", "PB5", "")),
])
def test_signals_parse(text, want):
    assert tuple(vexpect.parse_signal(text)) == want
    assert str(vexpect.parse_signal(text)) == text


@pytest.mark.parametrize("text", [
    "", "frame:can_acu", "frame:can acu.0x100", "frame:can_acu.0x100.x.y", "symbol:ams",
    "pin:ams.PB5\n", "pin:ams.PB5;quit", "frame:can_acu.0x1FFFFFFFF", "wire:ams.PB5",
    'symbol:ams.a"b', "pin:../ams.PB5", "frame:can_acu.0x100 ", "pin:ams.PB5 ",
])
def test_bad_signals_are_refused(text):
    with pytest.raises(ValueError):
        vexpect.parse_signal(text)


# -- the schema -----------------------------------------------------------------------

def exp(**kw):
    return Expect.model_validate({"check": "eventually", "signal": "pin:ams.PB5", "value": 1, **kw})


@pytest.mark.parametrize("kw, message", [
    ({"value": None}, "needs a value"),
    ({"signal": "frame:can_acu.0x100"}, "reads a value"),
    ({"until_ms": 5, "at_ms": 10}, "before at_ms"),
    ({"value": "Precharge", "op": "<"}, "== or != only"),
    ({"min_ms": 5}, "takes no min_ms"),
    ({"check": "period", "signal": "frame:can_acu.0x100", "value": None}, "needs min_ms or max_ms"),
    ({"check": "period", "signal": "frame:can_acu.0x100", "max_ms": 12}, "takes no value"),
    ({"check": "period", "signal": "pin:ams.PB5", "value": None, "max_ms": 12}, "counts a frame"),
    ({"check": "count", "signal": "frame:can_acu.0x100.x", "value": None, "max": 0},
     "counts a frame"),
    ({"check": "count", "signal": "frame:can_acu.0x100", "value": None, "min": 3, "max": 1},
     "more than"),
    ({"value": float("nan")}, "finite"),
    ({"value": "a\nb"}, "pattern"),
    ({"check": "sometimes"}, "check"),
    ({"name": "a b"}, "pattern"),
])
def test_an_expect_says_what_is_wrong_with_it(kw, message):
    with pytest.raises(ValidationError, match=message):
        exp(**kw)


def test_expects_take_numbers_booleans_and_labels():
    assert exp(value=True).value is True
    assert exp(value=1.5, op=">=").value == 1.5
    assert exp(value="high").value == "high"
    assert exp(signal="frame:can_acu.AMS_status.fsm_state", value="Precharge").value == "Precharge"
    assert exp(check="count", signal="frame:can_acu.0x100", value=None, max=0).max == 0


def run(**kw):
    return RunScenario.model_validate({"kind": "run", "virtual_ms": 1000, **kw})


def test_a_stop_names_a_periodic_started_before_it():
    periodic = {"kind": "can_periodic", "name": "vcu", "at_ms": 100, "bus": "can_acu",
                "id": 0x100, "data": "00", "period_ms": 10}
    run(stimuli=[periodic, {"kind": "stop_periodic", "at_ms": 200, "periodic": "vcu"}])
    with pytest.raises(ValidationError, match="no can_periodic named 'x'"):
        run(stimuli=[periodic, {"kind": "stop_periodic", "at_ms": 200, "periodic": "x"}])
    with pytest.raises(ValidationError, match="before it starts"):
        run(stimuli=[periodic, {"kind": "stop_periodic", "at_ms": 50, "periodic": "vcu"}])
    with pytest.raises(ValidationError, match="a second periodic named 'vcu'"):
        run(stimuli=[periodic, periodic])


def test_an_expect_ends_by_the_runs_end():
    run(expect=[{"check": "never", "signal": "pin:ams.PB5", "value": 1, "until_ms": 1000}])
    with pytest.raises(ValidationError, match="past the run's 1000 ms"):
        run(expect=[{"check": "never", "signal": "pin:ams.PB5", "value": 1, "until_ms": 1001}])


def test_a_run_request_carries_a_scenario_name():
    req = RunRequest.model_validate({"system": "ams", "scenario": {
        "kind": "run", "name": "tsms-precharge", "virtual_ms": 10}})
    assert req.scenario.name == "tsms-precharge"
    for bad in ("TSMS", "a/b", "../x", "-x", "a b", "x\n"):
        with pytest.raises(ValidationError):
            RunRequest.model_validate({"system": "ams", "scenario": {
                "kind": "run", "name": bad, "virtual_ms": 10}})


# -- value checks ---------------------------------------------------------------------

STATE = "frame:can_acu.AMS_status.fsm_state"


def test_eventually_passes_at_the_first_match_with_its_evidence():
    r = one({"check": "eventually", "signal": STATE, "value": "Precharge", "until_ms": 800},
            [state(100, 0), state(600, 1), state(700, 3)])
    assert r["passed"] and r["t_us"] == 600_000 and r["value"] == "Precharge"
    assert r["detail"] == "== Precharge at 600 ms"


def test_eventually_fails_with_the_last_value_seen():
    r = one({"check": "eventually", "signal": STATE, "value": "Run", "until_ms": 800},
            [state(100, 0), state(600, 1), state(900, 3)])
    assert not r["passed"] and r["t_us"] == 600_000 and r["value"] == "Precharge"
    assert "never == Run" in r["detail"] and "last Precharge at 600 ms" in r["detail"]


def test_the_value_carried_into_the_window_counts_at_its_start():
    r = one({"check": "eventually", "signal": STATE, "value": 1, "at_ms": 500, "until_ms": 550},
            [state(100, 1), state(600, 3)])
    assert r["passed"] and r["t_us"] == 500_000


def test_numbers_compare_physical_values():
    r = one({"check": "always", "signal": "frame:can_acu.0x4A0.min_cell_mV", "op": ">=",
             "value": 3600}, [state(100, 0), state(200, 1)])
    assert r["passed"], r          # 0x0e10 = 3600 mV, big-endian at byte 4
    r = one({"check": "always", "signal": "frame:can_acu.0x4A0.min_cell_mV", "op": ">",
             "value": 3600}, [state(100, 0)])
    assert not r["passed"] and r["value"] == 3600 and r["detail"] == "3600 at 100 ms: not > 3600"


def test_always_needs_a_value_and_never_does_not():
    assert "no value" in one({"check": "always", "signal": STATE, "value": 0}, [])["detail"]
    r = one({"check": "never", "signal": STATE, "value": "Error"}, [])
    assert r["passed"] and "0 values" in r["detail"]


def test_never_fails_at_the_first_match():
    r = one({"check": "never", "signal": STATE, "value": "Error", "at_ms": 200},
            [state(100, 5), state(300, 0), state(400, 5)])
    assert not r["passed"] and r["t_us"] == 200_000 and r["value"] == "Error"


def test_the_scenarios_own_frames_are_not_observations():
    r = one({"check": "eventually", "signal": "frame:can_acu.VCU_heartbeat.dc_bus_voltage",
             "value": 356}, [frame(100, 0x100, "640102", src="stimulus")])
    assert not r["passed"] and "no value" in r["detail"]


def test_pins_read_their_initial_level_and_edges():
    records = [edge(0, 0, initial=True), edge(300, 1), edge(700, 0)]
    assert one({"check": "eventually", "signal": "pin:ams.PB5", "value": "high", "until_ms": 400},
               records)["t_us"] == 300_000
    assert one({"check": "always", "signal": "pin:ams.PB5", "value": True, "at_ms": 300,
                "until_ms": 600}, records)["passed"]
    r = one({"check": "always", "signal": "pin:ams.PB5", "value": 1, "at_ms": 300}, records)
    assert not r["passed"] and r["t_us"] == 700_000
    assert one({"check": "never", "signal": "pin:ams.PB4", "value": 1}, records)["passed"]


def test_symbols_read_their_samples():
    records = [sample(t, 0 if t < 500 else 1) for t in range(0, 1001, 10)]
    r = one({"check": "eventually", "signal": "symbol:ams.g_state", "value": 1}, records)
    assert r["passed"] and r["t_us"] == 500_000


# -- frame checks ---------------------------------------------------------------------

HB = [frame(t, 0x100, "000002") for t in range(10, 1001, 10)]


def test_period_passes_when_every_gap_is_in_range():
    r = one({"check": "period", "signal": "frame:can_acu.0x100", "min_ms": 9, "max_ms": 11,
             "at_ms": 100, "until_ms": 900}, HB)
    assert r["passed"] and r["value"] == 81


def test_period_fails_on_the_worst_gap_and_on_silence_at_the_end():
    gap = [f for f in HB if not 400_000 < f["t_us"] < 450_000]
    r = one({"check": "period", "signal": "frame:can_acu.VCU_heartbeat", "max_ms": 12}, gap)
    assert not r["passed"] and r["t_us"] == 450_000 and r["value"] == 50
    stops = [f for f in HB if f["t_us"] <= 500_000]
    r = one({"check": "period", "signal": "frame:can_acu.0x100", "max_ms": 12, "until_ms": 900},
            stops)
    assert not r["passed"] and r["t_us"] == 900_000 and r["value"] == 400


def test_period_needs_two_frames():
    r = one({"check": "period", "signal": "frame:can_acu.0x100", "max_ms": 12}, HB[:1])
    assert not r["passed"] and "a period needs two" in r["detail"]


def test_count_says_a_frame_never_came():
    assert one({"check": "count", "signal": "frame:can_acu.0x7FF", "max": 0}, HB)["passed"]
    r = one({"check": "count", "signal": "frame:can_acu.0x100", "max": 0, "at_ms": 500}, HB)
    assert not r["passed"] and r["t_us"] == 500_000 and r["value"] == 51
    assert one({"check": "count", "signal": "frame:can_acu.0x100", "min": 100}, HB)["passed"]


def test_a_frame_on_another_bus_is_another_frame():
    assert one({"check": "count", "signal": "frame:can_inv.0x100", "max": 0}, HB)["passed"]


# -- what the contract lacks ------------------------------------------------------------

@pytest.mark.parametrize("expect, detail", [
    ({"check": "eventually", "signal": "frame:can_acu.Nope.x", "value": 1}, "no message Nope"),
    ({"check": "eventually", "signal": "frame:can_acu.AMS_status.nope", "value": 1},
     "no field nope"),
    ({"check": "eventually", "signal": STATE, "value": "Charging"}, "not a label"),
    ({"check": "count", "signal": "frame:can_acu.Nope", "max": 0}, "no message Nope"),
])
def test_what_the_contract_lacks_fails_the_expect_and_says_why(expect, detail):
    r = one(expect, [state(100, 1)])
    assert not r["passed"] and detail in r["detail"]


def test_evaluate_trace_reads_a_file(tmp_path):
    path = tmp_path / "trace.jsonl"
    records = [{"kind": "run", "t_us": 0, "token": "x"}, {"kind": "log", "t_us": 0, "text": "hi"},
               state(100, 0), state(600, 1)]
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + '{"kind": "frame", "t_')
    results = vexpect.evaluate_trace(path, [{"check": "eventually", "signal": STATE,
                                             "value": "Precharge", "name": "pre"}], CONTRACT,
                                     1_000_000)
    assert results[0]["passed"] and results[0]["name"] == "pre" and results[0]["index"] == 0
    assert vexpect.summary(results)["expects_failed"] == 0


def test_what_the_worker_must_watch():
    ex = [{"signal": "pin:ams.PB5"}, {"signal": "pin:ams.PB5"}, {"signal": "symbol:ams.g"},
          {"signal": "frame:can_acu.0x100"}, {"signal": "bad"}]
    assert vexpect.pins(ex) == [("ams", "PB5")]
    assert vexpect.symbols(ex) == [("ams", "g")]
