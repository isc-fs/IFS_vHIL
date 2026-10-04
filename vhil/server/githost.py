"""Where branches are pushed and PRs opened, and where firmware refs are listed.

Two seams, each with a GitHub implementation and a fake for tests:

  GitHost     push a workspace branch, open a PR against the base branch
  RefLister   a firmware repo's branches and tags (git ls-remote)

The GitHub implementations take a token from the environment
(VHIL_GITHUB_TOKEN) for now. M5.5 swaps in GitHub App installation tokens
(docs/architecture/m5-web-app.md, "Auth"): only `token` changes. Without a
token the host is unavailable and the API says so (409); nothing is pushed.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path


class HostUnavailable(Exception):
    """No credentials: the operation can't be attempted (HTTP 409)."""


class HostError(Exception):
    """The host refused or failed (HTTP 502)."""


def _scrub(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


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
    def __init__(self, token: str | None, repo: str, api: str = "https://api.github.com"):
        self.token, self.repo, self.api = token, repo, api

    def _need_token(self) -> str:
        if not self.token:
            raise HostUnavailable(
                "no GitHub credentials: set VHIL_GITHUB_TOKEN (contents + pull requests write "
                f"on {self.repo}) for the API service, or wait for the GitHub App (M5.5). "
                "The branch was saved in the workspace; push it and open the PR by hand.")
        return self.token

    def push(self, workspace: Path, branch: str) -> None:
        token = self._need_token()
        url = f"https://x-access-token:{token}@github.com/{self.repo}.git"
        out = subprocess.run(["git", "-C", str(workspace), "-c", "credential.helper=",
                              "push", "--porcelain", url, f"refs/heads/{branch}:refs/heads/{branch}"],
                             capture_output=True, text=True,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
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

class RefLister(ABC):
    @abstractmethod
    def refs(self, repo: str) -> dict[str, list[str]]:
        """{"branches": [...], "tags": [...]} of an owner/name repository."""


def parse_ls_remote(text: str) -> dict[str, list[str]]:
    branches, tags = [], []
    for line in text.splitlines():
        _, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            branches.append(ref.removeprefix("refs/heads/"))
        elif ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags.append(ref.removeprefix("refs/tags/"))
    return {"branches": sorted(branches), "tags": sorted(tags, reverse=True)}


class LsRemote(RefLister):
    """git ls-remote on github.com, with the token for private firmware repos."""

    def __init__(self, token: str | None = None):
        self.token = token

    def refs(self, repo: str) -> dict[str, list[str]]:
        auth = f"x-access-token:{self.token}@" if self.token else ""
        out = subprocess.run(["git", "-c", "credential.helper=", "ls-remote", "--heads", "--tags",
                              f"https://{auth}github.com/{repo}.git"],
                             capture_output=True, text=True, timeout=30,
                             # Outside any checkout: needs none, and a broken
                             # one in the cwd (a mounted worktree) makes git fail.
                             cwd=tempfile.gettempdir(),
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if out.returncode != 0:
            hint = "" if self.token else " (a private repo needs VHIL_GITHUB_TOKEN)"
            raise HostError(_scrub(f"ls-remote {repo} failed{hint}: {out.stderr.strip()}",
                                   self.token))
        return parse_ls_remote(out.stdout)


class FakeRefLister(RefLister):
    def __init__(self, refs: dict[str, dict[str, list[str]]]):
        self._refs, self.calls = refs, []

    def refs(self, repo: str) -> dict[str, list[str]]:
        self.calls.append(repo)
        if repo not in self._refs:
            raise HostError(f"ls-remote {repo} failed: not found")
        return self._refs[repo]


class CachedRefs(RefLister):
    """Remembers each repo's refs for `ttl_s`: the picker asks on every open."""

    def __init__(self, inner: RefLister, ttl_s: float = 60.0, clock=time.monotonic):
        self.inner, self.ttl_s, self.clock = inner, ttl_s, clock
        self._cache: dict[str, tuple[float, dict]] = {}

    def refs(self, repo: str) -> dict[str, list[str]]:
        hit = self._cache.get(repo)
        if hit and self.clock() - hit[0] < self.ttl_s:
            return hit[1]
        refs = self.inner.refs(repo)
        self._cache[repo] = (self.clock(), refs)
        return refs
