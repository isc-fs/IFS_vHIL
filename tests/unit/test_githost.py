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


def test_ls_remote_keeps_the_token_off_the_command_line(monkeypatch):
    run = Recorder(stdout="abc\trefs/heads/dev\n")
    monkeypatch.setattr(githost.subprocess, "run", run)
    assert LsRemote(TOKEN).refs("isc-fs/IFS08-CE-ECU")["branches"] == ["dev"]
    (cmd, env), = run.calls
    assert not leaks(cmd) and "https://github.com/isc-fs/IFS08-CE-ECU.git" in cmd
    assert HEADER in env.values()


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
