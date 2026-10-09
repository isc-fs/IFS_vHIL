"""Host-only tests reach no network: a run's creation resolves its firmware
refs (git ls-remote) and the firmware picker asks the GitHub API, so both
fail here unless a test hands the app a fake (or patches them itself,
as tests/unit/test_githost.py does)."""
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    try:
        from vhil.server import githost
    except ImportError:          # no server dependencies (the editor's CI image)
        return

    real = subprocess.run

    def run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "no network in unit tests")
        return real(cmd, *a, **k)

    client = githost.GitHubBranches._client

    def no_api(self, token):
        if self.transport is None:      # a test's MockTransport is fine
            raise githost.HostError("no network in unit tests")
        return client(self, token)

    monkeypatch.setattr(githost.subprocess, "run", run)
    monkeypatch.setattr(githost.GitHubBranches, "_client", no_api)
