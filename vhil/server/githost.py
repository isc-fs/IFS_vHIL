"""Where branches are pushed and PRs opened, and where firmware refs are listed.

Three seams, each with a GitHub implementation and a fake for tests:

  GitHost        push a workspace branch, open a PR against the base branch
  RefLister      a firmware repo's branches and tags, with their commits (git ls-remote)
  BranchDetails  its default branch, open PRs and each head commit's date and
                 author (the GitHub REST API), for the picker's active branches

GitHubHost pushes and opens PRs with the GitHub App's installation token for
this repository when the App is configured (vhil.server.github_app, M5.5),
else with VHIL_GITHUB_TOKEN. Without either the host is unavailable and the
API says so (409); nothing is pushed.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Union

from vhil.system import REF

# A token, or a function returning one when it is needed (an App installation
# token is minted on use and renewed before it expires).
Token = Union[str, Callable[[], str], None]


class HostUnavailable(Exception):
    """No credentials: the operation can't be attempted (HTTP 409)."""


class HostError(Exception):
    """The host refused or failed (HTTP 502)."""


def _scrub(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def git_auth_env(token: str | None, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a git subprocess that authenticates to github.com
    with `token`: `base` (default os.environ) plus an `http.extraHeader`
    entry through GIT_CONFIG_COUNT/KEY/VALUE (git >= 2.31), appended after
    any entries `base` already carries (the dev compose sets safe.directory
    that way).

    The token never goes on git's command line or into a URL: argv is
    readable by every process on the host (ps, /proc/<pid>/cmdline) and git
    echoes URLs in its errors, while a process's environment is readable only
    by its own user and root.
    """
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        try:
            n = int(env.get("GIT_CONFIG_COUNT") or 0)
        except ValueError:
            n = 0
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env[f"GIT_CONFIG_KEY_{n}"] = "http.https://github.com/.extraHeader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"Authorization: Basic {basic}"
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


def repo_slug(workspace: Path) -> str:
    """owner/name of the repository the workspace pushes to."""
    if os.environ.get("VHIL_GITHUB_REPO"):
        return os.environ["VHIL_GITHUB_REPO"]
    out = subprocess.run(["git", "-C", str(workspace), "remote", "get-url", "origin"],
                         capture_output=True, text=True)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", out.stdout.strip())
    return m.group(1) if m else "isc-fs/IFS_vHIL"


# -- pushing and PRs --------------------------------------------------------------

class GitHost(ABC):
    @abstractmethod
    def push(self, workspace: Path, branch: str) -> None:
        """Push refs/heads/<branch> of the workspace to the same name upstream."""

    @abstractmethod
    def open_pr(self, branch: str, base: str, title: str, body: str) -> str:
        """Open (or find the open) PR of `branch` into `base`; its URL."""


class GitHubHost(GitHost):
    def __init__(self, token: Token, repo: str, api: str = "https://api.github.com"):
        self.token, self.repo, self.api = token, repo, api

    def _need_token(self) -> str:
        if not self.token:
            raise HostUnavailable(
                "no GitHub credentials: configure the GitHub App (VHIL_GITHUB_APP_ID / "
                "VHIL_GITHUB_APP_KEY) or set VHIL_GITHUB_TOKEN (contents + pull requests write "
                f"on {self.repo}) for the API service. "
                "The branch was saved in the workspace; push it and open the PR by hand.")
        if callable(self.token):
            try:
                return self.token()
            except Exception as e:  # noqa: BLE001 - the App's own errors, network
                raise HostError(f"no GitHub App token for {self.repo}: {e}")
        return self.token

    def push(self, workspace: Path, branch: str) -> None:
        token = self._need_token()
        url = f"https://github.com/{self.repo}.git"
        out = subprocess.run(["git", "-C", str(workspace), "-c", "credential.helper=",
                              "push", "--porcelain", url, f"refs/heads/{branch}:refs/heads/{branch}"],
                             capture_output=True, text=True, env=git_auth_env(token))
        if out.returncode != 0:
            raise HostError(_scrub(f"push of {branch} failed: {out.stderr.strip()}", token))

    def open_pr(self, branch: str, base: str, title: str, body: str) -> str:
        import httpx
        token = self._need_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        with httpx.Client(base_url=self.api, headers=headers, timeout=30) as http:
            r = http.post(f"/repos/{self.repo}/pulls",
                          json={"head": branch, "base": base, "title": title, "body": body})
            if r.status_code == 201:
                return r.json()["html_url"]
            if r.status_code == 422 and "already exists" in r.text:
                owner = self.repo.split("/")[0]
                found = http.get(f"/repos/{self.repo}/pulls",
                                 params={"head": f"{owner}:{branch}", "base": base, "state": "open"})
                if found.status_code == 200 and found.json():
                    return found.json()[0]["html_url"]
            raise HostError(_scrub(f"GitHub refused the PR ({r.status_code}): {r.text[:500]}",
                                   token))


class FakeGitHost(GitHost):
    """Records what would have been pushed and opened. For tests."""

    def __init__(self, available: bool = True):
        self.available = available
        self.pushed: list[str] = []
        self.prs: list[dict] = []

    def push(self, workspace: Path, branch: str) -> None:
        if not self.available:
            raise HostUnavailable("fake host without credentials")
        self.pushed.append(branch)

    def open_pr(self, branch: str, base: str, title: str, body: str) -> str:
        self.prs.append({"branch": branch, "base": base, "title": title, "body": body})
        return f"https://github.example/pr/{len(self.prs)}"


# -- firmware refs ------------------------------------------------------------------

# A ref is {"name": <branch or tag>, "sha": <commit>}.
Refs = dict[str, list[dict[str, str]]]
# What a remote may answer with and still be shown: a commit id, and a name a
# system file's firmware_ref / bootloader_ref could hold (vhil.system.REF).
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
MAX_REFS = 1000                  # per kind: a picker, not a mirror
LS_REMOTE_TIMEOUT_S = 15
REPO = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")  # owner/name


class RefLister(ABC):
    @abstractmethod
    def refs(self, repo: str) -> Refs:
        """{"branches": [{name, sha}...], "tags": [{name, sha}...]} of an
        owner/name repository; a tag's sha is the commit it points at."""


def _version_key(name: str) -> list:
    """v1.10.0 after v1.9.0 (digit runs compare as numbers), and v1.6.2 after
    v1.6.2-rc1 (a "-" suffix is a pre-release)."""
    key = [(0, int(p), "") if p.isdigit() else (1, 0 if p.startswith("-") else 2, p)
           for p in re.split(r"(\d+)", name) if p]
    return key + [(1, 1, "")]


def parse_ls_remote(text: str) -> Refs:
    """`git ls-remote --heads --tags` output as refs. The remote is not
    trusted: a line whose sha or name isn't one a system file could hold
    (vhil.system.REF) is dropped, and each kind is capped at MAX_REFS."""
    heads, tags, peeled = {}, {}, {}
    for line in text.splitlines():
        sha, _, ref = line.partition("\t")
        if not SHA.fullmatch(sha):
            continue
        if ref.startswith("refs/heads/"):
            name, into = ref.removeprefix("refs/heads/"), heads
        elif ref.startswith("refs/tags/"):
            name, into = ref.removeprefix("refs/tags/"), tags
            if name.endswith("^{}"):              # an annotated tag's commit
                name, into = name[:-3], peeled
        else:
            continue
        if REF.fullmatch(name):
            into[name] = sha
    as_list = lambda d, names: [{"name": n, "sha": d[n]} for n in names][:MAX_REFS]
    tags = {n: peeled.get(n, sha) for n, sha in tags.items()}
    return {"branches": as_list(heads, sorted(heads)),
            "tags": as_list(tags, sorted(tags, key=_version_key, reverse=True))}


class LsRemote(RefLister):
    """git ls-remote on github.com, time-bounded. The firmware repos are
    public, so it needs no credentials; with the GitHub App configured it
    asks with a read-only token for that repo (`token_for`), else with
    `token` (VHIL_GITHUB_TOKEN), for a private one. If the authenticated
    call fails it tries once without: a public repo the App isn't installed
    on still lists."""

    def __init__(self, token: str | None = None,
                 token_for: Callable[[str], str] | None = None):
        self.token, self.token_for = token, token_for

    def _token(self, repo: str) -> str | None:
        if self.token_for is not None:
            try:
                return self.token_for(repo)
            except Exception:  # noqa: BLE001 - not in the App's org, not installed
                pass
        return self.token

    def _ls_remote(self, repo: str, token: str | None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-c", "credential.helper=", "ls-remote", "--heads", "--tags",
                 f"https://github.com/{repo}.git"],
                capture_output=True, text=True, timeout=LS_REMOTE_TIMEOUT_S,
                # Outside any checkout: needs none, and a broken one in the
                # cwd (a mounted worktree) makes git fail.
                cwd=tempfile.gettempdir(), env=git_auth_env(token))
        except subprocess.TimeoutExpired:
            raise HostError(f"ls-remote {repo} timed out after {LS_REMOTE_TIMEOUT_S} s")

    def refs(self, repo: str) -> Refs:
        if not REPO.fullmatch(repo) or ".." in repo:
            raise HostError(f"{repo!r} is not an owner/name repository")
        token = self._token(repo)
        out = self._ls_remote(repo, token)
        if out.returncode != 0 and token:
            out, failed = self._ls_remote(repo, None), out
            if out.returncode != 0:
                out = failed
        if out.returncode != 0:
            hint = "" if token else " (a private repo needs the GitHub App or VHIL_GITHUB_TOKEN)"
            raise HostError(_scrub(f"ls-remote {repo} failed{hint}: {out.stderr.strip()}",
                                   token))
        return parse_ls_remote(out.stdout)


class FakeRefLister(RefLister):
    def __init__(self, refs: dict[str, Refs]):
        self._refs, self.calls = refs, []

    def refs(self, repo: str) -> Refs:
        self.calls.append(repo)
        if repo not in self._refs:
            raise HostError(f"ls-remote {repo} failed: not found")
        return self._refs[repo]


class CachedRefs(RefLister):
    """Remembers each repo's refs for `ttl_s`: the picker asks on every open."""

    def __init__(self, inner: RefLister, ttl_s: float = 300.0, clock=time.monotonic):
        self.inner, self.ttl_s, self.clock = inner, ttl_s, clock
        self._cache: dict[str, tuple[float, dict]] = {}

    def refs(self, repo: str) -> Refs:
        hit = self._cache.get(repo)
        if hit and self.clock() - hit[0] < self.ttl_s:
            return hit[1]
        return self.fresh(repo)

    def fresh(self, repo: str) -> Refs:
        """Asked now, not from the cache (and cached): what a run resolves
        its refs with, so a branch pushed a minute ago runs its new head."""
        refs = self.inner.refs(repo)
        self._cache[repo] = (self.clock(), refs)
        return refs


def fresh_refs(lister: RefLister, repo: str) -> Refs:
    return lister.fresh(repo) if isinstance(lister, CachedRefs) else lister.refs(repo)


def commit_of(refs: Refs, name: str) -> str | None:
    """The commit a branch or tag name is at (a branch wins, as for `git
    clone -b`); a full commit id is its own. None if the repo has no such ref."""
    if re.fullmatch(r"[0-9a-f]{40}", name):
        return name
    for kind in ("branches", "tags"):
        for r in refs.get(kind, []):
            if r["name"] == name:
                return r["sha"]
    return None


# -- branch details (GitHub REST API) ---------------------------------------------

API = "https://api.github.com"
API_TIMEOUT_S = 10
MAX_COMMIT_LOOKUPS = 60          # head commits asked per listing; more go undated


class BranchDetails(ABC):
    @abstractmethod
    def details(self, repo: str, branches: list[dict]) -> dict:
        """{"default_branch": name, "prs": {branch: [{number, title, url}]},
        "commits": {sha: {"date": ISO 8601, "author": login or name}}} for
        the branches given ([{name, sha}], from RefLister). Raises
        HostUnavailable / HostError when the API can't be asked."""


class GitHubBranches(BranchDetails):
    """The GitHub REST API, with the App's read-only token for the repo when
    it is configured (`token_for`), else `token` (VHIL_GITHUB_TOKEN), else
    anonymously: the firmware repos are public, and an anonymous client may
    ask 60 times an hour, which the caches below keep well within. A head
    commit's date and author never change, so they are remembered for good
    (bounded); the default branch and the open PRs, for `ttl_s`."""

    def __init__(self, token: str | None = None, token_for: Callable[[str], str] | None = None,
                 api: str = API, ttl_s: float = 120.0, clock=time.monotonic,
                 transport=None):
        self.token, self.token_for, self.api = token, token_for, api.rstrip("/")
        self.ttl_s, self.clock, self.transport = ttl_s, clock, transport
        self._commits: dict[str, dict] = {}
        self._repo: dict[str, tuple[float, dict]] = {}

    def _token(self, repo: str) -> str | None:
        if self.token_for is not None:
            try:
                return self.token_for(repo)
            except Exception:  # noqa: BLE001 - not installed there: ask anonymously
                pass
        return self.token

    def _client(self, token: str | None):
        import httpx
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(base_url=self.api, headers=headers, timeout=API_TIMEOUT_S,
                            transport=self.transport)

    @staticmethod
    def _get(http, path: str, token: str | None, **params):
        import httpx
        try:
            r = http.get(path, params=params or None)
        except httpx.HTTPError as e:
            raise HostError(f"GitHub API {path}: {type(e).__name__}") from None
        if r.status_code != 200:
            limited = r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0"
            raise HostError(_scrub(f"GitHub API {path}: {r.status_code}"
                                   + (" (rate limit; configure the GitHub App or "
                                      "VHIL_GITHUB_TOKEN)" if limited else "")
                                   + f" {r.text[:200]}", token))
        return r.json()

    def details(self, repo: str, branches: list[dict]) -> dict:
        if not REPO.fullmatch(repo) or ".." in repo:
            raise HostError(f"{repo!r} is not an owner/name repository")
        token = self._token(repo)
        with self._client(token) as http:
            hit = self._repo.get(repo)
            if hit and self.clock() - hit[0] < self.ttl_s:
                info = hit[1]
            else:
                meta = self._get(http, f"/repos/{repo}", token)
                pulls = self._get(http, f"/repos/{repo}/pulls", token, state="open",
                                  per_page=100)
                prs: dict[str, list[dict]] = {}
                for pr in pulls if isinstance(pulls, list) else []:
                    head = pr.get("head") or {}
                    # A fork's branch of the same name is not this repo's.
                    if ((head.get("repo") or {}).get("full_name") or "").lower() != repo.lower():
                        continue
                    prs.setdefault(str(head.get("ref")), []).append(
                        {"number": int(pr["number"]), "title": str(pr.get("title", ""))[:200],
                         "url": str(pr.get("html_url", ""))})
                info = {"default_branch": str(meta.get("default_branch") or ""), "prs": prs}
                self._repo[repo] = (self.clock(), info)
            wanted = [b["sha"] for b in branches if b["sha"] not in self._commits]
            for sha in wanted[:MAX_COMMIT_LOOKUPS]:
                c = self._get(http, f"/repos/{repo}/commits/{sha}", token)
                commit = c.get("commit") or {}
                when = (commit.get("committer") or {}).get("date") or \
                    (commit.get("author") or {}).get("date")
                who = (c.get("author") or {}).get("login") or (commit.get("author") or {}).get("name")
                if len(self._commits) > 5000:
                    self._commits.clear()
                self._commits[sha] = {"date": when, "author": who}
        return {**info, "commits": {b["sha"]: self._commits[b["sha"]] for b in branches
                                    if b["sha"] in self._commits}}


class FakeBranchDetails(BranchDetails):
    def __init__(self, details: dict[str, dict] | None = None, error: Exception | None = None):
        self._details, self.error, self.calls = details or {}, error, []

    def details(self, repo: str, branches: list[dict]) -> dict:
        self.calls.append(repo)
        if self.error is not None:
            raise self.error
        if repo not in self._details:
            raise HostError(f"GitHub API /repos/{repo}: 404")
        return self._details[repo]


def active_branches(branches: list[dict], details: dict | None, *, catalogue_ref: str,
                    days: float, now: float | None = None) -> list[dict]:
    """Every branch with what the picker shows of it, most recent first:
    `date`, `author`, `prs` and `active` with the reasons (`why`): the repo's
    default branch, `dev` and `main`, the catalogue's ref, a branch with an
    open PR, a head commit within the last `days`. Without details (the API
    could not be asked) a branch has no date and is active only for its
    name; the caller then shows every branch."""
    now = time.time() if now is None else now
    details = details or {}
    commits, prs = details.get("commits") or {}, details.get("prs") or {}
    pinned = {"dev", "main", catalogue_ref, details.get("default_branch") or ""}
    out = []
    for b in branches:
        c = commits.get(b["sha"]) or {}
        when = _epoch(c.get("date"))
        why = []
        if b["name"] == details.get("default_branch"):
            why.append("default")
        elif b["name"] in pinned:
            why.append(b["name"] if b["name"] in ("dev", "main") else "catalogue")
        if prs.get(b["name"]):
            why.append("pr")
        if when is not None and now - when <= days * 86400:
            why.append("recent")
        out.append({**b, "date": c.get("date"), "author": c.get("author"),
                    "prs": prs.get(b["name"], []), "active": bool(why), "why": why})
    out.sort(key=lambda r: (-(_epoch(r["date"]) or 0), r["name"]))
    return out


def _epoch(text) -> float | None:
    from datetime import datetime
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
