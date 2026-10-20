"""A store of built firmware images, keyed by everything a build depends on.

`vhil.system build` (System.build_firmware) clones a firmware source and
builds it with its recipe. With VHIL_FW_CACHE set (CI only; locally and on
the web app's workers it is unset, and every build runs as before), the
build's ELF and its flat .bin are kept in that directory under a key of
what decides their bytes, and a later build with the same key copies them
from there instead of running the recipe. The source is still cloned, at
the same path: what reads the checkout (vhil/candef.py's .def files, a
debugger's sources) sees what a build would have left, and the commit the
key names is the one the checkout is at (`git rev-parse HEAD`), so a ref
that moved since is a different key.

The key: the firmware's id, repo and exact commit; its recipe (IFS_HIL's,
invariant 1: system.recipe_id, which covers the configure and build commands,
the declared toolchain, the ELF's path and the submodules; and the recipe's
source); where the source was built (the absolute directory: debug info and
__FILE__ carry it); the toolchain that would build it (arm-none-eabi-gcc,
cmake and make, as each reports its version); and the build code itself
(the SHA-256 of System.build_firmware's source, and VERSION here). So a cached image is the one a fresh
build here would give.

Each entry records the SHA-256 of its files. A hit verifies them before and
after the copy and fails, naming both, on a mismatch: a damaged or tampered
store is never used silently.

    <VHIL_FW_CACHE>/<id>+<commit[:12]>.<key[:16]>/{entry.json, image.elf, image.bin}
    <VHIL_FW_CACHE>/.used    the entries this process tree used or added, one a line
    <VHIL_FW_CACHE>/.added   the entries it added (CI saves the store when non-empty)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ENV = "VHIL_FW_CACHE"
# Bump when the way an image is built or stored changes (System.build_firmware,
# this module): every key changes with it.
VERSION = 1
ENTRY = "entry.json"
TOOLS = (("arm-none-eabi-gcc", "--version"), ("cmake", "--version"), ("make", "--version"))


class FirmwareCacheError(RuntimeError):
    pass


def root() -> Path | None:
    value = os.environ.get(ENV, "").strip()
    return Path(value) if value else None


def _first_line(cmd: tuple[str, ...]) -> str:
    try:
        out = subprocess.run(list(cmd), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "absent"
    return (out.stdout.strip().splitlines() or ["absent"])[0]


_toolchain: dict | None = None


def toolchain() -> dict:
    global _toolchain
    if _toolchain is None:
        _toolchain = {cmd[0]: _first_line(cmd) for cmd in TOOLS}
    return _toolchain


def head(src: Path) -> str:
    out = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True)
    return out.stdout.strip()


def key(fw: dict, commit: str, src: Path, recipe: str, build_code: str) -> dict:
    """What the image's bytes depend on, as a dict (hashed for the entry's name)."""
    return {"version": VERSION, "firmware": fw["id"], "repo": fw.get("repo"), "commit": commit,
            "recipe": recipe, "recipe_source": fw["build"].get("recipe_source"),
            "source_dir": str(Path(src).resolve()), "toolchain": toolchain(),
            "build_code": build_code}


def _digest(k: dict) -> str:
    return hashlib.sha256(json.dumps(k, sort_keys=True).encode()).hexdigest()


def entry_dir(store: Path, k: dict) -> Path:
    return store / f"{k['firmware']}+{k['commit'][:12]}.{_digest(k)[:16]}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _note(store: Path, name: str, entry: Path) -> None:
    with open(store / name, "a") as f:
        f.write(entry.name + "\n")


def restore(store: Path, k: dict, elf: Path) -> bool:
    """Copy the cached image for key `k` to `elf` (and its .bin beside it).
    False when there is none; FirmwareCacheError when there is one whose files
    are not what it recorded."""
    entry = entry_dir(store, k)
    try:
        meta = json.loads((entry / ENTRY).read_text())
    except (OSError, ValueError):
        return False
    if meta.get("key") != k:
        raise FirmwareCacheError(f"{entry}: its key is not the one it is filed under "
                                 f"(recorded {meta.get('key')}, wanted {k})")
    pairs = ((entry / "image.elf", elf, meta.get("elf_sha256")),
             (entry / "image.bin", elf.with_suffix(".bin"), meta.get("bin_sha256")))
    for cached, _, want in pairs:
        got = sha256(cached) if cached.is_file() else "missing"
        if got != want:
            raise FirmwareCacheError(f"{cached}: SHA-256 {got}, its entry recorded {want}")
    elf.parent.mkdir(parents=True, exist_ok=True)
    for cached, dest, want in pairs:
        shutil.copyfile(cached, dest)
        got = sha256(dest)
        if got != want:
            raise FirmwareCacheError(f"{dest}: SHA-256 {got} after the copy from {cached}, "
                                     f"its entry recorded {want}")
    _note(store, ".used", entry)
    return True


def store(store_dir: Path, k: dict, elf: Path) -> Path:
    """File a fresh build's ELF and .bin under key `k`."""
    entry = entry_dir(store_dir, k)
    store_dir.mkdir(parents=True, exist_ok=True)
    tmp = store_dir / f".{entry.name}.{os.getpid()}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    shutil.copyfile(elf, tmp / "image.elf")
    shutil.copyfile(elf.with_suffix(".bin"), tmp / "image.bin")
    meta = {"key": k, "elf": elf.name, "elf_sha256": sha256(tmp / "image.elf"),
            "bin_sha256": sha256(tmp / "image.bin")}
    if meta["elf_sha256"] != sha256(elf):
        raise FirmwareCacheError(f"{elf}: changed while it was copied into {entry}")
    (tmp / ENTRY).write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    shutil.rmtree(entry, ignore_errors=True)
    os.replace(tmp, entry)
    _note(store_dir, ".used", entry)
    _note(store_dir, ".added", entry)
    return entry


def prune(store_dir: Path) -> list[str]:
    """Drop every entry not listed in .used (what this job used or added), so
    a saved store holds what a run needs and does not grow without bound.
    Returns the entries kept, sorted."""
    used = set()
    if (store_dir / ".used").is_file():
        used = {line.strip() for line in (store_dir / ".used").read_text().splitlines() if line.strip()}
    kept = []
    for entry in store_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in used:
            kept.append(entry.name)
        else:
            shutil.rmtree(entry)
    return sorted(kept)


def main(argv: list[str] | None = None) -> int:
    """python -m vhil.fwcache prune <dir>: prune it and print the cache key of
    what is left (CI's save step)."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m vhil.fwcache")
    ap.add_argument("command", choices=("prune",))
    ap.add_argument("dir", type=Path)
    args = ap.parse_args(argv)
    kept = prune(args.dir)
    print(hashlib.sha256("\n".join(kept).encode()).hexdigest()[:20])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
