"""The nightly impact audit: the safety net behind CI's test-impact selection
(scripts/ci-select.py, ci/impact.yaml, docs/ci.md).

    python scripts/ci-audit.py [--since ISO-8601] [--summary FILE] [--comment]

1. Every pull request merged into dev since the last audit: the label it had
   (`full-ci-all`, `full-ci` or none) and the selection `full-ci` gives its
   changes (its merge commit against its first parent).
2. The nightly runs on dev since then (sim, smoke, scenarios, IFS_HIL): what
   failed, as precisely as the artifacts say (a tests/sim file from each
   shard's results.tsv, a scenario from scenarios.xml, else the job).
3. A failure outside a merged PR's selection is reported: for a `full-ci` PR
   it is a selection gap (the PR ran without it, then the nightly failed),
   commented on the PR with --comment; for an unlabelled PR, a note.
4. The impact map: what each tests/sim file exercised in the nightly
   (VHIL_IMPACT_MAP: the systems its Sims booted, the vhil/ Python it ran;
   through its systems, the firmware and the device models they load), and
   for each such path whether ci/impact.yaml selects that file when the path
   changes. A path that doesn't is covered only by the guard (fingerprint)
   for that file: listed, so a rule can be widened if it should.

Report only (step summary, warnings, PR comments): it never fails the run.
Needs `gh` (GH_TOKEN) and a full clone.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ci_select", REPO / "scripts" / "ci-select.py")
cs = importlib.util.module_from_spec(spec)
sys.modules["ci_select"] = cs
spec.loader.exec_module(cs)

NIGHTLY = ("sim.yml", "smoke.yml", "scenarios.yml", "ifs-hil-ecu.yml")
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "isc-fs/IFS_vHIL")


# -- failures --------------------------------------------------------------------

@dataclass(frozen=True)
class Failure:
    kind: str       # sim | scenario | smoke | ifs_hil | guard | speed | renode | job
    item: str       # tests/sim file, <system>/<scenario>, system, job name
    run_url: str = ""

    def label(self) -> str:
        return f"{self.kind}: {self.item}"


def in_selection(f: Failure, sel: "cs.Selection") -> bool:
    """Would this selection have run what failed?"""
    if f.kind == "sim":
        return f.item in sel.sim_files or f.item == "*" and bool(sel.sim_files)
    if f.kind == "scenario":
        system = f.item.split("/")[0]
        return system == "*" and bool(sel.scenarios) or system in sel.scenarios
    if f.kind == "smoke":
        return f.item in sel.smoke
    if f.kind == "ifs_hil":
        return f.item in sel.ifs_hil
    if f.kind == "speed":
        return sel.speed
    # The guard and the Renode build run with every heavy selection.
    return sel.heavy


def sim_failures(results_tsv: str) -> list[str]:
    """Failed files from a shard's results.tsv (file, exit code, seconds)."""
    out = []
    for line in results_tsv.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] != "0":
            out.append(parts[0])
    return out


def scenario_failures(junit_xml: str) -> list[str]:
    """Failed scenario ids (<system>/<name>) from tests/scenarios' JUnit."""
    out = []
    for case in ET.fromstring(junit_xml).iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            out.append(case.get("name", "").removeprefix("test_scenario[").removesuffix("]"))
    return out


def job_failure(name: str) -> tuple[str, str]:
    """A failed job's (kind, item) from its check name."""
    if name.startswith("smoke (") and name.endswith(")"):
        return "smoke", name[7:-1]
    if name in cs.IFS_HIL:
        return "ifs_hil", name
    if name == "impact guard":
        return "guard", name
    if name == "emulation speed":
        return "speed", name
    if name == "renode build":
        return "renode", name
    if name.startswith("sim (") or name == "sim":
        return "sim", "*"
    if name == "scenarios":
        return "scenario", "*/*"
    return "job", name


# -- GitHub --------------------------------------------------------------------------

def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def gh_json(path: str):
    return json.loads(gh("api", "-H", "Accept: application/vnd.github+json", path))


def previous_audit(now: dt.datetime) -> dt.datetime:
    """When the last completed audit started, else 26 h ago."""
    try:
        runs = gh_json(f"repos/{REPO_SLUG}/actions/workflows/impact-audit.yml/runs?branch=dev&status=success&per_page=5")
        for run in runs.get("workflow_runs", []):
            started = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if started < now - dt.timedelta(minutes=30):
                return started
    except subprocess.CalledProcessError:
        pass
    return now - dt.timedelta(hours=26)


def merged_prs(since: dt.datetime) -> list[dict]:
    out = []
    pulls = gh_json(f"repos/{REPO_SLUG}/pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=100")
    for pr in pulls:
        if pr.get("merged_at"):
            merged = dt.datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))
            if merged >= since:
                out.append(pr)
    return sorted(out, key=lambda p: p["merged_at"])


def nightly_failures(since: dt.datetime, workdir: Path) -> tuple[list[Failure], list[str]]:
    """What failed in the nightly runs on dev created since `since`, and the
    runs looked at (for the report)."""
    failures, seen = [], []
    for wf in NIGHTLY:
        runs = gh_json(f"repos/{REPO_SLUG}/actions/workflows/{wf}/runs?branch=dev&event=schedule&per_page=5")
        runs = [r for r in runs.get("workflow_runs", [])
                if dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= since]
        if not runs:
            seen.append(f"{wf}: no scheduled run since {since:%Y-%m-%d %H:%M}Z")
            continue
        run = runs[0]
        seen.append(f"[{wf} #{run['run_number']}]({run['html_url']}): {run['status']} / {run['conclusion']}")
        if run.get("conclusion") == "success":
            continue
        jobs = gh_json(f"repos/{REPO_SLUG}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        for job in jobs:
            if job.get("conclusion") not in ("failure", "timed_out", "cancelled"):
                continue
            name = job["name"]
            kind, item = job_failure(name)
            precise = []
            if name.startswith("sim (") and name[5:-1].isdigit():
                tsv = _artifact_file(run["id"], f"sim-logs-{name[5:-1]}", "results.tsv", workdir)
                precise = [Failure("sim", f, run["html_url"]) for f in sim_failures(tsv or "")]
            elif name == "scenarios":
                xml = _artifact_file(run["id"], "scenarios", "scenarios.xml", workdir)
                if xml:
                    precise = [Failure("scenario", s, run["html_url"]) for s in scenario_failures(xml)]
            elif name == "sim":
                continue        # the aggregate of the shards and the guard
            failures += precise or [Failure(kind, item, run["html_url"])]
    return sorted(set(failures), key=lambda f: (f.kind, f.item)), seen


def _artifact_file(run_id: int, artifact: str, name: str, workdir: Path) -> str | None:
    dest = workdir / f"{run_id}-{artifact}"
    if not dest.exists():
        try:
            gh("run", "download", str(run_id), "-R", REPO_SLUG, "-n", artifact, "-D", str(dest))
        except subprocess.CalledProcessError:
            return None
    hits = list(dest.rglob(name))
    return hits[0].read_text() if hits else None


# -- merged PRs' selections ----------------------------------------------------------------

def pr_changes(merge_sha: str) -> list[str] | None:
    try:
        return cs.changed_paths(f"{merge_sha}^1", merge_sha)
    except subprocess.CalledProcessError:
        return None


def pr_mode(pr: dict) -> str:
    labels = [x["name"] for x in pr.get("labels", [])]
    return cs.mode_of("pull_request", pr["base"]["ref"], labels)[0]


# -- the impact map ----------------------------------------------------------------------------

def exercised(impact_dir: Path, selector: "cs.Selector") -> dict[str, set[str]]:
    """tests/sim file -> the repo paths it exercised in the nightly: the vhil/
    modules it ran (coverage), its systems' files, their boards' firmware
    entries and their devices' models (catalogue entry and C# source)."""
    out: dict[str, set[str]] = defaultdict(set)
    models: dict[str, set[str]] = {}
    for p in (REPO / "catalog" / "models").glob("*.yaml"):
        data = yaml.safe_load(p.read_text()) or {}
        files = {p.relative_to(REPO).as_posix()}
        if isinstance(data.get("renode"), dict) and data["renode"].get("source"):
            files.add(data["renode"]["source"])
        models[data.get("id", p.stem)] = files
    for sysfile in impact_dir.rglob("*.systems"):
        test = f"tests/sim/{sysfile.stem}.py"
        for sid in set(sysfile.read_text().split()):
            spec_ = selector.systems.get(sid)
            path = REPO / "systems" / f"{sid}.yaml"
            out[test].add(path.relative_to(REPO).as_posix() if path.exists()
                          else f"scripts/speed-systems/{sid}.yaml")
            if not spec_:
                continue
            for role in spec_["roles"]:
                fw = selector.role_fw.get(role, role)
                out[test].add(f"catalog/firmware/{fw}.yaml")
            for model in spec_["devices"]:
                out[test] |= models.get(model, set())
    try:
        from coverage import CoverageData
    except ImportError:
        CoverageData = None
    if CoverageData is not None:
        for data_file in impact_dir.rglob("*.coverage"):
            test = f"tests/sim/{data_file.stem}.py"
            data = CoverageData(basename=str(data_file))
            data.read()
            for measured in data.measured_files():
                rel = measured.split("/vhil/", 1)
                if len(rel) == 2 and data.lines(measured):
                    out[test].add("vhil/" + rel[1])
    return out


def map_gaps(ex: dict[str, set[str]], selector: "cs.Selector") -> dict[str, list[str]]:
    """Exercised path -> the tests/sim files that exercised it but that a
    change to it alone would not select (guard-only for them)."""
    cache: dict[str, set[str]] = {}
    gaps: dict[str, list[str]] = defaultdict(list)
    for test, paths in sorted(ex.items()):
        for path in sorted(paths):
            if path not in cache:
                cache[path] = selector.select([path]).sim_files
            if test not in cache[path]:
                gaps[path].append(test)
    return gaps


# -- report ------------------------------------------------------------------------------------

def report(since, prs, failures, seen, gaps, map_note,
           all_sim: set[str]) -> tuple[str, dict[int, list[Failure]]]:
    flagged: dict[int, list[Failure]] = {}
    lines = ["## Impact audit", "", f"Since {since:%Y-%m-%d %H:%M}Z.", "", "### Nightly runs", ""]
    lines += [f"- {s}" for s in seen] + [""]
    lines += ["### Nightly failures", ""]
    lines += [f"- `{f.label()}`" for f in failures] or ["- none"]
    lines += ["", "### Pull requests merged into dev", ""]
    if not prs:
        lines.append("- none")
    else:
        lines += ["| PR | Label | full-ci selection | Failures outside it |", "|---|---|---|---|"]
    for pr, mode, sel in prs:
        if sel is None:
            lines.append(f"| #{pr['number']} | {mode} | (diff unavailable) | |")
            continue
        outside = [f for f in failures if not in_selection(f, sel)]
        what = (f"{len(sel.sim_files)} sim files; scenarios {','.join(sorted(sel.scenarios)) or '-'}; "
                f"smoke {','.join(sorted(sel.smoke)) or '-'}; IFS_HIL {','.join(sorted(sel.ifs_hil)) or '-'}")
        if sel.sim_files == all_sim and sel.ifs_hil == set(cs.IFS_HIL):
            what = "everything"
        if mode == "impact" and outside:
            flagged[pr["number"]] = outside
        marker = {"impact": "**GAP**: ", "none": "(ran without full-ci) "}.get(mode, "")
        lines.append(f"| #{pr['number']} {pr['title'][:60]} | {mode} | {what} | "
                     + (marker + ", ".join(f"`{f.label()}`" for f in outside) if outside else "-") + " |")
    lines += ["", "### Impact map (ci/impact.yaml against what each test exercised)", ""]
    if map_note:
        lines.append(map_note)
    elif not gaps:
        lines.append("Every exercised path selects the tests that exercised it.")
    else:
        lines += [f"{len(gaps)} exercised path(s) whose change would not select every test that "
                  "exercised them (those tests then rely on the guard):", "",
                  "| Path | Tests not selected |", "|---|---|"]
        for path, tests in sorted(gaps.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            shown = ", ".join(Path(t).stem.removeprefix("test_") for t in tests[:8])
            more = f" (+{len(tests) - 8})" if len(tests) > 8 else ""
            lines.append(f"| `{path}` | {len(tests)}: {shown}{more} |")
    return "\n".join(lines) + "\n", flagged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", help="ISO-8601 (default: the previous audit's start, else 26 h ago)")
    ap.add_argument("--summary", type=Path)
    ap.add_argument("--comment", action="store_true", help="comment on PRs with a selection gap")
    ap.add_argument("--impact-dir", type=Path, help="local sim-logs (default: the nightly's artifacts)")
    args = ap.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    since = (dt.datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since
             else previous_audit(now))
    if since.tzinfo is None:
        since = since.replace(tzinfo=dt.timezone.utc)
    selector = cs.Selector()
    work = Path(tempfile.mkdtemp(prefix="ci-audit-"))

    prs = []
    for pr in merged_prs(since):
        changed = pr_changes(pr["merge_commit_sha"])
        prs.append((pr, pr_mode(pr), None if changed is None else selector.select(changed, "impact")))
    failures, seen = nightly_failures(since, work)

    impact_dir, map_note = args.impact_dir, ""
    if impact_dir is None:
        impact_dir = work / "impact"
        runs = gh_json(f"repos/{REPO_SLUG}/actions/workflows/sim.yml/runs?branch=dev&event=schedule&per_page=1")
        run = (runs.get("workflow_runs") or [None])[0]
        if run:
            try:
                gh("run", "download", str(run["id"]), "-R", REPO_SLUG, "-p", "sim-logs-*", "-D", str(impact_dir))
            except subprocess.CalledProcessError as e:
                map_note = f"No sim-logs artifacts from the last nightly ({e.stderr.strip()[:200]})."
    ex = exercised(impact_dir, selector) if impact_dir.exists() else {}
    if not ex and not map_note:
        map_note = "The last nightly recorded no impact map (VHIL_IMPACT_MAP)."
    gaps = map_gaps(ex, selector)

    text, flagged = report(since, prs, failures, seen, gaps, map_note, set(selector.markers))
    print(text)
    if args.summary:
        with args.summary.open("a") as f:
            f.write(text)
    for number, outside in flagged.items():
        msg = (f"Impact audit: the nightly on dev failed in "
               + ", ".join(f"`{f.label()}`" for f in outside)
               + ", which this PR's `full-ci` selection did not run (ci/impact.yaml). It may be "
               "unrelated (another PR, a flake), or a rule should select it: see docs/ci.md.\n\n"
               + "\n".join(sorted({f'- {f.run_url}' for f in outside if f.run_url})))
        print(f"::warning title=impact selection gap::PR #{number}: "
              + ", ".join(f.label() for f in outside))
        if args.comment:
            try:
                gh("pr", "comment", str(number), "-R", REPO_SLUG, "--body", msg)
            except subprocess.CalledProcessError as e:
                print(f"::warning::could not comment on #{number}: {e.stderr.strip()[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
