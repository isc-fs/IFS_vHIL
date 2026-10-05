"""Compose systems from the browser: save, PR, firmware picker (M5.4, #116).

    GET  /api/config                    editor URL, base branch, whether PRs can be opened
    GET  /api/firmware                  catalogue firmware sources (repo, default ref, recipe)
    GET  /api/firmware/{id}/refs        that repo's branches and tags (ls-remote, cached)
    GET  /api/systems/{id}/dataflow     a system as a Pipeline Manager graph (?branch=, ?new=)
    POST /api/systems/{id}/preview      {yaml | dataflow} -> {yaml, errors}, nothing saved
    PUT  /api/systems/{id}              {yaml | dataflow, message, branch} -> {ref, branch}
    POST /api/systems                   {id, yaml? | dataflow?, message, branch} -> {ref, branch}
    POST /api/systems/{id}/pr           {branch, title, body} -> {url}

A save is a commit of systems/<id>.yaml on `branch`, built with git plumbing
so no checkout moves (vhil.server.gitstore); `dev` and `main` are never
written. Before it commits, the file is checked as `python -m vhil.system
validate` checks it, against the catalogue of the commit it builds on, so what
is saved runs unchanged in CI. Errors come back as 422 {detail: {errors}}.
The graph <-> file translation is vhil.editor's (the editor's own import and
export), so an unedited system saves back byte for byte.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import tempfile
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from vhil import editor
from vhil.server.config import env_secret
from vhil.server.githost import (CachedRefs, GitHost, GitHubHost, HostError, HostUnavailable,
                                 LsRemote, RefLister, repo_slug)
from vhil.server.gitstore import (OWNER_TRAILER, TAKEOVER_TRAILER, BadBranch, Conflict, GitError,
                                  GitStore)
from vhil.server.workspace import SYSTEM_ID
from vhil.system import SCHEMA, System, SystemError

router = APIRouter()
log = logging.getLogger("vhil.server.systems")


# -- request bodies -------------------------------------------------------------------

class Author(BaseModel):
    name: str
    email: str


class Save(BaseModel):
    yaml: str | None = None
    dataflow: dict | None = None
    message: str
    branch: str
    author: Author | None = None     # dev mode only; with login, the signed-in user
    # Move a branch another member last saved (logged, and recorded in the
    # commit as Vhil-Takeover-From). Admins (VHIL_ADMINS) need not.
    takeover: bool = False


class Create(Save):
    id: str
    message: str = ""


class Preview(BaseModel):
    yaml: str | None = None
    dataflow: dict | None = None


class PullRequest(BaseModel):
    branch: str
    title: str
    body: str = ""


# -- app state, created on first use (tests put fakes in app.state first) -------------

def _store(request: Request) -> GitStore:
    st = request.app.state
    if getattr(st, "git_store", None) is None:
        st.git_store = GitStore(st.settings.workspace, os.environ.get("VHIL_BASE_BRANCH", "dev"))
    return st.git_store


def _host(request: Request) -> GitHost:
    st = request.app.state
    if getattr(st, "git_host", None) is None:
        repo = repo_slug(st.settings.workspace)
        # The GitHub App when configured (auth.install sets it), else a token.
        app = getattr(st, "github_app", None)
        token = ((lambda: app.token_for(repo, write=True)) if app is not None
                 else env_secret("VHIL_GITHUB_TOKEN") or None)
        st.git_host = GitHubHost(token, repo)
    return st.git_host


def _refs(request: Request) -> RefLister:
    st = request.app.state
    if getattr(st, "ref_lister", None) is None:
        st.ref_lister = CachedRefs(LsRemote(env_secret("VHIL_GITHUB_TOKEN") or None),
                                   float(os.environ.get("VHIL_REFS_TTL_S", "60")))
    return st.ref_lister


def _author(request: Request, body: Save) -> tuple[str, str]:
    """The commit author: the signed-in user in github mode (vhil/server/auth.py
    sets request.state.user), else in dev mode the body's author or the
    configured default (dev mode's fixed local user is not an author)."""
    user = getattr(request.state, "user", None)
    if user and request.app.state.settings.auth != "dev":
        login = user.get("login", "vhil")
        return (user.get("name") or login,
                user.get("email") or f"{login}@users.noreply.github.com")
    if request.app.state.settings.auth != "dev":
        raise HTTPException(401, "sign in to save")
    if body.author:
        return body.author.name, body.author.email
    return (os.environ.get("VHIL_GIT_AUTHOR_NAME", "vHIL dev"),
            os.environ.get("VHIL_GIT_AUTHOR_EMAIL", "vhil-dev@localhost"))


def _owner_trailers(request: Request, store: GitStore, body: Save, tip: str | None) -> dict:
    """The trailers a save records (its saver; a takeover), after checking
    the saver may move `branch`: a new branch, one they saved last, or with
    `takeover` / as an admin. Dev mode has one user and no checks."""
    settings = request.app.state.settings
    if settings.auth == "dev":
        return {}
    me = (getattr(request.state, "user", None) or {}).get("login") or ""
    trailers = {}
    if tip is not None:
        owner = store.owner(tip)
        if owner is None or owner.lower() != me.lower():
            who = owner or "someone outside the app"
            if settings.is_admin(me):
                log.warning("admin %s saves over branch %s (last saved by %s)", me, body.branch, who)
            elif body.takeover:
                log.warning("%s takes over branch %s from %s", me, body.branch, who)
                trailers[TAKEOVER_TRAILER] = owner or "unknown"
            else:
                raise HTTPException(409, {
                    "errors": [f"branch '{body.branch}' was last saved by {who}: save to your own "
                               f"branch, or take it over (logged)"],
                    "owner": owner, "takeover": True})
    trailers[OWNER_TRAILER] = me
    return trailers


def _id(system_id: str) -> str:
    if not SYSTEM_ID.match(system_id):
        raise HTTPException(422, {"errors": [f"'{system_id}' is not a system id: lowercase "
                                             f"letters, digits and '-', at most 64"]})
    return system_id


def _path(system_id: str) -> str:
    return f"systems/{system_id}.yaml"


# -- validation -----------------------------------------------------------------------

def _restore_extras(dataflow: dict, system_id: str, current: str | None) -> dict:
    """Pipeline Manager (v0.5.2) drops a graph's additionalData on graph_get,
    and with it what is not a graph: id, description, time, port, bench and
    the source text that keeps comments. Put back the file's own, from the
    version the save builds on, so a save from the editor loses none of it."""
    graphs = dataflow.get("graphs") or []
    if not graphs:
        return dataflow
    graph = next((g for g in graphs if g.get("id") == dataflow.get("entryGraph")), graphs[0])
    if (graph.get("additionalData") or {}).get("vhil"):
        return dataflow
    extra = {"id": system_id}
    if current is not None:
        try:
            extra = editor.to_dataflow(yaml.safe_load(current), source=current)[
                "graphs"][0]["additionalData"]["vhil"]
        except (yaml.YAMLError, KeyError, TypeError, AttributeError):
            pass
    graph["additionalData"] = {**(graph.get("additionalData") or {}), "vhil": extra}
    return dataflow


def _text(body: Preview | Save, system_id: str, current: str | None) -> str:
    """The system file a body carries: its YAML, or its graph translated
    (`current`: the file as it is where the save lands, if it exists)."""
    if body.yaml is not None:
        return body.yaml
    if body.dataflow is None:
        raise HTTPException(422, {"errors": ["send the system as yaml or as a dataflow"]})
    try:
        _restore_extras(body.dataflow, system_id, current)
        doc = editor.from_dataflow(body.dataflow)
    except (SystemError, KeyError, TypeError, IndexError) as e:
        raise HTTPException(422, {"errors": [f"the graph is not a system: {e}"]})
    return editor.write_system(doc, editor._source(body.dataflow))


@functools.cache
def _system_validator():
    from jsonschema import Draft202012Validator
    schema = json.loads(SCHEMA.read_text())
    return Draft202012Validator({"$ref": "#/$defs/system", "$defs": schema["$defs"]})


def check(store: GitStore, commit: str, system_id: str, text: str) -> list[str]:
    """What `python -m vhil.system validate` would say about `text` as
    systems/<id>.yaml in `commit`'s tree ([] if valid)."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"not YAML: {e}"]
    if not isinstance(doc, dict):
        return ["a system file is a YAML mapping (kind: system, id, boards, ...)"]
    if doc.get("id") != system_id:
        return [f"id '{doc.get('id')}' does not match the file name '{system_id}'"]
    # The schema's top level is oneOf(catalogue kinds, system): checked as a
    # system, each error names its place instead of "not valid under any".
    errors = sorted(_system_validator().iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        return [f"{'.'.join(str(p) for p in e.absolute_path) or '(top)'}: {e.message}"
                for e in errors[:10]]
    with tempfile.TemporaryDirectory(prefix="vhil-check-") as tmp:
        root = Path(tmp)
        store.extract(commit, ["catalog"], root)
        path = root / _path(system_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        try:
            System(path, catalog=root / "catalog")
        except SystemError as e:
            return [str(e).replace(f"{path}: ", "")]
    return []


def _save(request: Request, system_id: str, body: Save, create: bool) -> dict:
    store = _store(request)
    try:
        store.check_branch(body.branch)
    except BadBranch as e:
        raise HTTPException(422, {"errors": [str(e)]})
    if not body.message.strip():
        raise HTTPException(422, {"errors": ["a commit message is needed"]})
    author = _author(request, body)
    parent = store.parent(body.branch)
    trailers = _owner_trailers(request, store, body, store.branch_tip(body.branch))
    text = _text(body, system_id, store.read(parent, _path(system_id)))
    errors = check(store, parent, system_id, text)
    if errors:
        raise HTTPException(422, {"errors": errors, "yaml": text})
    try:
        out = store.commit_file(body.branch, _path(system_id), text, body.message, author,
                                must_not_exist=create, trailers=trailers, expect_parent=parent)
    except Conflict as e:
        raise HTTPException(409, str(e))
    except GitError as e:
        raise HTTPException(500, str(e))
    return {"id": system_id, "path": _path(system_id), **out}


# -- routes ---------------------------------------------------------------------------

@router.get("/api/config")
def config(request: Request):
    host = _host(request)
    return {"editor_url": os.environ.get("VHIL_EDITOR_URL", "http://localhost:5050"),
            "base_branch": _store(request).base,
            "auth": request.app.state.settings.auth,
            "can_open_pr": bool(host.token) if isinstance(host, GitHubHost) else True}


def _firmware_docs(request: Request) -> dict[str, dict]:
    folder = request.app.state.settings.workspace / "catalog" / "firmware"
    docs = (yaml.safe_load(p.read_text()) for p in sorted(folder.glob("*.yaml")))
    return {d["id"]: d for d in docs}


@router.get("/api/firmware")
def firmware(request: Request):
    return [{"id": d["id"], "description": " ".join(str(d.get("description", "")).split()),
             "repo": d["repo"], "ref": d["ref"], "submodules": d.get("submodules", False),
             "build": d["build"]} for d in _firmware_docs(request).values()]


@router.get("/api/firmware/{firmware_id}/refs")
def firmware_refs(firmware_id: str, request: Request):
    doc = _firmware_docs(request).get(firmware_id)
    if doc is None:
        raise HTTPException(404, f"no firmware '{firmware_id}' in the catalogue")
    try:
        refs = _refs(request).refs(doc["repo"])
    except HostError as e:
        raise HTTPException(502, str(e))
    return {"id": firmware_id, "repo": doc["repo"], "default": doc["ref"], **refs}


def _template(system_id: str, request: Request) -> str:
    cat = request.app.state.settings.workspace / "catalog"
    board = sorted(p.stem for p in (cat / "boards").glob("*.yaml"))[0]
    fw = sorted(p.stem for p in (cat / "firmware").glob("*.yaml"))[0]
    return editor.dump_system({"kind": "system", "id": system_id,
                               "description": "A new system: place boards, wire their buses.",
                               "boards": {"board0": {"board": board, "firmware": fw}}})


@router.get("/api/systems/{system_id}/dataflow")
def dataflow(system_id: str, request: Request, branch: str | None = None, new: bool = False):
    """The system file on `branch` (default: the checked-out tree) as the
    graph the editor loads; with new=true, a template if it doesn't exist."""
    _id(system_id)
    store = _store(request)
    ref = None
    if branch:
        ref = store.parent(branch)
        text = store.read(ref, _path(system_id))
    else:
        path = request.app.state.settings.workspace / _path(system_id)
        text = path.read_text() if path.is_file() else None
        ref = store.rev("HEAD")
    exists = text is not None
    if not exists:
        if not new:
            raise HTTPException(404, f"no system '{system_id}'" + (f" on {branch}" if branch else ""))
        text = _template(system_id, request)
    try:
        doc = yaml.safe_load(text)
        graph = editor.to_dataflow(doc, source=text)
    except (yaml.YAMLError, KeyError, TypeError) as e:
        raise HTTPException(422, {"errors": [f"the editor can't show this file: {e}"]})
    return {"id": system_id, "branch": branch, "ref": ref, "exists": exists, "yaml": text,
            "dataflow": graph, "errors": check(store, ref or store.base_commit(), system_id, text)}


@router.post("/api/systems/{system_id}/preview")
def preview(system_id: str, body: Preview, request: Request, branch: str | None = None):
    _id(system_id)
    store = _store(request)
    parent = store.parent(branch)
    text = _text(body, system_id, store.read(parent, _path(system_id)))
    return {"id": system_id, "yaml": text, "errors": check(store, parent, system_id, text)}


@router.put("/api/systems/{system_id}")
def save(system_id: str, body: Save, request: Request):
    return _save(request, _id(system_id), body, create=False)


@router.post("/api/systems", status_code=201)
def create(body: Create, request: Request):
    system_id = _id(body.id)
    if body.yaml is None and body.dataflow is None:
        body.yaml = _template(system_id, request)
    if not body.message.strip():
        body.message = f"feat(systems): add {system_id}"
    return _save(request, system_id, body, create=True)


@router.post("/api/systems/{system_id}/pr")
def pull_request(system_id: str, body: PullRequest, request: Request):
    _id(system_id)
    store = _store(request)
    try:
        store.check_branch(body.branch)
    except BadBranch as e:
        raise HTTPException(422, {"errors": [str(e)]})
    if not body.title.strip():
        raise HTTPException(422, {"errors": ["a PR needs a title"]})
    tip = store.rev(f"refs/heads/{body.branch}")
    if tip is None or store.read(tip, _path(system_id)) is None:
        raise HTTPException(404, f"no saved {_path(system_id)} on branch '{body.branch}': save first")
    host = _host(request)
    try:
        host.push(store.root, body.branch)
        url = host.open_pr(body.branch, store.base, body.title, body.body)
    except HostUnavailable as e:
        raise HTTPException(409, str(e))
    except HostError as e:
        raise HTTPException(502, str(e))
    return {"url": url, "branch": body.branch, "base": store.base, "ref": tip}
