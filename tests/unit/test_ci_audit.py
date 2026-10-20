"""scripts/ci-audit.py: the nightly impact audit's checks, without GitHub."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_audit", REPO / "scripts" / "ci-audit.py")
audit = importlib.util.module_from_spec(spec)
sys.modules["ci_audit"] = audit
spec.loader.exec_module(audit)
cs = audit.cs
SEL = cs.Selector()
F = audit.Failure


def test_failures_from_the_artifacts():
    tsv = "tests/sim/test_a.py\t0\t12\ntests/sim/test_b.py\t1\t40\ntests/sim/test_c.py\t2\t-\n"
    assert audit.sim_failures(tsv) == ["tests/sim/test_b.py", "tests/sim/test_c.py"]
    xml = """<testsuites><testsuite>
      <testcase name="test_scenario[ecu/heartbeat-r2d]"/>
      <testcase name="test_scenario[ecu-ams/welded-air-plus]"><failure message="x"/></testcase>
    </testsuite></testsuites>"""
    assert audit.scenario_failures(xml) == ["ecu-ams/welded-air-plus"]
    assert audit.job_failure("smoke (ams)") == ("smoke", "ams")
    assert audit.job_failure("ecu-ams") == ("ifs_hil", "ecu-ams")
    assert audit.job_failure("impact guard") == ("guard", "impact guard")


def test_a_failure_outside_a_full_ci_selection_is_flagged():
    sd = SEL.select(["models/renode/Stm32H7Sdmmc.cs"])
    assert audit.in_selection(F("sim", "tests/sim/test_ams_sd.py"), sd)
    assert not audit.in_selection(F("sim", "tests/sim/test_ecu_radio.py"), sd)
    assert audit.in_selection(F("scenario", "ecu-ams/welded-air-plus"), sd)
    assert not audit.in_selection(F("scenario", "ecu/heartbeat-r2d"), sd)
    assert audit.in_selection(F("smoke", "ams"), sd) and not audit.in_selection(F("smoke", "ecu"), sd)
    assert audit.in_selection(F("guard", "impact guard"), sd)     # always with full-ci
    docs = SEL.select(["docs/ci.md"])
    assert not audit.in_selection(F("sim", "tests/sim/test_ams_sd.py"), docs)
    assert audit.in_selection(F("renode", "renode build"), docs)

    pr = {"number": 7, "title": "fix(models): sd", "labels": [{"name": "full-ci"}], "base": {"ref": "dev"}}
    failures = [F("sim", "tests/sim/test_ecu_radio.py"), F("sim", "tests/sim/test_ams_sd.py")]
    text, flagged = audit.report(__import__("datetime").datetime(2026, 10, 20), [(pr, "impact", sd)],
                                 failures, [], {}, "", set(SEL.markers))
    assert flagged == {7: [F("sim", "tests/sim/test_ecu_radio.py")]}
    assert "GAP" in text
    unlabelled = audit.report(__import__("datetime").datetime(2026, 10, 20), [(pr, "none", sd)],
                              failures, [], {}, "", set(SEL.markers))[1]
    assert unlabelled == {}


def test_the_impact_map_lists_what_a_rule_would_not_select(tmp_path):
    impact = tmp_path / "sim-logs-1" / "impact"
    impact.mkdir(parents=True)
    (impact / "test_ams_sd.systems").write_text("ams\nams\n")
    (impact / "test_ecu_radio.systems").write_text("ecu\n")
    ex = audit.exercised(tmp_path, SEL)
    assert {"systems/ams.yaml", "catalog/firmware/ams.yaml", "models/renode/Stm32H7Sdmmc.cs",
            "catalog/models/sd-card.yaml", "models/renode/IsoSpi.cs"} <= ex["tests/sim/test_ams_sd.py"]
    assert "models/renode/Nrf24l01p.cs" in ex["tests/sim/test_ecu_radio.py"]
    gaps = audit.map_gaps(ex, SEL)
    # The AMS's isoSPI model is exercised by the SD test, which the isoSPI rule
    # leaves to the guard; the SD model selects it.
    assert "tests/sim/test_ams_sd.py" in gaps["models/renode/IsoSpi.cs"]
    assert "models/renode/Stm32H7Sdmmc.cs" not in gaps
    assert "catalog/firmware/ams.yaml" not in gaps
