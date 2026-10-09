"""vhil.server.githost: tokens reach git through its environment, never its
command line (argv is readable by every process on the host), and secrets
come from *_FILE files (vhil.server.config.env_secret)."""
import base64
import subprocess

import pytest

from vhil.server import githost
from vhil.server.config import env_secret
from vhil.server.githost import GitHubHost, HostError, LsRemote, git_auth_env

TOKEN = "ghs_s3cr3tT0kenValue"
HEADER = "Authorization: Basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


class Recorder:
    """Stands in for subprocess.run; records argv and env of each call."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self.result = (returncode, stdout, stderr)

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw.get("env")))
        rc, out, err = self.result
        return subprocess.CompletedProcess(cmd, rc, out, err)


def leaks(cmd) -> bool:
    return any(TOKEN in str(a) or "x-access-token" in str(a) for a in cmd)


def test_the_token_goes_in_an_extra_header_in_the_environment():
    env = git_auth_env(TOKEN, base={"PATH": "/bin"})
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == HEADER
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["PATH"] == "/bin"


def test_existing_config_entries_in_the_environment_are_kept():
    base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "/work"}
    env = git_auth_env(TOKEN, base=base)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("safe.directory", "/work")
    assert env["GIT_CONFIG_VALUE_1"] == HEADER


def test_without_a_token_no_header_is_added():
    env = git_auth_env(None, base={})
    assert "GIT_CONFIG_COUNT" not in env and env == {"GIT_TERMINAL_PROMPT": "0"}


def test_git_itself_reads_the_header_from_the_environment(tmp_path):
    out = subprocess.run(["git", "config", "--get", "http.https://github.com/.extraheader"],
                         capture_output=True, text=True, cwd=tmp_path,
                         env=git_auth_env(TOKEN, base={"PATH": "/usr/bin:/bin:/usr/local/bin",
                                                       "HOME": str(tmp_path)}))
    assert out.returncode == 0 and out.stdout.strip() == HEADER


def test_push_keeps_the_token_off_the_command_line(tmp_path, monkeypatch):
    run = Recorder()
    monkeypatch.setattr(githost.subprocess, "run", run)
    GitHubHost(TOKEN, "isc-fs/IFS_vHIL").push(tmp_path, "feat/x")
    (cmd, env), = run.calls
    assert not leaks(cmd)
    assert "https://github.com/isc-fs/IFS_vHIL.git" in cmd
    assert env["GIT_CONFIG_VALUE_" + str(int(env["GIT_CONFIG_COUNT"]) - 1)] == HEADER


def test_a_minted_app_token_is_kept_off_the_command_line_too(tmp_path, monkeypatch):
    run = Recorder()
    monkeypatch.setattr(githost.subprocess, "run", run)
    GitHubHost(lambda: TOKEN, "isc-fs/IFS_vHIL").push(tmp_path, "feat/x")
    (cmd, env), = run.calls
    assert not leaks(cmd) and HEADER in env.values()


def test_a_failed_push_does_not_echo_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(githost.subprocess, "run",
                        Recorder(1, stderr=f"fatal: bad credentials {TOKEN}"))
    with pytest.raises(HostError) as e:
        GitHubHost(TOKEN, "isc-fs/IFS_vHIL").push(tmp_path, "feat/x")
    assert TOKEN not in str(e.value)


SHA1 = "a" * 40


def test_ls_remote_keeps_the_token_off_the_command_line(monkeypatch):
    run = Recorder(stdout=f"{SHA1}\trefs/heads/dev\n")
    monkeypatch.setattr(githost.subprocess, "run", run)
    assert LsRemote(TOKEN).refs("isc-fs/IFS08-CE-ECU")["branches"] == [{"name": "dev", "sha": SHA1}]
    (cmd, env), = run.calls
    assert not leaks(cmd) and "https://github.com/isc-fs/IFS08-CE-ECU.git" in cmd
    assert HEADER in env.values()


def test_ls_remote_asks_with_the_apps_read_token_for_that_repo(monkeypatch):
    run = Recorder(stdout=f"{SHA1}\trefs/heads/dev\n")
    monkeypatch.setattr(githost.subprocess, "run", run)
    asked = []
    LsRemote(None, token_for=lambda repo: asked.append(repo) or TOKEN).refs("isc-fs/IFS08-CE-ECU")
    assert asked == ["isc-fs/IFS08-CE-ECU"] and HEADER in run.calls[0][1].values()


def test_ls_remote_falls_back_to_anonymous_for_a_public_repo(monkeypatch):
    """No App token (not installed there), or one the remote refuses: the
    firmware repos are public, so it asks again without."""
    def broken(repo):
        raise RuntimeError("not installed on " + repo)
    run = Recorder(stdout="")
    monkeypatch.setattr(githost.subprocess, "run", run)
    LsRemote(None, token_for=broken).refs("isc-fs/IFS08-CE-ECU")
    assert not any("extraHeader" in str(v) for v in run.calls[0][1].values())

    calls = []

    def flaky(cmd, **kw):
        calls.append(kw["env"])
        authed = any("extraHeader" in str(v) for v in kw["env"].values())
        return subprocess.CompletedProcess(cmd, 128 if authed else 0,
                                           "" if authed else f"{SHA1}\trefs/tags/v1.7.0\n",
                                           f"denied {TOKEN}" if authed else "")
    monkeypatch.setattr(githost.subprocess, "run", flaky)
    assert LsRemote(TOKEN).refs("isc-fs/stm32-can-bootloader")["tags"] == [
        {"name": "v1.7.0", "sha": SHA1}]
    assert len(calls) == 2


def test_ls_remote_is_time_bounded(monkeypatch):
    def slow(cmd, **kw):
        assert kw["timeout"] <= 30
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(githost.subprocess, "run", slow)
    with pytest.raises(HostError, match="timed out"):
        LsRemote(None).refs("isc-fs/IFS08-CE-ECU")


@pytest.mark.parametrize("repo", ["../x", "isc-fs/a b", "-u/x", "a/b/c", "isc-fs/..", ""])
def test_ls_remote_takes_only_an_owner_name_repo(monkeypatch, repo):
    monkeypatch.setattr(githost.subprocess, "run", Recorder())
    with pytest.raises(HostError, match="not an owner/name"):
        LsRemote(None).refs(repo)


def test_ls_remote_output_is_parsed_and_untrusted():
    """Annotated tags resolve to their commit; tags sort as versions; a
    line whose sha or name a system file couldn't hold is dropped."""
    from vhil.server.githost import parse_ls_remote
    b, c, d, e, f = ("b" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40)
    out = (f"{SHA1}\trefs/heads/dev\n{b}\trefs/heads/feat/x\n{c}\trefs/tags/v1.9.0\n"
           f"{d}\trefs/tags/v1.9.0^{{}}\n{e}\trefs/tags/v1.10.0\n{f}\tHEAD\n"
           f"{b}\trefs/tags/v1.10.0-rc1\n"
           f"{b}\trefs/heads/-upload-pack=x\n{b}\trefs/heads/a..b\n{b}\trefs/heads/x y\n"
           f"zz\trefs/heads/badsha\n{b}\trefs/pull/1/head\n{b}\trefs/heads/ok\"<x>\n")
    assert parse_ls_remote(out) == {
        "branches": [{"name": "dev", "sha": SHA1}, {"name": "feat/x", "sha": b}],
        "tags": [{"name": "v1.10.0", "sha": e}, {"name": "v1.10.0-rc1", "sha": b},
                 {"name": "v1.9.0", "sha": d}]}


def test_ls_remote_without_a_token_sends_no_header(monkeypatch):
    run = Recorder(stdout="")
    monkeypatch.setattr(githost.subprocess, "run", run)
    LsRemote(None).refs("isc-fs/IFS08-CE-ECU")
    (cmd, env), = run.calls
    assert not any("extraHeader" in str(v) for v in env.values())


# -- secrets from files ------------------------------------------------------------

def test_a_secret_comes_from_its_file_first(tmp_path, monkeypatch):
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n")
    monkeypatch.setenv("VHIL_X", "from-env")
    assert env_secret("VHIL_X") == "from-env"
    monkeypatch.setenv("VHIL_X_FILE", str(f))
    assert env_secret("VHIL_X") == TOKEN            # trailing newline dropped


def test_a_secret_file_that_cannot_be_read_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("VHIL_X_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="VHIL_X_FILE"):
        env_secret("VHIL_X")


def test_an_empty_secret_file_is_an_unset_secret(tmp_path, monkeypatch):
    (tmp_path / "empty").write_text("")
    monkeypatch.setenv("VHIL_X_FILE", str(tmp_path / "empty"))
    assert env_secret("VHIL_X", "default") == ""


def test_github_login_reads_its_secrets_from_files(tmp_path, monkeypatch):
    from vhil.server.auth import Auth
    (tmp_path / "client").write_text("client-secret\n")
    (tmp_path / "session").write_text("s" * 40 + "\n")
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_ID", "id")
    monkeypatch.delenv("VHIL_GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("VHIL_SESSION_SECRET", raising=False)
    monkeypatch.setenv("VHIL_GITHUB_CLIENT_SECRET_FILE", str(tmp_path / "client"))
    monkeypatch.setenv("VHIL_SESSION_SECRET_FILE", str(tmp_path / "session"))
    a = Auth.from_env("github")
    assert a.client_secret == "client-secret" and a.secret == b"s" * 40


def test_the_push_token_is_read_from_its_file(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from types import SimpleNamespace
    from vhil.server import systems_write
    (tmp_path / "token").write_text(TOKEN)
    monkeypatch.delenv("VHIL_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("VHIL_GITHUB_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("VHIL_GITHUB_REPO", "isc-fs/IFS_vHIL")
    state = SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path), github_app=None)
    host = systems_write._host(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert host.token == TOKEN


# -- active branches, commits of refs ------------------------------------------------

def _branches(*names_shas):
    return [{"name": n, "sha": s * 40} for n, s in names_shas]


def _day(n, now=1_800_000_000.0):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(now - n * 86400, timezone.utc).isoformat()


def test_active_branches_are_pinned_pr_or_recent_and_newest_first():
    from vhil.server.githost import active_branches
    now = 1_800_000_000.0
    branches = _branches(("dev", "a"), ("main", "b"), ("trunk", "c"), ("feat/new", "d"),
                         ("feat/stale", "e"), ("feat/pr", "f"), ("release", "1"))
    details = {"default_branch": "trunk",
               "prs": {"feat/pr": [{"number": 3, "title": "t", "url": "u"}]},
               "commits": {"a" * 40: {"date": _day(100), "author": "x"},
                           "b" * 40: {"date": _day(400), "author": "x"},
                           "c" * 40: {"date": _day(31), "author": "x"},
                           "d" * 40: {"date": _day(29.9), "author": "y"},
                           "e" * 40: {"date": _day(30.1), "author": "z"},
                           "f" * 40: {"date": _day(99), "author": "w"},
                           "1" * 40: {"date": _day(5), "author": "v"}}}
    out = active_branches(branches, details, catalogue_ref="release", days=30, now=now)
    assert [b["name"] for b in out] == ["release", "feat/new", "feat/stale", "trunk",
                                        "feat/pr", "dev", "main"]
    why = {b["name"]: b["why"] for b in out}
    assert why == {"release": ["catalogue", "recent"], "feat/new": ["recent"], "feat/stale": [],
                   "trunk": ["default"], "feat/pr": ["pr"], "dev": ["dev"], "main": ["main"]}
    assert [b["name"] for b in out if b["active"]] == ["release", "feat/new", "trunk", "feat/pr",
                                                       "dev", "main"]
    # No details (the API couldn't be asked): undated, active by name only.
    bare = active_branches(branches, None, catalogue_ref="dev", days=30, now=now)
    assert {b["name"] for b in bare if b["active"]} == {"dev", "main"}
    assert all(b["date"] is None for b in bare)


def test_commit_of_prefers_a_branch_and_takes_a_full_sha():
    from vhil.server.githost import commit_of
    refs = {"branches": _branches(("v1", "a"), ("dev", "b")), "tags": _branches(("v1", "c"))}
    assert commit_of(refs, "v1") == "a" * 40
    assert commit_of(refs, "dev") == "b" * 40
    assert commit_of(refs, "0123456789" * 4) == "0123456789" * 4
    assert commit_of(refs, "nope") is None


def test_a_run_resolves_with_fresh_refs_not_the_cache():
    from vhil.server.githost import CachedRefs, FakeRefLister, fresh_refs
    inner = FakeRefLister({"o/r": {"branches": _branches(("dev", "a")), "tags": []}})
    cached = CachedRefs(inner, ttl_s=300, clock=lambda: 0.0)
    cached.refs("o/r")
    cached.refs("o/r")
    assert inner.calls == ["o/r"]
    inner._refs["o/r"] = {"branches": _branches(("dev", "b")), "tags": []}
    assert fresh_refs(cached, "o/r")["branches"][0]["sha"] == "b" * 40
    assert cached.refs("o/r")["branches"][0]["sha"] == "b" * 40   # and cached
    assert len(inner.calls) == 2


def _api(routes, seen):
    import httpx

    def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


def test_github_branches_reads_prs_and_dates_and_caches_commits():
    pytest.importorskip("httpx")
    from vhil.server.githost import GitHubBranches
    routes = {
        "/repos/o/r": {"default_branch": "dev"},
        "/repos/o/r/pulls": [
            {"number": 4, "title": "Mine", "html_url": "https://github.com/o/r/pull/4",
             "head": {"ref": "feat/x", "repo": {"full_name": "o/r"}}},
            # A fork's branch of the same name is not this repo's.
            {"number": 5, "title": "Fork", "html_url": "u",
             "head": {"ref": "dev", "repo": {"full_name": "someone/r"}}}],
        f"/repos/o/r/commits/{'a' * 40}": {"author": {"login": "raul"},
                                            "commit": {"committer": {"date": "2026-10-01T00:00:00Z"},
                                                       "author": {"name": "Raul"}}},
        f"/repos/o/r/commits/{'b' * 40}": {"author": None,
                                            "commit": {"committer": {"date": "2026-09-01T00:00:00Z"},
                                                       "author": {"name": "Ana"}}},
    }
    seen, t = [], [0.0]
    gh = GitHubBranches(token=TOKEN, transport=_api(routes, seen), ttl_s=60, clock=lambda: t[0])
    branches = _branches(("dev", "a"), ("feat/x", "b"))
    d = gh.details("o/r", branches)
    assert d["default_branch"] == "dev"
    assert d["prs"] == {"feat/x": [{"number": 4, "title": "Mine",
                                    "url": "https://github.com/o/r/pull/4"}]}
    assert d["commits"] == {"a" * 40: {"date": "2026-10-01T00:00:00Z", "author": "raul"},
                            "b" * 40: {"date": "2026-09-01T00:00:00Z", "author": "Ana"}}
    assert all(auth == f"Bearer {TOKEN}" for _, auth in seen) and len(seen) == 4
    # Within the TTL nothing is asked again; past it only the repo and its
    # PRs, never a commit already known.
    gh.details("o/r", branches)
    assert len(seen) == 4
    t[0] = 61
    gh.details("o/r", branches)
    assert [p for p, _ in seen[4:]] == ["/repos/o/r", "/repos/o/r/pulls"]


def test_github_branches_without_a_token_asks_anonymously_and_says_rate_limit():
    pytest.importorskip("httpx")
    import httpx
    from vhil.server.githost import GitHubBranches, HostError
    seen = []
    limited = httpx.Response(403, headers={"x-ratelimit-remaining": "0"},
                             json={"message": "API rate limit exceeded"})
    gh = GitHubBranches(transport=_api({"/repos/o/r": limited}, seen))
    with pytest.raises(HostError, match="rate limit"):
        gh.details("o/r", [])
    assert seen == [("/repos/o/r", None)]
    with pytest.raises(HostError, match="not an owner/name"):
        gh.details("../x", [])
