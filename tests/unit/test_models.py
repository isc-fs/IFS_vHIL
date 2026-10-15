"""vhil/models.py: every model in one assembly, from Renode's compiler cache."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vhil import models
from vhil.system import REPO, System


def test_every_model_is_in_the_one_include():
    cmd = models.include_command()
    names = sorted(p.name for p in (REPO / "models" / "renode").glob("*.cs"))
    assert [Path(a[1:]).name for a in cmd.split()[1:]] == names
    assert "VhilMonitor.cs" in cmd and "VhilProbe.cs" in cmd


def test_the_digest_is_renodes():
    """Monitor.TryCompilePlugin: SHA-256 of the files' bytes in path order,
    upper-case hex; the contents only, not the paths."""
    paths = models.sources()
    h = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(paths, key=str))).hexdigest().upper()
    assert models.digest() == h


def test_another_renode_build_gets_another_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("VHIL_MODELS_CACHE", str(tmp_path))
    a, b = tmp_path / "a" / "renode", tmp_path / "b" / "renode"
    for r in (a, b):
        r.parent.mkdir()
        r.write_text("")
    (b.parent / "VHIL_RENODE_BUILD").write_text("renode 1.17.0 (x) + vhil patches y\n")
    assert models.home(str(a)) != models.home(str(b))
    env = models.env(str(a))
    assert env == {"XDG_CONFIG_HOME": str(models.home(str(a)))}
    assert "compiler-cache-enabled = true" in (models.home(str(a)) / "renode" / "config").read_text()


def test_renodes_shell_keeps_no_history(tmp_path, monkeypatch):
    """Renode's shell rewrote its whole history file after every command, and
    every run sharing the directory added to it (#260): the config points it
    at /dev/null, a config from before is brought up to date, and the history
    it let grow goes."""
    monkeypatch.setenv("VHIL_MODELS_CACHE", str(tmp_path))
    renode = tmp_path / "renode"
    renode.write_text("")
    user = models.home(str(renode)) / "renode"
    user.mkdir(parents=True)
    (user / "config").write_text("[general]\ncompiler-cache-enabled = true\nterminal = Termsharp\n")
    (user / "history").write_text("emulation RunFor \"0.01\"\n" * 1000)
    models.env(str(renode))
    config = (user / "config").read_text()
    assert "history-path = /dev/null" in config and "compiler-cache-enabled = true" in config
    assert not (user / "history").exists()
    # Renode adds its defaults to the file; that one is left as it is.
    (user / "config").write_text(config + "terminal = Termsharp\n[monitor]\nnumber-format = Hexadecimal\n")
    models.env(str(renode))
    assert "[monitor]" in (user / "config").read_text()


def test_prebuilt_mode_fails_loudly_without_the_assembly(tmp_path, monkeypatch):
    monkeypatch.setenv("VHIL_MODELS_CACHE", str(tmp_path))
    monkeypatch.setenv("VHIL_MODELS", "prebuilt")
    renode = tmp_path / "renode"
    renode.write_text("")
    with pytest.raises(models.ModelsError, match="VHIL_MODELS=prebuilt"):
        models.ensure(str(renode))
    cache = models.home(str(renode)) / "renode" / "compiler-cache"
    cache.mkdir(parents=True)
    (cache / models.digest()).write_bytes(b"MZ")
    assert models.ensure(str(renode)) == models.digest()


def test_a_bad_mode_is_refused(monkeypatch):
    monkeypatch.setenv("VHIL_MODELS", "sometimes")
    with pytest.raises(models.ModelsError):
        models.mode()


def test_a_preloaded_script_includes_no_model():
    system = System(REPO / "systems" / "ecu-ams.yaml")
    fw = {b: Path(f"/fw/{b}.elf") for b in system.boards}
    assert "include " not in system.render_renode(fw, lockstep=True, preloaded=True)
    assert "include " in system.render_renode(fw, lockstep=True)


def _fake_renode(tmp_path, body: str) -> str:
    renode = tmp_path / "bin" / "renode"
    renode.parent.mkdir()
    renode.write_text("#!/bin/sh\n" + body)
    renode.chmod(0o755)
    return str(renode)


def test_build_runs_the_monitor_on_the_console():
    """With `-P -1` a failed include's errors went nowhere and Renode never
    quit; on the console it prints them and exits at stdin's end."""
    cmd = models.build_command()
    assert cmd[:5] == ["renode", "--disable-gui", "--plain", "--console", "-e"]
    assert "-P" not in cmd and cmd[-1].endswith("; quit")


def test_a_compile_error_fails_the_build_fast_with_the_compilers_errors(tmp_path, monkeypatch):
    """Renode at a compile error: the errors on the console, the rest of the
    -e line skipped, the monitor reading stdin (/dev/null) to its end."""
    monkeypatch.setenv("VHIL_MODELS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(models, "BUILD_TIMEOUT_S", 20)
    renode = _fake_renode(tmp_path, (
        "echo '[INFO] Including script(s): /work/models/renode/VhilMonitor.cs'\n"
        "printf 'Errors during compilation or loading:\\r\\n'\n"
        "printf \"VhilMonitor.cs(196,21): error CS0104: 'Monitor' is an ambiguous reference\\r\\n\"\n"
        "cat >/dev/null\n"))
    with pytest.raises(models.ModelsError) as e:
        models.build(renode)
    assert "VhilMonitor.cs(196,21): error CS0104: 'Monitor' is an ambiguous reference" in str(e.value)
    assert "\r" not in str(e.value)


def test_a_renode_that_hangs_reports_its_output(tmp_path, monkeypatch):
    monkeypatch.setenv("VHIL_MODELS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(models, "BUILD_TIMEOUT_S", 1)
    renode = _fake_renode(tmp_path, "echo 'X.cs(1,2): error CS0029: nope'\nexec sleep 30\n")
    with pytest.raises(models.ModelsError, match=r"in 1 s:\nX.cs\(1,2\): error CS0029: nope"):
        models.build(renode)


def test_build_stores_and_records_the_assembly(tmp_path, monkeypatch):
    monkeypatch.setenv("VHIL_MODELS_CACHE", str(tmp_path / "cache"))
    sha = models.digest()
    renode = _fake_renode(tmp_path, (
        'mkdir -p "$XDG_CONFIG_HOME/renode/compiler-cache"\n'
        f'printf MZ > "$XDG_CONFIG_HOME/renode/compiler-cache/{sha}"\n'
        "cat >/dev/null\n"))
    assert models.build(renode) == sha
    assert models.built(renode, sha) and sha in models._manifest(renode)
