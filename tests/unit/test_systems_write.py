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
from vhil.server.githost import (CachedRefs, FakeGitHost, FakeRefLister, GitHubHost,  # noqa: E402
                                 parse_ls_remote)
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
    lister = FakeRefLister({"isc-fs/IFS08-CE-ECU": {"branches": ["dev", "feat/x", "main"],
                                                   "tags": ["v1.0.0"]}})
    app.state.ref_lister = CachedRefs(lister, ttl_s=60, clock=clock)
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
    ("kind: system\nid: other\nboards:\n  a: {board: mlc-carrier, firmware: ams}\n",
     "does not match the file name"),
    ("kind: system\nid: ams\nboards: {}\n", "should be non-empty"),
    ("kind: system\nid: ams\nwheels: 4\nboards:\n  a: {board: mlc-carrier, firmware: ams}\n",
     "wheels"),
    ("kind: system\nid: ams\nboards:\n  a: {board: no-such-board, firmware: ams}\n",
     "no board 'no-such-board'"),
    ("kind: system\nid: ams\nboards:\n  a: {board: mlc-carrier, firmware: ams}\n"
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
                 "branches": ["dev", "feat/x", "main"], "tags": ["v1.0.0"]}
    env.client.get("/api/firmware/ecu/refs")
    assert env.lister.calls == ["isc-fs/IFS08-CE-ECU"]
    env.clock.t = 61
    env.client.get("/api/firmware/ecu/refs")
    assert len(env.lister.calls) == 2


def test_firmware_refs_errors(env):
    assert env.client.get("/api/firmware/nope/refs").status_code == 404
    r = env.client.get("/api/firmware/ams/refs")     # not in the fake: ls-remote fails
    assert r.status_code == 502 and "IFS08-CE-AMS" in r.text


def test_ls_remote_output_is_parsed():
    out = ("a\trefs/heads/dev\nb\trefs/heads/feat/x\nc\trefs/tags/v1.0\n"
           "d\trefs/tags/v1.0^{}\ne\trefs/tags/v1.1\nf\tHEAD\n")
    assert parse_ls_remote(out) == {"branches": ["dev", "feat/x"], "tags": ["v1.1", "v1.0"]}


def test_config_names_the_editor(env, monkeypatch):
    monkeypatch.setenv("VHIL_EDITOR_URL", "http://editor.example:5050")
    c = env.client.get("/api/config").json()
    assert c["editor_url"] == "http://editor.example:5050" and c["base_branch"] == "dev"
    assert c["can_open_pr"] is True     # the fake host


def test_the_editor_page_is_served(env):
    assert env.client.get("/static/editor.js").status_code == 200
