"""The firmware image cache (vhil/fwcache.py) and System.build_firmware with it."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vhil import fwcache
from vhil import system as vs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _fw(**build):
    return {"id": "toy", "repo": "isc-fs/toy", "submodules": False,
            "build": {"toolchain": "Arm GNU 14.2.Rel1", "configure": "true",
                      "build": "mkdir -p out && cat main.c > out/toy.elf && echo built >> ../log",
                      "elf": "out/toy.elf", "recipe_source": "test", **build}}


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A local repository standing in for github.com/isc-fs/toy."""
    repo = tmp_path / "remote" / "isc-fs" / "toy"
    repo.mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "dev", str(repo)], check=True)
    (repo / "main.c").write_text("int main(void) { return 0; }\n")
    subprocess.run([*git, "add", "main.c"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "one"], check=True)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{tmp_path / 'remote'}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    # A flat .bin without the Arm toolchain: the ELF reversed.
    monkeypatch.setattr(vs, "_objcopy",
                        lambda elf: elf.with_suffix(".bin").write_bytes(elf.read_bytes()[::-1]))
    return SimpleNamespace(path=repo, git=git)


def _build(tmp_path, fw, ref="dev"):
    board = SimpleNamespace(name="toy", firmware=fw, bootloader=None, ref=lambda image: ref)
    owner = SimpleNamespace(boards={"toy": board})
    return vs.System.build_firmware(owner, tmp_path / "fw", log=lambda m: None)["toy"]


def _builds(tmp_path):
    log = tmp_path / "fw" / "log"
    return len(log.read_text().splitlines()) if log.is_file() else 0


def test_without_the_cache_every_build_runs(tmp_path, remote, monkeypatch):
    monkeypatch.delenv(fwcache.ENV, raising=False)
    _build(tmp_path, _fw())
    _build(tmp_path, _fw())
    assert _builds(tmp_path) == 2


def test_a_hit_gives_the_built_image_without_building(tmp_path, remote, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv(fwcache.ENV, str(store))
    elf = _build(tmp_path, _fw())
    first = (elf.read_bytes(), elf.with_suffix(".bin").read_bytes())
    assert _builds(tmp_path) == 1
    assert (store / ".added").read_text().count("\n") == 1
    again = _build(tmp_path, _fw())
    assert again == elf and _builds(tmp_path) == 1
    assert (again.read_bytes(), again.with_suffix(".bin").read_bytes()) == first
    # The checkout is still there, at the commit: candef reads it.
    assert (again.parent.parent / "main.c").is_file()


def test_a_new_commit_recipe_or_toolchain_misses(tmp_path, remote, monkeypatch):
    monkeypatch.setenv(fwcache.ENV, str(tmp_path / "store"))
    _build(tmp_path, _fw())
    (remote.path / "main.c").write_text("int main(void) { return 1; }\n")
    subprocess.run([*remote.git, "commit", "-q", "-am", "two"], check=True)
    elf = _build(tmp_path, _fw())
    assert _builds(tmp_path) == 2 and b"return 1" in elf.read_bytes()
    _build(tmp_path, _fw(configure="true # another recipe"))
    assert _builds(tmp_path) == 3
    monkeypatch.setattr(fwcache, "_toolchain", {"arm-none-eabi-gcc": "another"})
    _build(tmp_path, _fw(configure="true # another recipe"))
    assert _builds(tmp_path) == 4


def test_a_damaged_entry_fails_loudly(tmp_path, remote, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv(fwcache.ENV, str(store))
    _build(tmp_path, _fw())
    entry = next(p for p in store.iterdir() if p.is_dir())
    (entry / "image.elf").write_bytes(b"not what was built")
    with pytest.raises(vs.SystemError, match="SHA-256"):
        _build(tmp_path, _fw())


def test_an_entry_under_another_key_fails_loudly(tmp_path, remote, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv(fwcache.ENV, str(store))
    _build(tmp_path, _fw())
    entry = next(p for p in store.iterdir() if p.is_dir())
    meta = json.loads((entry / fwcache.ENTRY).read_text())
    meta["key"]["commit"] = "0" * 40
    (entry / fwcache.ENTRY).write_text(json.dumps(meta))
    with pytest.raises(vs.SystemError, match="key"):
        _build(tmp_path, _fw())


def test_prune_keeps_what_was_used(tmp_path, remote, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv(fwcache.ENV, str(store))
    _build(tmp_path, _fw())
    (store / "toy+000000000000.stale").mkdir()
    kept = fwcache.prune(store)
    assert len(kept) == 1 and not (store / "toy+000000000000.stale").exists()
