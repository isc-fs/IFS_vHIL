"""Scenario files: a system's runs as data, and its own tests (step 10 of
docs/architecture/editor-workspace.md; schema in docs/scenarios.md).

A scenario is `systems/<system>.scenarios/<name>.yaml`, next to the system
it drives: the stimuli a run applies, what it watches and what it expects,
in virtual time from power-on. Its rows are exactly a `run` scenario's
(vhil/server/runs.py RunScenario: the schema the worker executes and a live
session's ops reuse), so the file is a run, and its `expect` rows make it a
test (vhil/expect.py; the vHIL's own suite, tests/scenarios/, runs every
committed one).

    GET  /api/systems/{id}/contract       the CAN contract the system's firmware speaks,
                                          without a run (?branch=, ?fw=<image>=<ref>...)
    GET  /api/systems/{id}/scenarios      its scenarios (?branch=), each with its last run
    GET  /api/scenarios                   every system's, as checked out
    GET  /api/systems/{id}/scenarios/{n}  one: {yaml, scenario, errors, warnings}
    POST /api/systems/{id}/scenarios/{n}/preview
                                          {yaml | scenario} -> {yaml, scenario, errors,
                                          warnings}, nothing saved
    PUT  /api/systems/{id}/scenarios/{n}  {yaml | scenario, message, branch, base?} -> commit

A save is a one-file commit on a branch, as a system's is
(vhil/server/systems_write.py, the same owner and takeover rules), checked
first as the worker would take it: the schema (names, refs, hex and values
by pattern: the injection hardening the run API has), against the system
(buses, boards, pins of the right kind) and against the firmware's contract
where it is built here (messages, fields, labels; a symbol in the ELF). The
editor sends the scenario as data; the file is written canonically
(dump_scenario), one row a line, and an unchanged scenario keeps its text,
comments and all.
"""
from __future__ import annotations

import json
import math
import re
import tempfile
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

from vhil import elf as velf
from vhil import expect as vexpect
from vhil.server.runs import (_REF, SCENARIO_NAME, CanPeriodic, CanSend, Limits, RunScenario,
                              RunStore, check_scenario)
from vhil.server.workspace import SYSTEM_ID
from vhil.system import System, SystemError

KIND = "scenario"
MAX_TEXT = 512 << 10            # a scenario file's size
MAX_DESCRIPTION = 2000
DEFAULT_SLICE_MS = 100
# What a file carries besides the run's rows.
_META = ("kind", "system", "description")


def scenario_dir(system_id: str) -> str:
    return f"systems/{system_id}.scenarios"


def scenario_path(system_id: str, name: str) -> str:
    return f"{scenario_dir(system_id)}/{name}.yaml"


# -- the file -------------------------------------------------------------------------

def _where(loc: tuple) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif part in ("can_send", "can_periodic", "stop_periodic", "gpio", "analog", "watch",
                      "symbol", "pin") and out.endswith("]"):
            continue                        # the union's tag, not a field
        else:
            out += f".{part}" if out else str(part)
    return out


def errors_of(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        msg = err["msg"].removeprefix("Value error, ")
        where = _where(err["loc"])
        out.append(f"{where}: {msg}" if where else msg)
    return out


def parse(doc: dict, system_id: str, name: str) -> tuple[Optional[RunScenario], str, list[str]]:
    """(the run it is, its description, errors) of a scenario as data: a
    file's mapping, or the editor's (kind and system optional)."""
    if not isinstance(doc, dict):
        return None, "", ["a scenario is a YAML mapping (kind: scenario, system, stimuli, ...)"]
    errors = []
    if doc.get("kind", KIND) != KIND:
        errors.append(f"kind: '{doc.get('kind')}' is not '{KIND}'")
    if doc.get("system", system_id) != system_id:
        errors.append(f"system: '{doc.get('system')}' is not this file's system '{system_id}'")
    description = doc.get("description") or ""
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
        errors.append(f"description: text, at most {MAX_DESCRIPTION} characters")
        description = ""
    run = {k: v for k, v in doc.items() if k not in _META}
    try:
        sc = RunScenario.model_validate({**run, "kind": "run", "name": name})
    except ValidationError as e:
        return None, description, errors + errors_of(e)
    return (None if errors else sc), description, errors


def load_text(text: str, system_id: str, name: str):
    if len(text) > MAX_TEXT:
        return None, "", [f"a scenario file is at most {MAX_TEXT >> 10} KiB"]
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, "", [f"not YAML: {e}"]
    return parse(doc, system_id, name)


# Keys of a row, in the order a file writes them.
_ORDER = ("kind", "check", "name", "at_ms", "until_ms", "bus", "board", "symbol", "id", "ext",
          "data", "period_ms", "periodic", "pin", "level", "volts", "size", "signal", "op",
          "value", "min_ms", "max_ms", "min", "max")
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*\Z")
_YAML_WORDS = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "none", "~"}


def _scalar(key: str, v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if key == "id" and isinstance(v, int):
        return f"0x{v:X}"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    if isinstance(v, str) and _PLAIN.match(v) and v.lower() not in _YAML_WORDS:
        return v
    return json.dumps(v)        # a JSON string is a YAML double-quoted scalar


def _row(item: dict) -> str:
    keys = [k for k in _ORDER if k in item] + sorted(k for k in item if k not in _ORDER)
    parts = []
    for k in keys:
        v = item[k]
        if v is None or (k == "ext" and v is False):
            continue
        parts.append(f"{k}: {_scalar(k, v)}")
    return "{" + ", ".join(parts) + "}"


def dump_scenario(sc: RunScenario, system_id: str, description: str = "") -> str:
    """The canonical file of a scenario: its rows one a line, ids in hex."""
    d = sc.model_dump()
    lines = [f"kind: {KIND}", f"system: {system_id}"]
    if description:
        lines.append(f"description: {json.dumps(description)}")
    lines.append(f"virtual_ms: {d['virtual_ms']}")
    if d["slice_ms"] != DEFAULT_SLICE_MS:
        lines.append(f"slice_ms: {d['slice_ms']}")
    for key in ("stimuli", "watch", "expect"):
        rows = d[key]
        if not rows:
            continue
        lines.append(f"{key}:")
        lines += [f"  - {_row(r)}" for r in rows]
    return "\n".join(lines) + "\n"


def as_data(sc: RunScenario, description: str) -> dict:
    """What the editor edits: the run's rows and the description, None left out."""
    d = sc.model_dump(exclude_none=True)
    d.pop("kind", None)
    d.pop("name", None)
    return {"description": description, **d}


# -- checks against the firmware --------------------------------------------------------

def check_contract(sc: RunScenario, contract: dict, elfs: dict[str, Path]) -> tuple[list[str],
                                                                                    list[str]]:
    """(errors, warnings) of the rows the firmware decides: an expect's
    message, field and label against the contract (vhil/server/decode.py
    system_contract), a frame's length against its message, a symbol
    against the board's ELF. What isn't built here is a warning, not an
    error: the worker builds it, and checks again."""
    errors, warnings = [], []
    buses = contract.get("buses") or {}
    unbuilt = sorted(b for b, info in (contract.get("boards") or {}).items() if info.get("error"))
    noted = set()

    def no_contract(bus: str, where: str) -> None:
        if bus not in noted:
            noted.add(bus)
            warnings.append(f"{where}: no contract for {bus} here (firmware of "
                            f"{', '.join(unbuilt) or 'its boards'} not built): its messages are "
                            f"checked when the run builds it")

    for i, s in enumerate(sc.stimuli):
        if isinstance(s, (CanSend, CanPeriodic)):
            msg = (buses.get(s.bus) or {}).get(str(s.id))
            if msg and not s.ext and len(s.data) // 2 != msg["dlc"]:
                warnings.append(f"stimuli[{i}]: {len(s.data) // 2} bytes, but {msg['name']} "
                                f"(0x{s.id:X}) is {msg['dlc']}")
    for i, e in enumerate(sc.expect):
        where = f"expect[{i}]"
        sig = vexpect.parse_signal(e.signal)
        if sig.kind == "symbol":
            path = elfs.get(sig.owner)
            built = path is not None and path.is_file()
            if built:
                try:
                    velf.symbol(path, sig.item)
                except KeyError:
                    errors.append(f"{where}: no symbol '{sig.item}' in {sig.owner}'s firmware")
                except (OSError, ValueError):
                    pass
            if isinstance(e.value, str):
                # A label: of the symbol's enum (DWARF, or its state view's
                # table; vhil/stateview.py), which needs the image.
                table = vexpect.signal_labels(contract, sig)
                if e.value not in table.values():
                    if not built and not table:
                        warnings.append(f"{where}: '{e.value}' is checked against "
                                        f"{sig.owner}'s enums when the run builds its firmware")
                    else:
                        have = ", ".join(table.values())
                        errors.append(f"{where}: '{e.value}' is not a label of {sig.item}"
                                      + (f" ({have})" if have else ": it has no enum (give a "
                                         "number, or name its enum in the state view)"))
            continue
        if sig.kind != "frame":
            continue
        if not buses.get(sig.owner):
            if sig.item.lower().startswith("0x") and not sig.field:
                continue                    # a raw id needs no contract
            no_contract(sig.owner, where)
            continue
        msg = vexpect.find_message(contract, sig.owner, sig.item)
        if msg is None:
            if sig.item.lower().startswith("0x") and not sig.field:
                continue
            errors.append(f"{where}: no message {sig.item} on {sig.owner} in the firmware's "
                          f"contract")
            continue
        if not sig.field:
            continue
        f = vexpect.find_field(msg, sig.field)
        if f is None:
            errors.append(f"{where}: no field {sig.field} in {msg['name']} "
                          f"({', '.join(x['name'] for x in msg['fields'])})")
            continue
        v = e.value
        if isinstance(v, str):
            table = {**vexpect.signal_labels(contract, sig), **(f.get("values") or {})}
            if vexpect.label_raw({"values": table}, v) is None:
                have = ", ".join(table.values())
                errors.append(f"{where}: '{v}' is not a label of {msg['name']}.{f['name']}"
                              + (f" ({have})" if have else ": it has no value table"))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = vexpect.field_range(f)
            if not (lo <= v <= hi) and math.isfinite(v):
                warnings.append(f"{where}: {v:g} is outside {msg['name']}.{f['name']}'s "
                                f"range {lo:g}..{hi:g}")
    return errors, warnings


def check_rows(sc: RunScenario, system: System, contract: dict, elfs: dict[str, Path],
               limits: Limits) -> tuple[list[str], list[str]]:
    errors = check_scenario(sc, system)
    for key, cap in (("stimuli", limits.max_stimuli), ("watch", limits.max_watch),
                     ("expect", limits.max_expect)):
        if len(getattr(sc, key)) > cap:
            errors.append(f"{key}: {len(getattr(sc, key))} is over this server's limit of {cap}")
    if sc.virtual_ms > limits.max_virtual_ms:
        errors.append(f"virtual_ms: {sc.virtual_ms} is over this server's limit of "
                      f"{limits.max_virtual_ms}")
    warnings = [f"{key}[{i}]: at {row.at_ms:g} ms, after the run's end ({sc.virtual_ms} ms)"
                for key in ("stimuli", "expect") for i, row in enumerate(getattr(sc, key))
                if row.at_ms > sc.virtual_ms]
    if not errors:
        more, warn = check_contract(sc, contract, elfs)
        errors += more
        warnings += warn
    return errors, warnings


# -- the API --------------------------------------------------------------------------

class Body(BaseModel):
    yaml: Optional[str] = None
    scenario: Optional[dict] = None


class SaveBody(Body):
    message: str = ""
    branch: str
    base: Optional[str] = None
    takeover: bool = False
    author: Optional[dict] = None


def _check_id(system_id: str) -> str:
    if not SYSTEM_ID.match(system_id):
        raise HTTPException(422, {"errors": [f"'{system_id}' is not a system id"]})
    return system_id


def _check_name(name: str) -> str:
    if not SCENARIO_NAME.match(name):
        raise HTTPException(422, {"errors": [f"'{name}' is not a scenario name: lowercase "
                                             f"letters, digits and '-', at most 64"]})
    return name


def _check_ref(ref: Optional[str], what: str = "branch") -> Optional[str]:
    if ref and not _REF.match(ref):
        raise HTTPException(422, {"errors": [f"{what} {ref!r} is not a plain git ref"]})
    return ref or None


def _fw_refs(fw: list[str], system: System) -> dict[str, str]:
    """?fw=<image>=<ref> pairs, checked against the system's images."""
    out = {}
    images = system.images()
    for pair in fw:
        key, _, ref = pair.partition("=")
        if key not in images:
            raise HTTPException(422, {"errors": [f"fw: '{key}' is not an image of {system.id}"]})
        out[key] = _check_ref(ref, f"fw ref for '{key}'") or ""
    return {k: v for k, v in out.items() if v}


def router(settings, workspace, limits: Optional[Limits] = None) -> APIRouter:
    from vhil.server import systems_write as sw
    from vhil.server.decode import system_contract
    from vhil.worker import FirmwareResolver

    limits = limits or Limits.from_env()
    runs = RunStore(settings.db)
    r = APIRouter(tags=["scenarios"])

    class At:
        """A system as of a branch (its tip) or the checked-out tree."""

        def __init__(self, request: Request, system_id: str, branch: Optional[str],
                     commit: Optional[str] = None):
            self.request, self.store = request, sw._store(request)
            self.id, self.branch = system_id, _check_ref(branch)
            self.commit = commit or (self.store.parent(self.branch) if self.branch else None)
            self.ref = self.commit or self.store.rev("HEAD") or ""
            self._tmp = tempfile.TemporaryDirectory(prefix="vhil-scenario-")
            text = self.read(f"systems/{system_id}.yaml")
            if text is None:
                raise HTTPException(404, f"no system '{system_id}'"
                                    + (f" on {branch}" if branch else ""))
            path = Path(self._tmp.name) / f"{system_id}.yaml"
            path.write_text(text)
            try:
                self.system = System(path)
            except SystemError as e:
                raise HTTPException(422, {"errors": [f"system '{system_id}' does not validate: {e}"]})

        def read(self, path: str) -> Optional[str]:
            if self.commit:
                return self.store.read(self.commit, path)
            p = Path(workspace.root) / path
            return p.read_text() if p.is_file() else None

        def names(self) -> list[str]:
            d = scenario_dir(self.id)
            if self.commit:
                out = self.store.git("ls-tree", "--name-only", self.commit, f"{d}/")
                files = [Path(line).name for line in out.splitlines()]
            else:
                folder = Path(workspace.root) / d
                files = [p.name for p in folder.glob("*.yaml")] if folder.is_dir() else []
            return sorted(f[:-5] for f in files if f.endswith(".yaml") and SCENARIO_NAME.match(f[:-5]))

        def elfs(self, fw: dict[str, str]) -> dict[str, Path]:
            want = FirmwareResolver(sw._fw_dir(self.request), build=False).expected(self.system,
                                                                                    fw)
            return {b: want[b][1] for b in self.system.boards if b in want}

        def close(self) -> None:
            self._tmp.cleanup()

    def contract_of(at: "At", fw: list[str]) -> tuple[dict, dict[str, Path]]:
        elfs = at.elfs(_fw_refs(fw, at.system))
        return system_contract(at.system, elfs), elfs

    def last_runs(system_id: str) -> dict[str, dict]:
        out = {}
        for name, run in runs.last_by_scenario(system_id).items():
            s = run.get("summary") or {}
            out[name] = {"id": run["id"], "state": run["state"], "created": run["created"],
                         "finished": run.get("finished"), "ref_name": run.get("ref_name", ""),
                         "expects_passed": s.get("expects_passed"),
                         "expects_failed": s.get("expects_failed")}
        return out

    def listing(at: "At") -> list[dict]:
        last = last_runs(at.id)
        out = []
        for name in at.names():
            text = at.read(scenario_path(at.id, name)) or ""
            sc, description, errors = load_text(text, at.id, name)
            out.append({"system": at.id, "name": name, "path": scenario_path(at.id, name),
                        "description": " ".join(description.split()),
                        "virtual_ms": sc.virtual_ms if sc else None,
                        "rows": (len(sc.stimuli) + len(sc.watch) + len(sc.expect)) if sc else 0,
                        "expects": len(sc.expect) if sc else 0,
                        "valid": not errors, "last_run": last.get(name)})
        return out

    def body_scenario(body: Body, at: "At", name: str, current: Optional[str]):
        """(run, description, text, errors) of what a request carries."""
        if body.yaml is not None:
            sc, description, errors = load_text(body.yaml, at.id, name)
            return sc, description, body.yaml, errors
        if body.scenario is None:
            raise HTTPException(422, {"errors": ["send the scenario as yaml or as data"]})
        sc, description, errors = parse(body.scenario, at.id, name)
        if sc is None:
            return None, description, None, errors
        text = dump_scenario(sc, at.id, description)
        if current is not None:
            # An unchanged scenario keeps its file (comments and all).
            old, old_desc, old_errors = load_text(current, at.id, name)
            if not old_errors and old == sc and old_desc == description:
                text = current
        return sc, description, text, errors

    def checked(at: "At", name: str, body: Body, fw: list[str]) -> dict:
        current = at.read(scenario_path(at.id, name))
        sc, description, text, errors = body_scenario(body, at, name, current)
        warnings: list[str] = []
        if sc is not None:
            contract, elfs = contract_of(at, fw)
            errors, warnings = check_rows(sc, at.system, contract, elfs, limits)
        return {"system": at.id, "name": name, "path": scenario_path(at.id, name),
                "exists": current is not None, "ref": at.ref, "yaml": text,
                "scenario": as_data(sc, description) if sc else None,
                "errors": errors, "warnings": warnings}

    @r.get("/api/systems/{system_id}/contract")
    def contract(system_id: str, request: Request, branch: Optional[str] = None,
                 fw: list[str] = Query([])):
        at = At(request, _check_id(system_id), branch)
        try:
            out, elfs = contract_of(at, fw)
        finally:
            at.close()
        return {**out, "system": system_id, "ref": at.ref,
                "built": {b: p.is_file() for b, p in elfs.items()}}

    @r.get("/api/systems/{system_id}/scenarios")
    def scenarios(system_id: str, request: Request, branch: Optional[str] = None):
        at = At(request, _check_id(system_id), branch)
        try:
            return listing(at)
        finally:
            at.close()

    @r.get("/api/scenarios")
    def every(request: Request):
        out = []
        for s in workspace.systems():
            try:
                at = At(request, s["id"], None)
            except HTTPException:
                continue
            try:
                out += listing(at)
            finally:
                at.close()
        return out

    @r.get("/api/systems/{system_id}/scenarios/{name}")
    def one(system_id: str, name: str, request: Request, branch: Optional[str] = None,
            fw: list[str] = Query([])):
        at = At(request, _check_id(system_id), branch)
        try:
            text = at.read(scenario_path(system_id, _check_name(name)))
            if text is None:
                raise HTTPException(404, f"no scenario '{name}' for {system_id}"
                                    + (f" on {branch}" if branch else ""))
            return checked(at, name, Body(yaml=text), fw)
        finally:
            at.close()

    @r.post("/api/systems/{system_id}/scenarios/{name}/preview")
    def preview(system_id: str, name: str, body: Body, request: Request,
                branch: Optional[str] = None, fw: list[str] = Query([])):
        at = At(request, _check_id(system_id), branch)
        try:
            return checked(at, _check_name(name), body, fw)
        finally:
            at.close()

    @r.put("/api/systems/{system_id}/scenarios/{name}")
    def save(system_id: str, name: str, body: SaveBody, request: Request,
             fw: list[str] = Query([])):
        _check_id(system_id)
        _check_name(name)
        store = sw._store(request)
        try:
            store.check_branch(body.branch)
        except sw.BadBranch as e:
            raise HTTPException(422, {"errors": [str(e)]})
        author = sw._author(request, sw.Save(message=body.message or "x", branch=body.branch,
                                             author=body.author))
        base = None
        if body.base:
            base = store.rev(body.base) if sw._COMMIT.match(body.base) else None
            if base is None:
                raise HTTPException(422, {"errors": [f"base {body.base!r} is not a commit of "
                                                     f"the workspace"]})
        parent = store.parent(body.branch, base)
        trailers = sw._owner_trailers(request, store, sw.Save(
            message=body.message or "x", branch=body.branch, takeover=body.takeover),
            store.branch_tip(body.branch))
        at = At(request, system_id, None, commit=parent)
        try:
            out = checked(at, name, body, fw)
        finally:
            at.close()
        if out["errors"]:
            raise HTTPException(422, {"errors": out["errors"], "yaml": out["yaml"]})
        message = body.message.strip() or (
            f"test(scenarios): {'update' if out['exists'] else 'add'} {system_id}/{name}")
        try:
            saved = store.commit_file(body.branch, out["path"], out["yaml"], message, author,
                                      trailers=trailers, expect_parent=parent, base=base)
        except sw.Conflict as e:
            raise HTTPException(409, str(e))
        except sw.GitError as e:
            raise HTTPException(500, str(e))
        return {"system": system_id, "name": name, "path": out["path"],
                "warnings": out["warnings"], **saved}

    return r
