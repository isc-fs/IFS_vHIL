"""CI's test-impact selection: what heavy CI a change runs (ci/impact.yaml,
docs/ci.md).

    python scripts/ci-select.py --explain --changed vhil/plant_ts.py docs/ci.md
    python scripts/ci-select.py --diff origin/dev HEAD               # this branch, as `full-ci`
    python scripts/ci-select.py --event pull_request --base-ref dev --labels '["full-ci"]' \\
        --diff HEAD^1 HEAD --github-output "$GITHUB_OUTPUT"          # what CI runs

The mode comes from the event: a pull request labelled `full-ci-all`, a pull
request into main, the nightly (schedule), a dispatch or any other event runs
everything; a pull request labelled `full-ci` runs what its changed paths
select plus the guard; any other pull request nothing heavy (unit tests run
on every PR regardless). Without --event the mode is `full-ci`'s (impact).
The editor check is selected by paths alone, labelled or not.

Writes key=value job outputs (--github-output) for the workflows' `if:`s and
matrices, a step summary (--summary), and with --explain which rule each
path matched. Fail-safe: a path no rule matches, or a diff that can't be
computed, selects everything.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
IMPACT = REPO / "ci" / "impact.yaml"
SIM_DIR = REPO / "tests" / "sim"
SHARDS_MAX = 8          # the full run's shard count (sim.yml)
LANES = 2               # files a shard runs at once (scripts/sim-shard.sh)
LANE_TARGET_S = 600     # a lane of the full run (tests/sim/durations.json / 16)
SMOKE = ("ecu", "ams")                      # tests/<s>_smoke.robot, smoke.yml's legs
IFS_HIL = ("ecu", "ecu-ams", "ams")         # ifs-hil-ecu.yml's jobs (system ids)
LABEL_IMPACT, LABEL_ALL = "full-ci", "full-ci-all"
TARGET_KEYS = {"markers", "sim", "importers", "systems", "runs", "boards", "firmware", "devices",
               "scenarios", "smoke", "ifs_hil", "speed", "editor"}


# -- globs ---------------------------------------------------------------------

def glob_regex(pattern: str) -> re.Pattern:
    """`*` within a segment, `**` across segments, `{name}` one segment's
    characters captured as `name`."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "{":
            j = pattern.index("}", i)
            out, i = out + f"(?P<{pattern[i + 1:j]}>[^/]+?)", j + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def substitute(value, captures: dict[str, str]):
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: captures.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [substitute(v, captures) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, captures) for k, v in value.items()}
    return value


# -- the repository's facts ----------------------------------------------------

def sim_markers(repo: Path = REPO) -> dict[str, set[str]]:
    """tests/sim file -> the pytest markers its module-level `pytestmark`
    names (pytest.mark.<name>; parametrize/skipif/xfail included, harmless)."""
    out = {}
    for path in sorted((repo / "tests" / "sim").glob("test_*.py")):
        marks: set[str] = set()
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark"
                                                    for t in node.targets):
                for sub in ast.walk(node.value):
                    if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Attribute)
                            and sub.value.attr == "mark"):
                        marks.add(sub.attr)
        out[path.relative_to(repo).as_posix()] = marks
    return out


def imports(repo: Path = REPO) -> dict[str, set[str]]:
    """Test file (tests/sim/test_*.py, tests/scenarios/test_*.py) -> the
    modules it imports directly: `import a.b` -> a.b; `from a.b import c` ->
    a.b and a.b.c (c may be a module)."""
    out = {}
    files = sorted((repo / "tests" / "sim").glob("test_*.py"))
    files += sorted((repo / "tests" / "scenarios").glob("test_*.py"))
    for path in files:
        mods: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mods.add(node.module)
                mods.update(f"{node.module}.{a.name}" for a in node.names)
        out[path.relative_to(repo).as_posix()] = mods
    return out


def module_of(path: str) -> str | None:
    """The module a changed .py file is, as the tests import it: vhil/x.py ->
    vhil.x, vhil/server/__init__.py -> vhil.server, tests/sim/ams_car.py ->
    ams_car (tests/sim is on the tests' path)."""
    if not path.endswith(".py"):
        return None
    if path.startswith("tests/sim/") and path.count("/") == 2:
        return Path(path).stem
    if path.startswith("vhil/"):
        parts = path[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    return None


def systems(repo: Path = REPO) -> dict[str, dict]:
    """System id -> {"roles": {role,...}, "devices": {model,...}}, for
    systems/*.yaml and the benchmark systems (scripts/speed-systems/)."""
    out = {}
    files = sorted((repo / "systems").glob("*.yaml")) + sorted((repo / "scripts" / "speed-systems").glob("*.yaml"))
    for path in files:
        data = yaml.safe_load(path.read_text()) or {}
        roles = {b.get("role") or b.get("firmware") for b in (data.get("boards") or {}).values()}
        devices = {d.get("model") for d in (data.get("devices") or {}).values()}
        out[data.get("id", path.stem)] = {"roles": roles - {None}, "devices": devices - {None}}
    return out


def role_firmware(repo: Path = REPO) -> dict[str, str]:
    """MainLite role -> its application's firmware id (catalog/boards)."""
    out = {}
    for path in (repo / "catalog" / "boards").glob("*.yaml"):
        for role, spec in ((yaml.safe_load(path.read_text()) or {}).get("roles") or {}).items():
            if isinstance(spec, dict) and spec.get("firmware"):
                out[role] = spec["firmware"]
    return out


def durations(repo: Path = REPO) -> dict[str, float]:
    p = repo / "tests" / "sim" / "durations.json"
    return json.loads(p.read_text()) if p.exists() else {}


# -- the selection ---------------------------------------------------------------

@dataclass
class Selection:
    mode: str                                   # none | impact | all
    reason: str = ""
    sim_files: set[str] = field(default_factory=set)
    scenarios: set[str] = field(default_factory=set)
    smoke: set[str] = field(default_factory=set)
    ifs_hil: set[str] = field(default_factory=set)
    speed: bool = False
    editor: bool = False
    matched: dict[str, list[str]] = field(default_factory=dict)   # path -> rule names

    @property
    def heavy(self) -> bool:
        return self.mode != "none"

    def as_dict(self) -> dict:
        return {"mode": self.mode, "reason": self.reason, "sim_files": sorted(self.sim_files),
                "scenarios": sorted(self.scenarios), "smoke": sorted(self.smoke),
                "ifs_hil": sorted(self.ifs_hil), "speed": self.speed, "editor": self.editor,
                "guard": self.heavy, "matched": self.matched}


class Selector:
    def __init__(self, repo: Path = REPO, impact: dict | None = None):
        self.repo = repo
        self.impact = impact if impact is not None else yaml.safe_load((repo / "ci" / "impact.yaml").read_text())
        self.everything = [glob_regex(g) for g in self.impact.get("everything") or []]
        self.rules = []
        for rule in self.impact.get("rules") or []:
            unknown = set(rule.get("select") or {}) - TARGET_KEYS
            if unknown:
                raise ValueError(f"ci/impact.yaml rule {rule.get('name')!r}: unknown target {sorted(unknown)}")
            self.rules.append((rule.get("name", "?"), [glob_regex(p) for p in rule["paths"]],
                               rule.get("select") or {}))
        self.markers = sim_markers(repo)
        self.imports = imports(repo)
        self.systems = systems(repo)
        self.role_fw = role_firmware(repo)
        self.scenario_systems = {p.name.removesuffix(".scenarios")
                                 for p in (repo / "systems").glob("*.scenarios") if p.is_dir()}

    # The whole set, for `all`. The editor check stays path-selected.
    def everything_selection(self, mode: str = "all", reason: str = "") -> Selection:
        return Selection(mode=mode, reason=reason, sim_files=set(self.markers),
                         scenarios=set(self.scenario_systems), smoke=set(SMOKE),
                         ifs_hil=set(IFS_HIL), speed=True)

    def _runs(self, sel: Selection, system: str) -> None:
        if system in self.scenario_systems:
            sel.scenarios.add(system)
        if system in SMOKE:
            sel.smoke.add(system)
        if system in IFS_HIL:
            sel.ifs_hil.add(system)

    def _systems(self, sel: Selection, system: str) -> None:
        marker = "system_" + system.replace("-", "_")
        sel.sim_files |= {f for f, m in self.markers.items() if marker in m}
        self._runs(sel, system)

    def _boards(self, sel: Selection, role: str) -> None:
        sel.sim_files |= {f for f, m in self.markers.items() if role in m}
        for sid, spec in self.systems.items():
            if role in spec["roles"]:
                self._systems(sel, sid)

    def _apply(self, sel: Selection, path: str, target: dict) -> None:
        def every(values, universe):
            return set(universe) if "*" in values else set(values) & set(universe)

        sel.sim_files |= {f for f, m in self.markers.items() if m & set(target.get("markers") or [])}
        for g in target.get("sim") or []:
            rx = glob_regex(g)
            sel.sim_files |= {f for f in self.markers if rx.match(f)}
        if target.get("importers"):
            mod = module_of(path)
            if mod is not None:
                for f, mods in self.imports.items():
                    if mod in mods or any(m.startswith(mod + ".") for m in mods if path.endswith("__init__.py")):
                        if f.startswith("tests/scenarios/"):
                            sel.scenarios |= self.scenario_systems
                        else:
                            sel.sim_files.add(f)
        for s in target.get("systems") or []:
            self._systems(sel, s)
        for s in target.get("runs") or []:
            self._runs(sel, s)
        for fw in target.get("firmware") or []:
            for role, rfw in self.role_fw.items():
                if rfw == fw:
                    self._boards(sel, role)
        for b in target.get("boards") or []:
            self._boards(sel, b)
        for model in target.get("devices") or []:
            for sid, spec in self.systems.items():
                if model in spec["devices"]:
                    self._runs(sel, sid)
        sel.scenarios |= every(target.get("scenarios") or [], self.scenario_systems)
        sel.smoke |= every(target.get("smoke") or [], SMOKE)
        sel.ifs_hil |= every(target.get("ifs_hil") or [], IFS_HIL)
        sel.speed |= bool(target.get("speed"))
        sel.editor |= bool(target.get("editor"))

    def select(self, changed: list[str], mode: str = "impact") -> Selection:
        """The selection for these changed paths. mode: impact (`full-ci`),
        all, or none. The editor check is selected by paths in every mode."""
        sel = Selection(mode=mode)
        forced = []
        for path in sorted(set(changed)):
            names = ["everything"] if any(rx.match(path) for rx in self.everything) else []
            mapped = False
            for name, globs, target in self.rules:
                for rx in globs:
                    m = rx.match(path)
                    if m:
                        names.append(name)
                        mapped = True
                        self._apply(sel, path, substitute(target, m.groupdict()))
                        break
            if not names:
                names = ["UNMAPPED: everything"]
            if "everything" in names or not mapped:
                forced.append(path)
            sel.matched[path] = names
        if mode == "none":
            return Selection(mode="none", editor=sel.editor, matched=sel.matched)
        if forced or mode == "all":
            reason = ""
            if forced:
                more = f" (+{len(forced) - 1} more)" if len(forced) > 1 else ""
                reason = f"{forced[0]} selects everything{more}"
            full = self.everything_selection(mode, reason)
            full.matched, full.editor = sel.matched, sel.editor
            return full
        sel.reason = f"{len(set(changed))} changed path(s)"
        return sel


def shards(files: set[str], all_files: bool, durs: dict[str, float]) -> int:
    """How many sim runners a selection needs: the full run's 8, or the
    fewest whose lanes, filled longest file first as scripts/sim-balance.py
    fills them, each take no longer than a lane of the full run (or the
    longest file)."""
    if not files:
        return 0
    if all_files:
        return SHARDS_MAX
    known = sorted(durs[f] for f in files if f in durs)
    default = known[len(known) // 2] if known else 60.0
    cost = {f: durs.get(f, default) for f in files}
    bound = max(LANE_TARGET_S, max(cost.values()))
    for n in range(1, SHARDS_MAX):
        lanes = [0.0] * (n * LANES)
        for f in sorted(files, key=lambda f: (-cost[f], f)):
            i = min(range(len(lanes)), key=lambda i: (lanes[i], i))
            lanes[i] += cost[f]
        if max(lanes) <= bound:
            return n
    return SHARDS_MAX


# -- the event -------------------------------------------------------------------

def mode_of(event: str | None, base_ref: str | None, labels: list[str]) -> tuple[str, str]:
    if event is None:
        return "impact", "local: as a full-ci-labelled PR"
    if event != "pull_request":
        return "all", f"{event}: runs everything"
    if base_ref == "main":
        return "all", "release PR into main: runs everything"
    if LABEL_ALL in labels:
        return "all", f"labelled {LABEL_ALL}: runs everything"
    if LABEL_IMPACT in labels:
        return "impact", f"labelled {LABEL_IMPACT}: the changes' tests + the guard"
    return "none", f"no {LABEL_IMPACT}/{LABEL_ALL} label: unit tests only"


def changed_paths(base: str, head: str, repo: Path = REPO) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "diff", "--no-renames", "--name-only", base, head],
                         check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def outputs(sel: Selection, all_files: bool, durs: dict[str, float]) -> dict[str, str]:
    n = shards(sel.sim_files, all_files, durs)
    tf = lambda b: "true" if b else "false"  # noqa: E731
    return {
        "mode": sel.mode,
        "reason": sel.reason.replace("\n", " "),
        "heavy": tf(sel.heavy),
        "guard": tf(sel.heavy),
        "sim": tf(bool(sel.sim_files)),
        "sim_files": " ".join(sorted(sel.sim_files)),
        "sim_shard_count": str(n),
        # A matrix needs a value even when the job is skipped.
        "sim_shards": json.dumps(list(range(1, n + 1)) or [0]),
        "speed": tf(sel.speed),
        "renode_sim": tf(sel.heavy),            # the guard always runs when heavy
        "scenarios": tf(bool(sel.scenarios)),
        "scenario_systems": " ".join(sorted(sel.scenarios)),
        "smoke": json.dumps(sorted(sel.smoke)),
        "smoke_any": tf(bool(sel.smoke)),
        "smoke_ecu": tf("ecu" in sel.smoke),
        "smoke_ams": tf("ams" in sel.smoke),
        "ifs_hil_any": tf(bool(sel.ifs_hil)),
        "ifs_hil_ecu": tf("ecu" in sel.ifs_hil),
        "ifs_hil_ecu_ams": tf("ecu-ams" in sel.ifs_hil),
        "ifs_hil_ams": tf("ams" in sel.ifs_hil),
        "editor": tf(sel.editor),
    }


def summary(sel: Selection, out: dict[str, str], changed: list[str]) -> str:
    lines = ["## CI impact selection", "",
             f"**Mode:** `{sel.mode}`: {sel.reason}", ""]
    if sel.mode != "none":
        lines += ["| Workflow | Runs |", "|---|---|",
                  "| guard (fingerprint, determinism) | yes |",
                  f"| sim | {len(sel.sim_files)} file(s) on {out['sim_shard_count']} shard(s) |",
                  f"| scenarios | {', '.join(sorted(sel.scenarios)) or '-'} |",
                  f"| smoke | {', '.join(sorted(sel.smoke)) or '-'} |",
                  f"| IFS_HIL | {', '.join(sorted(sel.ifs_hil)) or '-'} |",
                  f"| emulation speed | {'yes' if sel.speed else '-'} |", ""]
    lines += [f"| editor | {'yes' if sel.editor else '-'} |", ""]
    lines += ["<details><summary>Changed paths and the rules they matched "
              f"({len(changed)})</summary>", "", "| Path | Rules |", "|---|---|"]
    lines += [f"| `{p}` | {', '.join(r)} |" for p, r in sorted(sel.matched.items())]
    lines += ["", "</details>", ""]
    if sel.sim_files and sel.mode == "impact":
        lines += ["<details><summary>Selected tests/sim files</summary>", ""]
        lines += [f"- `{f}`" for f in sorted(sel.sim_files)] + ["", "</details>", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--event", help="github.event_name (default: as a full-ci PR)")
    ap.add_argument("--base-ref", help="the pull request's base branch")
    ap.add_argument("--labels", default="[]", help="the PR's labels: JSON list or comma-separated")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--changed", nargs="*", help="changed paths")
    src.add_argument("--diff", nargs=2, metavar=("BASE", "HEAD"), help="changed paths from git diff")
    ap.add_argument("--github-output", type=Path)
    ap.add_argument("--summary", type=Path)
    ap.add_argument("--json", action="store_true", help="print the selection as JSON")
    ap.add_argument("--explain", action="store_true", help="print the rule each path matched")
    args = ap.parse_args(argv)

    labels = args.labels.strip()
    labels = json.loads(labels) if labels.startswith("[") else [x for x in labels.split(",") if x]
    if labels and isinstance(labels[0], dict):          # github.event.pull_request.labels
        labels = [x.get("name", "") for x in labels]
    mode, why = mode_of(args.event, args.base_ref, labels)

    selector = Selector()
    changed: list[str] = []
    fail_safe = None
    if args.diff:
        try:
            changed = changed_paths(*args.diff)
        except subprocess.CalledProcessError as e:
            fail_safe = f"git diff {' '.join(args.diff)} failed ({e.stderr.strip()[:200]}): everything"
    elif args.changed is not None:
        changed = args.changed
    if fail_safe:
        sel = (selector.everything_selection("all", fail_safe) if mode != "none"
               else Selection(mode="none", reason=fail_safe))
        sel.editor = True
    else:
        sel = selector.select(changed, mode)
        sel.reason = f"{why}; {sel.reason}" if mode == "impact" else why
    out = outputs(sel, sel.sim_files == set(selector.markers), durations())

    if args.github_output:
        with args.github_output.open("a") as f:
            for k, v in out.items():
                f.write(f"{k}={v}\n")
    if args.summary:
        with args.summary.open("a") as f:
            f.write(summary(sel, out, changed) + "\n")
    if args.json:
        print(json.dumps(sel.as_dict() | {"outputs": out}, indent=1))
    elif args.explain:
        for p, r in sorted(sel.matched.items()):
            print(f"{p}: {', '.join(r)}")
        print(f"mode: {sel.mode} ({sel.reason})")
        for k, v in out.items():
            print(f"  {k} = {v}")
    else:
        for k, v in out.items():
            print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
