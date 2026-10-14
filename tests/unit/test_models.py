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
