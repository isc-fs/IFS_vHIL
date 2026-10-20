"""scripts/ci-select.py: CI's test-impact selection over ci/impact.yaml
(docs/ci.md)."""
import configparser
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_select", REPO / "scripts" / "ci-select.py")
cs = importlib.util.module_from_spec(spec)
sys.modules["ci_select"] = cs
spec.loader.exec_module(cs)

SEL = cs.Selector()
ALL_SIM = set(SEL.markers)
BOARDS = {"ecu", "ams", "udv"}


def select(*paths, mode="impact"):
    return SEL.select(list(paths), mode)


def nothing_heavy(sel):
    return not (sel.sim_files or sel.scenarios or sel.smoke or sel.ifs_hil or sel.speed)


def everything(sel):
    return (sel.sim_files == ALL_SIM and sel.scenarios == SEL.scenario_systems
            and sel.smoke == {"ecu", "ams"} and sel.ifs_hil == {"ecu", "ecu-ams", "ams"} and sel.speed)


# -- the event decides the mode ---------------------------------------------------

@pytest.mark.parametrize("event, base, labels, mode", [
    ("pull_request", "dev", [], "none"),
    ("pull_request", "dev", ["documentation"], "none"),
    ("pull_request", "dev", ["full-ci"], "impact"),
    ("pull_request", "dev", ["full-ci", "full-ci-all"], "all"),
    ("pull_request", "dev", ["full-ci-all"], "all"),
    ("pull_request", "main", [], "all"),
    ("schedule", None, [], "all"),
    ("workflow_dispatch", None, [], "all"),
    ("push", None, [], "all"),
])
def test_the_mode_comes_from_the_event_and_labels(event, base, labels, mode):
    assert cs.mode_of(event, base, labels)[0] == mode


def test_an_unlabelled_pr_runs_nothing_heavy_even_for_a_force_all_path():
    sel = select("vhil/sim.py", "docs/ci.md", mode="none")
    assert sel.mode == "none" and not sel.heavy and nothing_heavy(sel)
    out = cs.outputs(sel, False, {})
    assert out["heavy"] == out["guard"] == out["renode_sim"] == "false"
    assert out["sim"] == out["scenarios"] == out["smoke_ecu"] == out["ifs_hil_ecu"] == "false"


def test_full_ci_all_runs_everything_for_a_docs_change():
    assert everything(select("docs/ci.md", mode="all"))


# -- docs only ---------------------------------------------------------------------

def test_docs_only_selects_nothing_heavy_but_the_guard_runs():
    sel = select("docs/ci.md", "README.md", "CLAUDE.md", "docs/proposals/x/y.md")
    assert nothing_heavy(sel) and sel.heavy
    out = cs.outputs(sel, False, {})
    assert out["guard"] == "true" and out["renode_sim"] == "true"
    assert out["sim"] == "false" and out["sim_shards"] == "[0]"
    assert out["smoke"] == "[]" and out["smoke_ecu"] == out["smoke_ams"] == "false"
    assert out["scenarios"] == "false"
    assert out["ifs_hil_any"] == "false" and out["speed"] == "false"


def test_unit_only_code_selects_nothing_heavy():
    assert nothing_heavy(select("tests/unit/test_system.py", "deploy/backup.py", "tests/js/vtable.test.mjs"))


# -- fail safe and force-all ---------------------------------------------------------

@pytest.mark.parametrize("path", [
    "some/new/dir/file.py", "vhil/new_module.py", "models/renode/NewModel.cs", "Makefile",
    "tests/new_suite/test_x.py", "catalog/new-kind/x.yaml",
])
def test_an_unmapped_path_selects_everything(path):
    sel = select("docs/ci.md", path)
    assert everything(sel), sel.as_dict()
    assert sel.matched[path] == ["UNMAPPED: everything"]
    assert path in sel.reason


@pytest.mark.parametrize("path", [
    "docker/renode/patches/0001-x.patch", "docker/renode/pins.env", "platforms/cpus/stm32h733.repl",
    "catalog/platforms/stm32h733.yaml", "catalog/boards/mainlite.yaml",
    "catalog/firmware/can-bootloader.yaml", "vhil/sim.py", "vhil/system.py", "vhil/worker.py",
    "vhil/renode.py", "vhil/broker.py", "vhil/bench.py", "vhil/bench_operator.py",
    "vhil/benchclock.py", "vhil/pytest_plugin.py", ".github/workflows/sim.yml",
    ".github/workflows/new.yml", ".github/actions/setup-renode/action.yml", "ci/impact.yaml",
    "ci/fingerprints.json", "schemas/vhil.schema.json", "scripts/ci-select.py",
    "models/renode/VhilCanBus.cs", "models/renode/Stm32H7Fdcan.cs",
])
def test_a_force_all_path_selects_everything(path):
    sel = select(path)
    assert everything(sel), sel.as_dict()
    assert "everything" in sel.matched[path]
    assert cs.outputs(sel, sel.sim_files == ALL_SIM, cs.durations())["sim_shard_count"] == "8"


# -- mapping -----------------------------------------------------------------------------

def test_a_peripheral_model_selects_its_subsystem_and_the_systems_that_drive_it():
    sel = select("models/renode/Stm32H7Sdmmc.cs")
    assert sel.sim_files == {f for f, m in SEL.markers.items() if "sd" in m}
    assert {"tests/sim/test_ams_sd.py", "tests/sim/test_ams_logfs.py"} <= sel.sim_files
    # Every system with an sd-card device: ams, ecu-ams, ecu-ams-udv.
    assert sel.scenarios == {"ams", "ecu-ams"} and sel.smoke == {"ams"}
    assert sel.ifs_hil == {"ams", "ecu-ams"} and not sel.speed
    assert not any("ecu" in SEL.markers[f] and "ams" not in SEL.markers[f] for f in sel.sim_files)


def test_a_plant_file_selects_the_plant_tests_and_importers_and_the_plant_scenarios():
    sel = select("vhil/plant_ts.py")
    plant = {f for f, m in SEL.markers.items() if "plant" in m}
    assert plant <= sel.sim_files
    assert {"tests/sim/test_plant_ts.py", "tests/sim/test_fmi_plant.py"} <= sel.sim_files
    assert sel.scenarios == {"ecu-ams"}     # every ecu-ams scenario runs a plant
    assert not sel.smoke and not sel.ifs_hil and len(sel.sim_files) < len(ALL_SIM) / 3


def test_a_firmware_entry_selects_its_board_and_every_system_with_it():
    sel = select("catalog/firmware/udv.yaml")
    assert sel.sim_files == {f for f, m in SEL.markers.items() if "udv" in m}
    assert not sel.scenarios and not sel.smoke and not sel.ifs_hil
    ams = select("catalog/firmware/ams.yaml")
    assert ams.sim_files >= {f for f, m in SEL.markers.items() if "ams" in m}
    assert ams.scenarios == {"ams", "ecu-ams"} and ams.smoke == {"ams"}
    assert ams.ifs_hil == {"ams", "ecu-ams"}


def test_a_system_selects_its_tests_and_runs():
    sel = select("systems/ecu-ams.yaml")
    assert sel.sim_files == {f for f, m in SEL.markers.items() if "system_ecu_ams" in m}
    assert sel.scenarios == {"ecu-ams"} and sel.ifs_hil == {"ecu-ams"} and not sel.smoke
    scenario = select("systems/ecu.scenarios/heartbeat-r2d.yaml")
    assert scenario.scenarios == {"ecu"} and not scenario.sim_files


def test_a_test_file_selects_itself_and_its_importers():
    sel = select("tests/sim/test_plant_ts.py")
    assert sel.sim_files == {"tests/sim/test_plant_ts.py", "tests/sim/test_fmi_plant.py"}
    helper = select("tests/sim/ams_car.py")
    assert helper.sim_files and all(f.startswith("tests/sim/test_") for f in helper.sim_files)
    assert select("tests/ecu_smoke.robot").smoke == {"ecu"}


def test_the_scenario_suite_and_the_run_path_select_every_scenario():
    assert select("tests/scenarios/test_scenarios.py").scenarios == SEL.scenario_systems
    assert select("vhil/expect.py").scenarios == SEL.scenario_systems


def test_editor_paths_select_the_editor_check_in_every_mode():
    for mode in ("none", "impact", "all"):
        assert select("editor/pipeline-manager/x.ts", mode=mode).editor
        assert select("vhil/server/app.py", mode=mode).editor
        assert not select("vhil/plant_ts.py", mode=mode).editor
    assert nothing_heavy(select("editor/pipeline-manager/x.ts"))


def test_selections_add_up():
    a, b = select("models/renode/Stm32H7Sdmmc.cs"), select("catalog/firmware/udv.yaml")
    both = select("models/renode/Stm32H7Sdmmc.cs", "catalog/firmware/udv.yaml")
    assert both.sim_files == a.sim_files | b.sim_files
    assert both.scenarios == a.scenarios | b.scenarios


# -- shards ------------------------------------------------------------------------------

def test_a_small_selection_needs_fewer_shards():
    durs = cs.durations()
    assert cs.shards(set(), False, durs) == 0
    assert cs.shards({"tests/sim/test_adc3.py"}, False, durs) == 1
    assert cs.shards(ALL_SIM, True, durs) == 8
    half = set(sorted(ALL_SIM)[: len(ALL_SIM) // 2])
    assert 1 < cs.shards(half, False, durs) <= 8


def test_the_balancer_splits_only_the_selected_files():
    files = "tests/sim/test_adc3.py tests/sim/test_ams_sd.py tests/sim/test_ams_logfs.py"
    got = []
    for k in (1, 2):
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "sim-balance.py"), "2", str(k),
                              "--files", files], check=True, capture_output=True, text=True).stdout
        got += out.split()
    assert sorted(got) == sorted(files.split())


# -- outputs and the CLI ---------------------------------------------------------------------

def test_the_cli_writes_job_outputs(tmp_path):
    out = tmp_path / "out"
    summary = tmp_path / "summary.md"
    cs.main(["--event", "pull_request", "--base-ref", "dev", "--labels",
             json.dumps([{"name": "full-ci"}]), "--changed", "models/renode/Stm32H7Sdmmc.cs",
             "--github-output", str(out), "--summary", str(summary)])
    kv = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert kv["mode"] == "impact" and kv["heavy"] == "true"
    assert kv["smoke_ams"] == "true" and kv["smoke_ecu"] == "false"
    assert json.loads(kv["sim_shards"]) == [1]
    assert "Stm32H7Sdmmc.cs" in summary.read_text()


def test_a_diff_that_fails_selects_everything(tmp_path):
    out = tmp_path / "out"
    cs.main(["--event", "pull_request", "--base-ref", "dev", "--labels", "full-ci",
             "--diff", "no-such-ref", "HEAD", "--github-output", str(out)])
    kv = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert kv["mode"] == "all" and kv["sim_shard_count"] == "8" and kv["editor"] == "true"


# -- the markers the selection reads ------------------------------------------------------------

def _registered():
    ini = configparser.ConfigParser()
    ini.read(REPO / "pytest.ini")
    return {line.split(":")[0].strip() for line in ini["pytest"]["markers"].splitlines() if line.strip()}


SUBSYSTEMS = {"can", "bootloader", "flash", "adc", "dma", "sd", "isospi", "i2c", "radio", "plant",
              "timers", "cpu_timing", "fast_forward", "fault_hooks", "reset", "live_session", "server",
              "debugger", "broker", "probe"}
BUILTIN = {"skipif", "xfail", "parametrize", "usefixtures", "filterwarnings", "skip"}


@pytest.mark.parametrize("path", sorted(SEL.markers))
def test_every_native_test_file_is_tagged(path):
    marks = SEL.markers[path] - BUILTIN
    assert marks <= _registered(), marks - _registered()
    systems = {m.removeprefix("system_").replace("_", "-") for m in marks if m.startswith("system_")}
    assert systems, f"{path}: no system_<id> marker"
    assert marks & SUBSYSTEMS, f"{path}: no subsystem marker"
    # A board marker for exactly the roles its systems place.
    roles = set().union(*(SEL.systems[s]["roles"] for s in systems))
    assert marks & BOARDS == roles, f"{path}: boards {marks & BOARDS} vs systems' roles {roles}"


def test_every_rule_target_and_glob_is_valid():
    for name, globs, target in SEL.rules:
        assert globs and set(target) <= cs.TARGET_KEYS, name
        for m in target.get("markers") or []:
            assert m in _registered(), (name, m)
        for s in target.get("systems", []) + target.get("runs", []):
            assert "{" in s or s in SEL.systems, (name, s)
        for d in target.get("devices") or []:
            assert (REPO / "catalog" / "models" / f"{d}.yaml").exists(), (name, d)


def test_globs():
    rx = cs.glob_regex
    assert rx("docs/**").match("docs/a/b.md") and not rx("docs/**").match("docsx/a")
    assert rx("*.md").match("README.md") and not rx("*.md").match("docs/x.md")
    assert rx("vhil/bench*.py").match("vhil/bench_operator.py")
    m = rx("systems/{system}.scenarios/**").match("systems/ecu-ams.scenarios/x.yaml")
    assert m and m.group("system") == "ecu-ams"
    assert not rx("tests/sim/{file}.py").match("tests/sim/sub/x.py")
