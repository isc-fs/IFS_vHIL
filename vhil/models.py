"""The vHIL's C# models (models/renode/*.cs) as one assembly, compiled once.

Every Renode the vHIL starts (`vhil.renode.launch`: native tests, scenario
runs, the wall-clock bench, speed.py) loads every model at start, in one
`include`. Compiling them is Renode's own `AdHocCompiler` (Roslyn, every
loaded assembly referenced): ~1.4 s and ~100 MB a process kept (#260). So
the assembly is compiled once and Renode loads it from its compiler cache
(`EmulationManager.CompiledFilesCache`, `[general] compiler-cache-enabled`):
`include` of a set of `.cs` files hashes their contents (SHA-256 of the
files' bytes in path order, `Monitor.TryCompilePlugin`) and loads the
assembly stored under that hash instead of compiling. The cache is content
addressed, so an assembly built from other sources is never loaded: a change
to any model is a different hash.

Where it lives: one Renode user directory per Renode build (its config, with
the cache on, and `compiler-cache/`), under VHIL_MODELS_CACHE (default
`~/.cache/vhil/renode-models`); Renode finds it through XDG_CONFIG_HOME,
which `env()` sets for Renode's process only. An assembly references the
Renode it was compiled against, so another build gets a directory of its own.

Two modes (VHIL_MODELS):

- unset or `auto` (local iteration): `ensure()` compiles the models into the
  cache when their hash is not there (the first start after a change) and
  every later start loads it;
- `prebuilt` (CI, the web app's workers): the assembly must already be there,
  built from the same sources (`python -m vhil.models build`, run from the
  same checkout: CI's step after the checkout, a worker at its start).
  A start whose sources' hash is not in the cache fails, naming both, instead
  of compiling.

    python -m vhil.models build     compile into the cache (no-op when there)
    python -m vhil.models check     exit 1 unless the current sources are built
    python -m vhil.models info      the hash, the directory, what is built
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO / "models" / "renode"
DEFAULT_CACHE = Path.home() / ".cache" / "vhil" / "renode-models"
MANIFEST = "vhil-models.json"
CONFIG = "[general]\ncompiler-cache-enabled = true\n"
BUILD_TIMEOUT_S = 600


class ModelsError(RuntimeError):
    pass


def sources() -> list[Path]:
    """Every model, in the order Renode hashes them (Array.Sort of the paths:
    one directory, ASCII names, so the names' ordinal order)."""
    return sorted(MODELS_DIR.glob("*.cs"), key=lambda p: p.as_posix())


def digest(paths: list[Path] | None = None) -> str:
    """The hash Renode files the assembly under (Monitor.TryCompilePlugin:
    SHA-256 of every file's bytes concatenated in path order, upper-case hex).
    It depends on the contents only, not on where the checkout is."""
    h = hashlib.sha256()
    for p in sorted(paths or sources(), key=lambda p: p.as_posix()):
        h.update(p.read_bytes())
    return h.hexdigest().upper()


def include_command(paths: list[Path] | None = None) -> str:
    from vhil import renode as rn
    return "include " + " ".join(rn.file_arg(p) for p in (paths or sources()))


def mode() -> str:
    value = os.environ.get("VHIL_MODELS", "auto") or "auto"
    if value not in ("auto", "prebuilt"):
        raise ModelsError(f"VHIL_MODELS={value!r}: expected 'auto' or 'prebuilt'")
    return value


def _renode_key(renode: str) -> str:
    from vhil import renode as rn
    found = Path(shutil.which(renode) or renode).resolve()
    return hashlib.sha256(f"{found}\n{rn.build_id(renode)}".encode()).hexdigest()[:16]


def home(renode: str) -> Path:
    """XDG_CONFIG_HOME for `renode`: Renode's user directory is <home>/renode."""
    root = Path(os.environ.get("VHIL_MODELS_CACHE") or DEFAULT_CACHE)
    return root / _renode_key(renode)


def env(renode: str) -> dict[str, str]:
    """Environment for Renode's process: its user directory, with the
    compiler cache on. Written on first use."""
    h = home(renode)
    user = h / "renode"
    user.mkdir(parents=True, exist_ok=True)
    config = user / "config"
    if not config.exists():
        tmp = user / f".config.{os.getpid()}"
        tmp.write_text(CONFIG)
        os.replace(tmp, config)
    return {"XDG_CONFIG_HOME": str(h)}


def _cache(renode: str) -> Path:
    return home(renode) / "renode" / "compiler-cache"


def _manifest(renode: str) -> dict:
    try:
        return json.loads((home(renode) / MANIFEST).read_text())
    except (OSError, ValueError):
        return {}


def built(renode: str, sha: str | None = None) -> bool:
    return (_cache(renode) / (sha or digest())).is_file()


def _commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@contextlib.contextmanager
def _lock(renode: str):
    h = home(renode)
    h.mkdir(parents=True, exist_ok=True)
    with open(h / ".vhil.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def build(renode: str) -> str:
    """Compile every model into Renode's cache for `renode`, as the vHIL's
    starts include them, unless it is there. Returns the hash. Renode itself
    compiles and stores it (the code path every start then takes), and the
    file it stored must be the hash computed here, or a start would miss it
    and compile again unseen."""
    from vhil import renode as rn
    paths = sources()
    sha = digest(paths)
    with _lock(renode):
        if built(renode, sha):
            return sha
        cache = _cache(renode)
        before = set(os.listdir(cache)) if cache.is_dir() else set()
        cmd = [renode, "--disable-gui", "--plain", "-P", "-1", "-e",
               f"{include_command(paths)}; quit"]
        proc = subprocess.run(cmd, env={**rn.RENODE_ENV, **os.environ, **env(renode)},
                              capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
        after = set(os.listdir(cache)) if cache.is_dir() else set()
        new = sorted(after - before - {".lock"})
        if not built(renode, sha):
            tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-30:])
            raise ModelsError(f"Renode did not store the models' assembly {sha} "
                              f"(stored: {new or 'nothing'}):\n{tail}")
        manifest = _manifest(renode)
        manifest[sha] = {"sources": [p.name for p in paths], "commit": _commit(),
                         "renode": rn.build_id(renode),
                         "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        tmp = home(renode) / f".{MANIFEST}.{os.getpid()}"
        tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        os.replace(tmp, home(renode) / MANIFEST)
    return sha


def ensure(renode: str) -> str:
    """Before a start: the models' assembly for the current sources is in the
    cache. `auto` compiles it when it is not; `prebuilt` fails, loudly."""
    sha = digest()
    if built(renode, sha):
        return sha
    if mode() == "prebuilt":
        have = _manifest(renode)
        listed = "; ".join(f"{s[:12]} (commit {m.get('commit', '?')[:12]}, {m.get('built', '?')})"
                           for s, m in sorted(have.items(), key=lambda kv: kv[1].get("built", ""))) \
            or "none"
        raise ModelsError(
            f"VHIL_MODELS=prebuilt: no models assembly for these sources (hash {sha[:12]}, "
            f"{MODELS_DIR}, commit {_commit()[:12]}) in {home(renode)}; built there: {listed}. "
            f"Run `python -m vhil.models build` from this checkout first.")
    return build(renode)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vhil.models", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("build", "check", "info"))
    ap.add_argument("--renode", default=os.environ.get("RENODE", "renode"))
    args = ap.parse_args(argv)
    sha = digest()
    if args.command == "build":
        build(args.renode)
        print(f"vhil.models: {sha} built for {args.renode} in {home(args.renode)}")
        return 0
    if args.command == "check":
        ok = built(args.renode, sha)
        print(f"vhil.models: {sha} {'built' if ok else 'NOT built'} ({home(args.renode)})")
        return 0 if ok else 1
    print(json.dumps({"hash": sha, "home": str(home(args.renode)), "mode": mode(),
                      "built": built(args.renode, sha), "manifest": _manifest(args.renode)},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
