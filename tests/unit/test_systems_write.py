"""vhil.server (M5.4, #116): save systems to branches, open PRs, pick firmware refs.

Every test works on a throwaway clone of a throwaway bare remote seeded with
this repo's catalogue and systems; nothing here touches this checkout's
branches or any real remote, and PRs go to a fake host.
"""
import shutil
import subprocess
import sys

import pytest
import yaml

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vhil import editor  # noqa: E402
from vhil.server import create_app  # noqa: E402
from vhil.server.config import Settings  # noqa: E402
from vhil.server.githost import (CachedRefs, FakeGitHost, FakeRefLister,  # noqa: E402
                                 GitHubHost)
from vhil.system import REPO  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=seed", "-c",
                           "user.email=seed@example.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """(workspace clone, bare remote): dev holds this repo's catalog/ and systems/."""
    seed = tmp_path / "seed"
    for d in ("catalog", "systems"):
        shutil.copytree(REPO / d, seed / d)
    git(seed, "init", "-q", "-b", "dev")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", "-q", str(bare), str(ws)], check=True)
    return ws, bare


REFS = {"branches": [{"name": n, "sha": c * 40} for n, c in (("dev", "a"), ("feat/x", "b"),
                                                              ("main", "c"))],
        "tags": [{"name": "v1.0.0", "sha": "d" * 40}]}


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(remote, tmp_path):
    ws, bare = remote
    app = create_app(Settings(workspace=ws, db=tmp_path / "vhil.db", results=tmp_path / "runs",
                              auth="dev"))
    app.state.git_host = FakeGitHost()
    clock = Clock()
    lister = FakeRefLister({"isc-fs/IFS08-CE-ECU": REFS})
    app.state.ref_lister = CachedRefs(lister, ttl_s=60, clock=clock)
    app.state.fw_dir = tmp_path / "fw"          # no builds, whatever the host has
    return type("Env", (), dict(client=TestClient(app), app=app, ws=ws, bare=bare,
                                lister=lister, clock=clock))


def snapshot(ws):
    """What the shared checkout looks like: must not change on a save."""
    return (git(ws, "rev-parse", "HEAD"), git(ws, "rev-parse", "dev"),
            git(ws, "status", "--porcelain"), git(ws, "branch", "--show-current"),
            (ws / "systems" / "ams.yaml").read_text())


def edited_ams(ws, ref="feat/x"):
    text = (ws / "systems" / "ams.yaml").read_text()
    doc = yaml.safe_load(text)
    doc["boards"]["ams"]["firmware_ref"] = ref
    return editor.write_system(doc, text)


def put(env, system_id, **body):
    body.setdefault("message", "test: save")
    return env.client.put(f"/api/systems/{system_id}", json=body)


# -- save ---------------------------------------------------------------------------

def test_a_save_commits_on_a_new_branch_and_leaves_the_checkout_alone(env):
    before = snapshot(env.ws)
    text = edited_ams(env.ws)
    r = put(env, "ams", yaml=text, branch="feat/ams-ref", message="feat(systems): pin ams")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["branch"] == "feat/ams-ref" and out["created"] and out["changed"]
    assert git(env.ws, "rev-parse", "feat/ams-ref") == out["ref"]
    assert git(env.ws, "show", "feat/ams-ref:systems/ams.yaml") + "\n" == text
    assert git(env.ws, "rev-parse", f"{out['ref']}^") == git(env.ws, "rev-parse", "origin/dev")
    assert git(env.ws, "log", "-1", "--format=%an <%ae>|%s", out["ref"]) == \
        "vHIL dev <vhil-dev@localhost>|feat(systems): pin ams"
    # Only the one file changed, and nothing reached the remote.
    assert git(env.ws, "diff", "--name-only", "origin/dev", out["ref"]) == "systems/ams.yaml"
    assert "feat/ams-ref" not in git(env.bare, "branch")
    assert snapshot(env.ws) == before


def test_saves_stack_on_the_branch(env):
    first = put(env, "ams", yaml=edited_ams(env.ws, "feat/a"), branch="feat/s").json()
    second = put(env, "ams", yaml=edited_ams(env.ws, "feat/b"), branch="feat/s").json()
    assert not second["created"] and second["changed"]
    assert git(env.ws, "rev-parse", f"{second['ref']}^") == first["ref"]
    again = put(env, "ams", yaml=edited_ams(env.ws, "feat/b"), branch="feat/s").json()
    assert again["ref"] == second["ref"] and not again["changed"]


def test_an_unchanged_system_makes_no_commit(env):
    text = (env.ws / "systems" / "ams.yaml").read_text()
    out = put(env, "ams", yaml=text, branch="feat/same").json()
    assert not out["changed"] and out["ref"] == git(env.ws, "rev-parse", "origin/dev")
    assert "feat/same" not in git(env.ws, "branch")


def test_dev_mode_takes_the_author_from_the_request(env):
    out = put(env, "ams", yaml=edited_ams(env.ws), branch="feat/who",
              author={"name": "Ada", "email": "ada@example.com"}).json()
    assert git(env.ws, "log", "-1", "--format=%an <%ae>", out["ref"]) == "Ada <ada@example.com>"


@pytest.mark.parametrize("body, message", [
    ("kind: system\nid: ams\nboards: [unclosed\n", "not YAML"),
    ("- a\n- list\n", "YAML mapping"),
    ("kind: system\nid: other\nboards:\n  a: {board: mainlite, role: ams}\n",
     "does not match the file name"),
    ("kind: system\nid: ams\nboards: {}\n", "should be non-empty"),
    ("kind: system\nid: ams\nwheels: 4\nboards:\n  a: {board: mainlite, role: ams}\n",
     "wheels"),
    ("kind: system\nid: ams\nboards:\n  a: {board: no-such-board, firmware: ams}\n",
     "no board 'no-such-board'"),
    ("kind: system\nid: ams\nboards:\n  a: {board: mainlite, role: ams}\n"
     "buses:\n  b: {kind: can, nodes: [a.FDCAN9]}\n", "no connector or pin 'FDCAN9'"),
])
def test_invalid_systems_are_422_with_the_reason_and_save_nothing(env, body, message):
    before = snapshot(env.ws)
    r = put(env, "ams", yaml=body, branch="feat/bad")
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert any(message in e for e in errors), errors
    assert "feat/bad" not in git(env.ws, "branch")
    assert snapshot(env.ws) == before


@pytest.mark.parametrize("branch", ["dev", "main", "-x", "a..b", "feat/x.lock", "x y", ""])
def test_protected_and_malformed_branches_are_refused(env, branch):
    before = snapshot(env.ws)
    r = put(env, "ams", yaml=edited_ams(env.ws), branch=branch)
    assert r.status_code == 422
    assert git(env.ws, "rev-parse", "dev") == before[1]


def test_a_branch_checked_out_in_the_workspace_is_refused(env):
    git(env.ws, "checkout", "-q", "-b", "feat/live")
    r = put(env, "ams", yaml=edited_ams(env.ws), branch="feat/live")
    assert r.status_code == 409 and "checked out" in r.text


def test_a_save_needs_a_message(env):
    assert put(env, "ams", yaml=edited_ams(env.ws), branch="feat/m", message=" ").status_code == 422


def test_bad_ids_are_refused(env):
    assert put(env, "AMS", yaml="x", branch="feat/x").status_code == 422
    assert env.client.post("/api/systems", json={"id": "../x", "branch": "feat/x"}).status_code == 422


def test_a_saved_system_validates_with_the_cli(env, tmp_path):
    """The file a save writes is the one CI runs: `python -m vhil.system validate`."""
    out = put(env, "ams", yaml=edited_ams(env.ws), branch="feat/ci").json()
    path = tmp_path / "ams.yaml"
    path.write_text(git(env.ws, "show", f"{out['ref']}:systems/ams.yaml") + "\n")
    r = subprocess.run([sys.executable, "-m", "vhil.system", "validate", str(path)],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert yaml.safe_load(path.read_text())["boards"]["ams"]["firmware_ref"] == "feat/x"


# -- new systems ----------------------------------------------------------------------

def test_a_new_system_starts_from_a_valid_template(env):
    r = env.client.post("/api/systems", json={"id": "bench-one", "branch": "feat/new"})
    assert r.status_code == 201, r.text
    text = git(env.ws, "show", "feat/new:systems/bench-one.yaml")
    assert yaml.safe_load(text)["id"] == "bench-one"
    assert git(env.ws, "log", "-1", "--format=%s", "feat/new") == "feat(systems): add bench-one"
    again = env.client.post("/api/systems", json={"id": "bench-one", "branch": "feat/new"})
    assert again.status_code == 409


def test_a_new_system_may_not_overwrite_one(env):
    r = env.client.post("/api/systems", json={"id": "ams", "branch": "feat/new",
                                              "yaml": edited_ams(env.ws)})
    assert r.status_code == 409 and "already exists" in r.text


# -- the editor's graph ----------------------------------------------------------------

def test_a_system_comes_back_as_the_editors_graph_and_saves_back_identical(env):
    g = env.client.get("/api/systems/ecu-ams/dataflow").json()
    assert g["exists"] and g["errors"] == []
    assert {n["instanceName"] for n in g["dataflow"]["graphs"][0]["nodes"]} >= {"ecu", "ams"}
    out = put(env, "ecu-ams", dataflow=g["dataflow"], branch="feat/rt").json()
    assert not out["changed"]


def test_a_graph_edit_saves_as_a_file_edit(env):
    g = env.client.get("/api/systems/ams/dataflow").json()["dataflow"]
    node = next(n for n in g["graphs"][0]["nodes"] if n["instanceName"] == "ams")
    next(p for p in node["properties"] if p["name"] == "firmware_ref")["value"] = "feat/x"
    out = put(env, "ams", dataflow=g, branch="feat/graph").json()
    saved = git(env.ws, "show", f"{out['ref']}:systems/ams.yaml") + "\n"
    assert saved == edited_ams(env.ws)      # comments and layout kept
    on_branch = env.client.get("/api/systems/ams/dataflow", params={"branch": "feat/graph"}).json()
    assert on_branch["yaml"] == saved and on_branch["ref"] == out["ref"]


def test_a_graph_without_additional_data_keeps_the_files_other_parts(env):
    """Pipeline Manager 0.5.2's graph_get drops the graph's additionalData
    (id, description, bench, the source text): the save puts the file's back."""
    g = env.client.get("/api/systems/ams/dataflow").json()["dataflow"]
    graph = g["graphs"][0]
    del graph["additionalData"]
    graph["name"] = "whatever the editor calls it"
    node = next(n for n in graph["nodes"] if n["instanceName"] == "ams")
    next(p for p in node["properties"] if p["name"] == "firmware_ref")["value"] = "feat/x"
    out = put(env, "ams", dataflow=g, branch="feat/pm").json()
    assert git(env.ws, "show", f"{out['ref']}:systems/ams.yaml") + "\n" == edited_ams(env.ws)


def test_a_broken_graph_is_422(env):
    g = env.client.get("/api/systems/ams/dataflow").json()["dataflow"]
    g["graphs"][0]["nodes"][0]["name"] = "no-such-type"
    r = put(env, "ams", dataflow=g, branch="feat/broken")
    assert r.status_code == 422 and "not a system" in r.text


def test_preview_translates_and_validates_without_saving(env):
    g = env.client.get("/api/systems/ams/dataflow").json()["dataflow"]
    p = env.client.post("/api/systems/ams/preview", json={"dataflow": g}).json()
    assert p["errors"] == [] and p["yaml"] == (env.ws / "systems" / "ams.yaml").read_text()
    bad = env.client.post("/api/systems/ams/preview", json={"yaml": "kind: system\nid: ams\n"})
    assert bad.json()["errors"]


def test_an_unrouted_pin_warns_but_saves(env):
    """Wiring a pin the role's backplane leaves unconnected is a warning:
    Check shows it, and a save goes through with it."""
    text = (env.ws / "systems" / "ams.yaml").read_text()
    doc = yaml.safe_load(text)
    doc["buses"]["can_x"] = {"kind": "can", "nodes": ["ams.FDCAN3"]}
    text = editor.write_system(doc, text)
    warning = "ams: FDCAN3 is not connected on the AMS backplane (docs/backplanes/ams.md)"
    p = env.client.post("/api/systems/ams/preview", json={"yaml": text}).json()
    assert p["errors"] == [] and p["warnings"] == [warning]
    r = put(env, "ams", yaml=text, branch="feat/warned", message="feat(systems): fdcan3")
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == [warning]
    d = env.client.get("/api/systems/ams/dataflow", params={"branch": "feat/warned"}).json()
    assert d["errors"] == [] and d["warnings"] == [warning]


def test_unknown_systems_are_404_unless_new(env):
    assert env.client.get("/api/systems/nope/dataflow").status_code == 404
    t = env.client.get("/api/systems/nope/dataflow", params={"new": True}).json()
    assert not t["exists"] and t["errors"] == []


# -- PRs ------------------------------------------------------------------------------

def test_a_pr_pushes_the_branch_and_opens_against_dev(env):
    put(env, "ams", yaml=edited_ams(env.ws), branch="feat/pr")
    r = env.client.post("/api/systems/ams/pr", json={"branch": "feat/pr", "title": "Pin ams",
                                                     "body": "Why."})
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://github.example/pr/")
    host = env.app.state.git_host
    assert host.pushed == ["feat/pr"]
    assert host.prs == [{"branch": "feat/pr", "base": "dev", "title": "Pin ams", "body": "Why."}]


def test_a_pr_needs_a_saved_branch(env):
    r = env.client.post("/api/systems/ams/pr", json={"branch": "feat/none", "title": "x"})
    assert r.status_code == 404
    r = env.client.post("/api/systems/ams/pr", json={"branch": "dev", "title": "x"})
    assert r.status_code == 422


def test_without_credentials_a_pr_is_409_and_nothing_is_pushed(env, monkeypatch):
    env.app.state.git_host = GitHubHost(token=None, repo="isc-fs/IFS_vHIL")
    put(env, "ams", yaml=edited_ams(env.ws), branch="feat/notoken")
    r = env.client.post("/api/systems/ams/pr", json={"branch": "feat/notoken", "title": "x"})
    assert r.status_code == 409 and "VHIL_GITHUB_TOKEN" in r.text
    assert "feat/notoken" not in git(env.bare, "branch")
    assert env.client.get("/api/config").json()["can_open_pr"] is False


def test_the_github_app_token_is_used_when_configured(env):
    from types import SimpleNamespace

    from vhil.server import systems_write
    from vhil.server.githost import HostError

    asked = []

    class App:
        def token_for(self, repo, *, write=False):
            asked.append((repo, write))
            return "ghs_app"

    st = env.app.state
    st.git_host, st.github_app = None, App()
    host = systems_write._host(SimpleNamespace(app=env.app))
    assert isinstance(host, GitHubHost)
    assert host._need_token() == "ghs_app" and asked == [(host.repo, True)]
    assert env.client.get("/api/config").json()["can_open_pr"] is True

    def broken():
        raise RuntimeError("not installed")
    with pytest.raises(HostError, match="not installed"):
        GitHubHost(token=broken, repo="isc-fs/IFS_vHIL")._need_token()


# -- firmware picker -----------------------------------------------------------------

def test_firmware_lists_the_catalogue_sources(env):
    fw = {f["id"]: f for f in env.client.get("/api/firmware").json()}
    assert set(fw) == {"ams", "ecu", "can-bootloader"}
    assert fw["ecu"]["repo"] == "isc-fs/IFS08-CE-ECU" and fw["ecu"]["ref"] == "dev"
    assert fw["ecu"]["build"]["elf"] == "build/ECU08.elf"


def test_firmware_refs_come_from_ls_remote_cached(env):
    r = env.client.get("/api/firmware/ecu/refs").json()
    assert r == {"id": "ecu", "repo": "isc-fs/IFS08-CE-ECU", "default": "dev",
                 "default_built": False,
                 "branches": [{**b, "built": False} for b in REFS["branches"]],
                 "tags": [{**t, "built": False} for t in REFS["tags"]]}
    env.client.get("/api/firmware/ecu/refs")
    assert env.lister.calls == ["isc-fs/IFS08-CE-ECU"]
    env.clock.t = 61
    env.client.get("/api/firmware/ecu/refs")
    assert len(env.lister.calls) == 2


def test_firmware_refs_say_which_are_built(env, tmp_path):
    """A ref is built when the firmware volume's built.txt lists its image
    and the file is there: the worker reuses it (else the first run builds)."""
    fw = tmp_path / "fw"
    elf = fw / "ecu@feat_x" / "build" / "ECU08.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"\x7fELF")
    (fw / "built.txt").write_text(f"ecu={elf}\necu={fw}/ecu@dev/build/ECU08.elf\n")
    env.app.state.fw_dir = fw
    r = env.client.get("/api/firmware/ecu/refs").json()
    assert {b["name"]: b["built"] for b in r["branches"]} == {
        "dev": False, "feat/x": True, "main": False}   # dev: listed, but no file
    assert r["default_built"] is False


@pytest.mark.parametrize("ident", ["..", "ECU", "a b", "-x", "ecu%2F..", "e" * 300])
def test_firmware_refs_take_only_a_catalogue_id(env, ident):
    r = env.client.get(f"/api/firmware/{ident}/refs")
    assert r.status_code in (404, 422) and not env.lister.calls


def test_firmware_refs_errors(env):
    assert env.client.get("/api/firmware/nope/refs").status_code == 404
    r = env.client.get("/api/firmware/ams/refs")     # not in the fake: ls-remote fails
    assert r.status_code == 502 and "IFS08-CE-AMS" in r.text


def test_config_names_the_editor(env, monkeypatch):
    """Same origin, under /editor/, whatever a deployment's old
    VHIL_EDITOR_URL says."""
    monkeypatch.setenv("VHIL_EDITOR_URL", "http://editor.example:5050")
    c = env.client.get("/api/config").json()
    assert c["editor_url"] == "/editor/" and c["base_branch"] == "dev"
    assert c["can_open_pr"] is True     # the fake host


def test_the_editor_page_is_served(env):
    assert env.client.get("/static/editor.js").status_code == 200


# -- runs at a saved ref --------------------------------------------------------------

def saved_ams(env, branch="feat/ams-run", fw_ref="feat/x", description="saved from the editor",
              **kw):
    text = (env.ws / "systems" / "ams.yaml").read_text()
    doc = yaml.safe_load(text)
    doc["boards"]["ams"]["firmware_ref"] = fw_ref
    doc["description"] = description
    r = put(env, "ams", yaml=editor.write_system(doc, text), branch=branch, **kw)
    assert r.status_code == 200, r.text
    return r.json()["ref"]


def run_scenario(**kw):
    return {"kind": "run", "virtual_ms": 100, **kw}


def test_a_run_at_a_saved_branch_runs_that_file_and_leaves_the_checkout_alone(env, tmp_path):
    from vhil.server.runs import RunStore
    from vhil.worker import FirmwareResolver, Worker
    from .test_runs import FakeSim

    before = snapshot(env.ws)
    sha = saved_ams(env)
    r = env.client.post("/api/runs", json={"system": "ams", "ref": "feat/ams-run",
                                           "scenario": run_scenario()})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    run = env.client.get(f"/api/runs/{run_id}").json()
    assert run["ref"] == sha and run["ref_name"] == "feat/ams-run"

    seen = {}

    class Resolver(FirmwareResolver):
        def resolve(self, system, refs):
            seen["expected"] = self.expected(system, refs)
            return {k: tmp_path / f"{k}.elf" for k in system.images()}

    def factory(path, firmware, log_path):
        seen["path"] = path
        seen["text"] = path.read_text()
        return FakeSim(system=path)

    settings = env.app.state.settings
    worker = Worker(settings, worker_id="w", sim_factory=factory, resolver=Resolver(tmp_path))
    assert worker.run_once() == run_id
    run = RunStore(settings.db).get(run_id)
    assert run["state"] == "passed", run["summary"]
    assert run["summary"]["system_ref"] == sha
    # The branch's file, written into the run's directory, not the checkout's.
    assert "saved from the editor" in seen["text"]
    assert seen["path"] == settings.results / str(run_id) / "system" / "ams.yaml"
    assert "saved from the editor" not in (env.ws / "systems" / "ams.yaml").read_text()
    # The saved board firmware_ref decides which image the worker resolves.
    assert seen["expected"]["ams"][0] == "feat/x"
    assert "ams@feat_x" in str(seen["expected"]["ams"][1])
    trace = (settings.results / str(run_id) / "trace.jsonl").read_text()
    assert f"at feat/ams-run ({sha[:12]})" in trace
    assert snapshot(env.ws) == before


def test_a_run_without_a_ref_runs_the_workspace_head(env):
    head = git(env.ws, "rev-parse", "HEAD")
    r = env.client.post("/api/runs", json={"system": "ams", "scenario": run_scenario()})
    assert r.status_code == 201, r.text
    run = env.client.get(f"/api/runs/{r.json()['run_id']}").json()
    assert run["ref"] == head and run["ref_name"] == ""


def test_a_ref_given_as_tag_or_short_sha_resolves(env):
    sha = saved_ams(env)
    git(env.ws, "tag", "t1", sha)
    for ref in ("t1", sha[:10]):
        r = env.client.post("/api/runs", json={"system": "ams", "ref": ref, "scenario": run_scenario()})
        assert r.status_code == 201, r.text
        assert env.client.get(f"/api/runs/{r.json()['run_id']}").json()["ref"] == sha


def test_runs_at_refs_that_would_not_run_as_in_ci_are_refused(env):
    from vhil.server.gitstore import GitStore
    saved_ams(env)
    store = GitStore(env.ws)
    who = ("t", "t@example.com")
    store.commit_file("feat/code", "catalog/firmware/ams.yaml", "kind: firmware\n", "test: code", who)
    saved_ams(env, branch="feat/bad")
    store.commit_file("feat/bad", "systems/ams.yaml", "kind: system\nid: ams\n", "test: break", who)
    for body, needle in (
            ({"ref": "feat/nope"}, "no ref 'feat/nope'"),
            ({"ref": "--upload-pack=x"}, "plain git ref"),
            ({"ref": "feat/code"}, "catalog/firmware/ams.yaml"),
            ({"ref": "feat/bad"}, "does not validate"),
            ({"ref": "feat/ams-run", "system": "nope"}, "no system 'nope' at feat/ams-run"),
            ({"ref": "feat/ams-run", "scenario": {"kind": "pytest",
                                                 "select": "tests/sim/test_probe.py"}}, "pytest")):
        body = {"system": "ams", "scenario": run_scenario(), **body}
        r = env.client.post("/api/runs", json=body)
        assert r.status_code == 422 and needle in r.text, (body, r.text)


def test_a_worker_refuses_a_ref_the_workspace_code_moved_away_from(env, tmp_path):
    """Queued at a ref, then the workspace's catalogue moved (a pull): the run
    would no longer be the one CI makes, so it errors instead of running."""
    from vhil.server.runs import RunStore
    from vhil.worker import Worker
    from .test_runs import FakeSim, FixedResolver
    sha = saved_ams(env)
    run_id = env.client.post("/api/runs", json={"system": "ams", "ref": "feat/ams-run",
                                                "scenario": run_scenario()}).json()["run_id"]
    (env.ws / "catalog" / "firmware" / "ams.yaml").write_text(
        (env.ws / "catalog" / "firmware" / "ams.yaml").read_text() + "# moved\n")
    git(env.ws, "commit", "-qam", "move the catalogue")
    settings = env.app.state.settings
    Worker(settings, sim_factory=lambda *a: FakeSim(), resolver=FixedResolver()).run_once()
    run = RunStore(settings.db).get(run_id)
    assert run["state"] == "error" and "catalog/firmware/ams.yaml" in run["summary"]["error"]
    assert sha[:12] in run["summary"]["error"]


# -- save then run, with the deployment pinned behind dev's tip ----------------------

def advance_dev(env, tmp_path):
    """Move the remote's dev past the workspace's commit with a catalogue
    change, and fetch it: the workspace stays at its (older, pinned) commit,
    as a deployment at VHIL_WORKSPACE_REF does while dev moves on."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(env.bare), str(other)], check=True)
    fw = other / "catalog" / "firmware" / "ams.yaml"
    fw.write_text(fw.read_text() + "# newer on dev\n")
    git(other, "commit", "-qam", "catalogue moves on dev")
    git(other, "push", "-q", "origin", "dev")
    git(env.ws, "fetch", "-q")
    assert git(env.ws, "rev-parse", "HEAD") != git(env.ws, "rev-parse", "origin/dev")


def opened_at(env, system="ams"):
    """The commit the editor opens the checked-out tree's system at."""
    return env.client.get(f"/api/systems/{system}/dataflow").json()["ref"]


def test_a_new_branch_builds_on_the_commit_the_save_names_as_its_base(env, tmp_path):
    advance_dev(env, tmp_path)
    head = git(env.ws, "rev-parse", "HEAD")
    assert opened_at(env) == head
    sha = saved_ams(env, branch="feat/from-head", base=head)
    assert git(env.ws, "rev-parse", f"{sha}^") == head
    assert git(env.ws, "diff", "--name-only", head, sha) == "systems/ams.yaml"
    # An existing branch stacks on its tip, whatever the base.
    again = saved_ams(env, branch="feat/from-head", description="again",
                      base=git(env.ws, "rev-parse", "origin/dev"))
    assert git(env.ws, "rev-parse", f"{again}^") == sha
    # Without one, a new branch still starts at the base branch's tip.
    other = saved_ams(env, branch="feat/from-dev")
    assert git(env.ws, "rev-parse", f"{other}^") == git(env.ws, "rev-parse", "origin/dev")


@pytest.mark.parametrize("base", ["dev", "--upload-pack=x", "0" * 40, "zz" * 20])
def test_a_base_that_is_not_a_workspace_commit_is_422(env, base):
    r = put(env, "ams", yaml=edited_ams(env.ws), branch="feat/bad-base", base=base)
    assert r.status_code == 422 and "is not a commit of the workspace" in r.text, r.text
    assert "feat/bad-base" not in git(env.ws, "branch")


def test_save_then_run_works_with_the_deployment_behind_dev(env, tmp_path):
    """The editor's main flow (#164): open the checked-out tree's system,
    save it to a new branch, run that commit. The branch is made from the
    commit the system was opened at, so only its system file differs from
    the deployment's code, and the run is queued and runs."""
    from vhil.server.runs import RunStore
    from vhil.worker import Worker
    from .test_runs import FakeSim, FixedResolver
    advance_dev(env, tmp_path)
    sha = saved_ams(env, branch="feat/editor-save", base=opened_at(env))
    r = env.client.post("/api/runs", json={"system": "ams", "ref": sha,
                                           "scenario": run_scenario()})
    assert r.status_code == 201, r.text
    settings = env.app.state.settings
    Worker(settings, sim_factory=lambda *a: FakeSim(), resolver=FixedResolver()).run_once()
    run = RunStore(settings.db).get(r.json()["run_id"])
    assert run["state"] == "passed", run["summary"]


def test_a_branch_from_devs_tip_is_refused_with_why_and_what_to_do(env, tmp_path):
    """A branch made from dev's tip changes no code itself, but dev moved
    past the deployment's commit: it would run dev's tip's system with the
    deployment's code. Refused, saying so and what to do instead."""
    advance_dev(env, tmp_path)
    saved_ams(env, branch="feat/from-dev")
    r = env.client.post("/api/runs", json={"system": "ams", "ref": "feat/from-dev",
                                           "scenario": run_scenario()})
    assert r.status_code == 422, r.text
    msg = r.json()["detail"]
    head = git(env.ws, "rev-parse", "HEAD")
    fork = git(env.ws, "rev-parse", "origin/dev")
    assert "catalog/firmware/ams.yaml" in msg
    assert "changes no code itself" in msg and f"built on dev at {fork[:12]}" in msg
    assert f"not on the workspace's {head[:12]}" in msg
    assert "new branch from the editor" in msg


def test_a_branch_that_changes_code_itself_is_still_refused(env, tmp_path):
    """Only the system file may differ: a branch made from the workspace's
    commit that also changes a model or the catalogue is refused, so a run
    never mixes a ref's code with the deployment's."""
    from vhil.server.gitstore import GitStore
    advance_dev(env, tmp_path)
    head = git(env.ws, "rev-parse", "HEAD")
    saved_ams(env, branch="feat/code-too", base=head)
    GitStore(env.ws).commit_file("feat/code-too", "catalog/boards/extra.yaml", "x: 1\n",
                                 "test: code", ("t", "t@example.com"))
    r = env.client.post("/api/runs", json={"system": "ams", "ref": "feat/code-too",
                                           "scenario": run_scenario()})
    assert r.status_code == 422, r.text
    msg = r.json()["detail"]
    assert "catalog/boards/extra.yaml" in msg and "changes that code itself" in msg


def test_workspace_refs_list_branches_and_tags(env):
    saved_ams(env)
    git(env.ws, "tag", "v9")
    got = env.client.get("/api/workspace/refs").json()
    assert got["head"] == git(env.ws, "rev-parse", "HEAD")
    assert {"dev", "feat/ams-run"} <= set(got["branches"]) and got["tags"] == ["v9"]


# -- branch ownership (github mode) ----------------------------------------------------

@pytest.fixture
def gh(remote, tmp_path, monkeypatch):
    """The app in github mode over the throwaway workspace; .user(login) is a
    client signed in as that member. carol is an admin."""
    ws, bare = remote
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_ID", "client-id")
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("VHIL_SESSION_SECRET", "test-session-secret-0123456789abcdef")
    monkeypatch.delenv("VHIL_PUBLIC_URL", raising=False)
    monkeypatch.delenv("VHIL_GITHUB_APP_ID", raising=False)
    app = create_app(Settings(workspace=ws, db=tmp_path / "vhil.db", results=tmp_path / "runs",
                              auth="github", admins=frozenset({"carol"})))
    app.state.git_host = FakeGitHost()

    def user(login):
        c = TestClient(app)
        cookie, session = app.state.auth.new_session({"login": login, "name": login.title(),
                                                      "avatar_url": ""})
        c.cookies.set("vhil_session", cookie)
        c.headers["X-CSRF-Token"] = app.state.auth.csrf_token(session)
        return c
    return type("Gh", (), dict(app=app, ws=ws, user=staticmethod(user)))


def gh_put(client, ws, branch, ref, **body):
    return client.put("/api/systems/ams", json={"yaml": edited_ams(ws, ref), "branch": branch,
                                                "message": "test: save", **body})


def test_a_save_records_its_saver_as_a_trailer(gh):
    out = gh_put(gh.user("alice"), gh.ws, "feat/own", "feat/a").json()
    assert git(gh.ws, "log", "-1", "--format=%(trailers:key=Vhil-User,valueonly)",
               out["ref"]).strip() == "alice"
    assert git(gh.ws, "log", "-1", "--format=%an <%ae>", out["ref"]) == \
        "Alice <alice@users.noreply.github.com>"


def test_only_the_last_saver_moves_a_branch_unless_taking_over(gh):
    alice, bob = gh.user("alice"), gh.user("bob")
    first = gh_put(alice, gh.ws, "feat/own", "feat/a").json()
    assert gh_put(alice, gh.ws, "feat/own", "feat/b").status_code == 200    # still hers
    tip = git(gh.ws, "rev-parse", "feat/own")
    r = gh_put(bob, gh.ws, "feat/own", "feat/c")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["owner"] == "alice" and detail["takeover"] is True
    assert "alice" in detail["errors"][0]
    assert git(gh.ws, "rev-parse", "feat/own") == tip, "a refused save moved the branch"
    out = gh_put(bob, gh.ws, "feat/own", "feat/c", takeover=True).json()
    trailers = git(gh.ws, "log", "-1", "--format=%(trailers)", out["ref"])
    assert "Vhil-User: bob" in trailers and "Vhil-Takeover-From: alice" in trailers
    # Now bob's: alice is the one who needs a takeover.
    assert gh_put(alice, gh.ws, "feat/own", "feat/d").status_code == 409
    assert first["created"]


def test_an_admin_saves_over_anyones_branch(gh):
    gh_put(gh.user("alice"), gh.ws, "feat/own", "feat/a")
    out = gh_put(gh.user("Carol"), gh.ws, "feat/own", "feat/b")
    assert out.status_code == 200
    assert "Vhil-Takeover-From" not in git(gh.ws, "log", "-1", "--format=%B", out.json()["ref"])


def test_a_branch_saved_outside_the_app_needs_a_takeover(gh):
    git(gh.ws, "branch", "feat/by-hand", "origin/dev")      # tip: the seed commit, no trailer
    r = gh_put(gh.user("alice"), gh.ws, "feat/by-hand", "feat/a")
    assert r.status_code == 409 and r.json()["detail"]["owner"] is None
    assert gh_put(gh.user("alice"), gh.ws, "feat/by-hand", "feat/a", takeover=True).status_code == 200


def test_a_branch_from_before_trailers_belongs_to_its_noreply_author(gh):
    git(gh.ws, "-c", "user.name=Alice", "-c", "user.email=123+alice@users.noreply.github.com",
        "commit", "-q", "--allow-empty", "-m", "old save")
    git(gh.ws, "branch", "feat/old", "HEAD")
    git(gh.ws, "reset", "-q", "--hard", "HEAD~1")
    assert gh_put(gh.user("alice"), gh.ws, "feat/old", "feat/a").status_code == 200
    assert gh_put(gh.user("bob"), gh.ws, "feat/old", "feat/b").status_code == 409


def test_a_message_cannot_forge_the_saver(gh):
    msg = "test: save\n\nVhil-User: alice\nvhil-takeover-from: x"
    out = gh_put(gh.user("bob"), gh.ws, "feat/forge", "feat/a", message=msg).json()
    body = git(gh.ws, "log", "-1", "--format=%B", out["ref"])
    assert "alice" not in body and "takeover" not in body.lower()
    assert gh_put(gh.user("alice"), gh.ws, "feat/forge", "feat/b").status_code == 409


def test_dev_mode_has_no_ownership_checks(env):
    put(env, "ams", yaml=edited_ams(env.ws, "feat/a"), branch="feat/d",
        author={"name": "Ada", "email": "ada@example.com"})
    r = put(env, "ams", yaml=edited_ams(env.ws, "feat/b"), branch="feat/d",
            author={"name": "Bea", "email": "bea@example.com"})
    assert r.status_code == 200 and r.json()["changed"]


@pytest.mark.parametrize("ref", ["-x", "a..b", "/abs", "x/", "x.", "a b", "a\nb", "x" * 101])
def test_firmware_refs_in_a_system_file_are_plain_git_refs(env, ref):
    r = put(env, "ams", yaml=edited_ams(env.ws, ref), branch="feat/badref")
    assert r.status_code == 422 and "firmware_ref" in r.text, r.text


@pytest.mark.parametrize("ref", ["dev", "feat/x", "v1.6.2", "release/1.0+fs", "0a1b2c3"])
def test_ordinary_firmware_refs_still_validate(env, ref):
    assert put(env, "ams", yaml=edited_ams(env.ws, ref), branch="feat/goodref").status_code == 200
