"""Commit files to branches of the workspace without touching any checkout.

A save from the browser is a commit on a branch (docs/architecture/m5-web-app.md,
principle 1), but the workspace is one git checkout shared by every user and
by the server itself: its working tree and HEAD must not move. So a save never
checks anything out. It builds the commit with git's plumbing in a private
index file:

    blob   = git hash-object -w --stdin                 (the new file)
    index  = git read-tree <parent>                     (GIT_INDEX_FILE = a temp file)
             git update-index --cacheinfo 100644,<blob>,<path>
    tree   = git write-tree
    commit = git commit-tree <tree> -p <parent>         (author from the request)
             git update-ref refs/heads/<branch> <commit> <old>

The last step is a compare-and-swap: if another save moved the branch in
between, it fails and nothing is lost. The parent is the branch's tip, else
the remote's copy of it (origin/<branch>), else the base branch (origin/dev,
dev). A branch checked out in any worktree is refused: moving its ref under
that tree would make the tree look like it reverted the save. `dev` and
`main` are never written; changes reach them through a PR.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

PROTECTED = frozenset({"dev", "main", "master", "HEAD"})
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


class GitError(Exception):
    pass


class BadBranch(ValueError):
    pass


class Conflict(GitError):
    pass


class GitStore:
    def __init__(self, root: Path, base: str = "dev"):
        self.root = Path(root)
        self.base = base
        self._lock = threading.Lock()

    # -- plumbing ---------------------------------------------------------------

    def git(self, *args: str, env: dict | None = None, input: str | bytes | None = None,
            check: bool = True, text: bool = True) -> str | bytes:
        out = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True,
                             input=input, text=text,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
        if check and out.returncode != 0:
            err = out.stderr if text else out.stderr.decode(errors="replace")
            raise GitError(f"git {args[0]}: {err.strip()}")
        return out.stdout.strip() if text else out.stdout

    def rev(self, ref: str) -> str | None:
        """The commit `ref` names, or None."""
        out = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--verify", "--quiet",
                              f"{ref}^{{commit}}"], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None

    # -- branches ---------------------------------------------------------------

    def check_branch(self, branch: str) -> str:
        """`branch` if the app may write it, else BadBranch."""
        if not isinstance(branch, str) or not _BRANCH.match(branch) or branch in PROTECTED:
            raise BadBranch(f"branch '{branch}' is not writable: use a new branch name "
                            f"like feat/<slug> (never {', '.join(sorted(PROTECTED - {'HEAD'}))})")
        out = subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"],
                             capture_output=True, text=True, cwd=tempfile.gettempdir())
        if out.returncode != 0:
            raise BadBranch(f"'{branch}' is not a valid branch name")
        return branch

    def base_commit(self) -> str:
        for ref in (f"refs/remotes/origin/{self.base}", f"refs/heads/{self.base}", "HEAD"):
            sha = self.rev(ref)
            if sha:
                return sha
        raise GitError("the workspace has no commits")

    def parent(self, branch: str | None) -> str:
        """The commit a save on `branch` builds on (and a read of it shows)."""
        if branch:
            for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
                sha = self.rev(ref)
                if sha:
                    return sha
        return self.base_commit()

    def checked_out(self) -> set[str]:
        """Branches checked out in the workspace or any of its worktrees."""
        out = self.git("worktree", "list", "--porcelain")
        return {line.split(" ", 1)[1].removeprefix("refs/heads/")
                for line in out.splitlines() if line.startswith("branch ")}

    # -- content ----------------------------------------------------------------

    def read(self, commit: str, path: str) -> str | None:
        out = subprocess.run(["git", "-C", str(self.root), "cat-file", "blob", f"{commit}:{path}"],
                             capture_output=True)
        return out.stdout.decode() if out.returncode == 0 else None

    def extract(self, commit: str, paths: list[str], dest: Path) -> None:
        """Write `paths` (directories) as of `commit` under `dest`."""
        data = self.git("archive", "--format=tar", commit, "--", *paths, text=False)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, filter="data")
            else:   # Python < 3.11.4; git archive writes no links out of the tree
                tar.extractall(dest)

    def commit_file(self, branch: str, path: str, text: str, message: str,
                    author: tuple[str, str], must_not_exist: bool = False) -> dict:
        """Commit `text` as `path` on `branch` (created from the base if new).
        Returns {ref, branch, parent, created, changed}."""
        self.check_branch(branch)
        if not message.strip():
            raise GitError("a commit needs a message")
        name, email = author
        with self._lock:
            if branch in self.checked_out():
                raise Conflict(f"branch '{branch}' is checked out in the workspace; "
                               f"save to another branch")
            local = self.rev(f"refs/heads/{branch}")
            parent = self.parent(branch)
            if must_not_exist and self.read(parent, path) is not None:
                raise Conflict(f"{path} already exists on {branch if local else self.base}")
            blob = self.git("hash-object", "-w", "--stdin", input=text)
            with tempfile.TemporaryDirectory(prefix="vhil-index-") as tmp:
                env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
                self.git("read-tree", parent, env=env)
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
                tree = self.git("write-tree", env=env)
            if tree == self.git("rev-parse", f"{parent}^{{tree}}"):
                # Nothing to save: no empty commit, and no new branch either.
                return {"ref": parent, "branch": branch, "parent": parent, "created": False,
                        "changed": False}
            who = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                   "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
            commit = self.git("commit-tree", tree, "-p", parent, "-F", "-", input=message,
                              env=who)
            try:
                # Old value "" = the branch must not exist yet.
                self.git("update-ref", "-m", f"vhil: save {path}", f"refs/heads/{branch}",
                         commit, local or "")
            except GitError as e:
                raise Conflict(f"branch '{branch}' moved during the save; retry ({e})")
            return {"ref": commit, "branch": branch, "parent": parent, "created": local is None,
                    "changed": True}
