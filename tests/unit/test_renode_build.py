"""vhil.renode.build_id: which Renode a run used (docker/renode/build.sh
writes VHIL_RENODE_BUILD beside the launcher), and launch() logging it."""
import io
import subprocess

from vhil import renode as rn


def _launcher(tmp_path, build_line=None):
    d = tmp_path / "renode"
    d.mkdir()
    exe = d / "renode"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    if build_line is not None:
        (d / rn.BUILD_FILE).write_text(build_line)
    return exe


def test_a_patched_build_names_its_patch_set(tmp_path):
    line = "renode 1.17.0 (ab721d88e135) + vhil patches 1.17.0-vhil.1 [0001,0002,0003] queue 0123456789ab"
    exe = _launcher(tmp_path, line + "\n")
    assert rn.build_id(str(exe)) == line


def test_a_release_package_says_so(tmp_path):
    assert rn.build_id(str(_launcher(tmp_path))) == rn.RELEASE_BUILD
    assert rn.build_id(str(tmp_path / "nowhere" / "renode")) == rn.RELEASE_BUILD
    (tmp_path / "empty").mkdir()
    assert rn.build_id(str(_launcher(tmp_path / "empty", "\n"))) == rn.RELEASE_BUILD


def test_a_symlinked_launcher_is_followed(tmp_path):
    exe = _launcher(tmp_path, "renode 1.17.0 + vhil patches X\n")
    link = tmp_path / "bin-renode"
    link.symlink_to(exe)
    assert rn.build_id(str(link)) == "renode 1.17.0 + vhil patches X"


def test_the_line_is_one_line_of_printable_text(tmp_path):
    exe = _launcher(tmp_path, "renode \x1b[31mred\x07\nsecond line\n")
    assert rn.build_id(str(exe)) == "renode ?[31mred?"


def test_launch_writes_the_build_first_in_the_log(tmp_path, monkeypatch):
    exe = _launcher(tmp_path, "renode 1.17.0 + vhil patches X\n")
    started = {}

    def fake_popen(cmd, **kw):
        started["cmd"] = cmd
        return "proc"

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    from vhil import models
    monkeypatch.setattr(models, "ensure", lambda renode: "X")    # no Renode to compile with
    monkeypatch.setenv("VHIL_MODELS_CACHE", str(tmp_path / "models"))
    log = io.StringIO()
    assert rn.launch(str(exe), 40123, stdout=log) == "proc"
    assert log.getvalue() == "vhil: renode 1.17.0 + vhil patches X\n"
    assert started["cmd"][0] == str(exe)
    binlog = io.BytesIO()
    binlog.mode = "wb"
    rn.launch(str(exe), 40123, stdout=binlog)
    assert binlog.getvalue() == b"vhil: renode 1.17.0 + vhil patches X\n"
    rn.launch(str(exe), 40123)   # DEVNULL: nothing to write to
