"""A board's firmware sources for the Debug tab (step 17 of
docs/architecture/editor-workspace.md; docs/debugger.md).

    GET /api/runs/{id}/debug/source?board=ecu&path=<file> -> {path, root, lines}

`path` is what GDB names a stop's file (an absolute path in the image's
firmware checkout, from its DWARF) or a path relative to that checkout. The
file is read from the checkout the board's image was built in, in the fw
volume (`<fw>/<firmware>@<ref>/`, vhil.system source_dir; read-only in the
API): the run's image as decode.board_elfs finds it, never a path the client
or the trace names.

The fw volume is written by the workers, which build untrusted firmware, so
a source file may be a symlink anywhere (the API's secrets): only a regular
file is read, opened without following a final symlink, and only when the
path the open file really has lies inside that checkout; and only source
files (SUFFIXES), up to MAX_BYTES.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from vhil.server.decode import board_elfs
from vhil.server.runs import RunStore
from vhil.system import System, SystemError

SUFFIXES = frozenset({".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".s", ".S",
                      ".inc", ".def"})
MAX_BYTES = 2 << 20


class SourceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def checkout_of(elf: Path, fw_dir: Path) -> Path:
    """The firmware checkout an image was built in: the fw directory's child
    the ELF lies under."""
    fw = fw_dir.resolve()
    try:
        rel = Path(os.path.realpath(elf)).relative_to(fw)
    except ValueError:
        raise SourceError(404, "the board's image is not in the firmware directory") from None
    if len(rel.parts) < 2:
        raise SourceError(404, "the board's image is not in a firmware checkout")
    return fw / rel.parts[0]


def _real(fd: int, fallback: str) -> str:
    """The path an open file really has (Linux: /proc/self/fd)."""
    proc = f"/proc/self/fd/{fd}"
    return os.readlink(proc) if os.path.islink(proc) else os.path.realpath(fallback)


def read_source(root: Path, path: str) -> tuple[str, list[str]]:
    """(its path relative to the checkout, its lines) of a source file in
    `root` (module doc)."""
    if not isinstance(path, str) or not path or len(path) > 1024 or "\x00" in path:
        raise SourceError(422, "not a source path")
    root = Path(os.path.realpath(root))
    p = Path(path)
    if p.is_absolute():
        try:
            rel = p.relative_to(root)
        except ValueError:
            raise SourceError(404, "not a file of this board's firmware checkout") from None
    else:
        rel = p
    if ".." in rel.parts or not rel.parts:
        raise SourceError(422, "not a source path")
    if rel.suffix not in SUFFIXES:
        raise SourceError(422, f"not a source file ({', '.join(sorted(SUFFIXES))})")
    full = root / rel
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise SourceError(404, "no such source file in the board's firmware checkout") from None
    try:
        st = os.fstat(fd)
        real = _real(fd, str(full))
        if not stat.S_ISREG(st.st_mode) or os.path.commonpath([real, str(root)]) != str(root):
            raise SourceError(404, "no such source file in the board's firmware checkout")
        if st.st_size > MAX_BYTES:
            raise SourceError(413, f"the file is over {MAX_BYTES >> 20} MiB")
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    return str(Path(real).relative_to(root)), data.decode("utf-8", errors="replace").splitlines()


def router(settings, workspace, fw_dir: Optional[Path] = None) -> APIRouter:
    from vhil.worker import DEFAULT_FW_DIR

    store = RunStore(settings.db)
    fw = Path(fw_dir or DEFAULT_FW_DIR)
    r = APIRouter(prefix="/api/runs", tags=["debug"])

    @r.get("/{run_id}/debug/source")
    def source(run_id: int, request: Request, board: str = Query(..., max_length=64),
               path: str = Query(..., max_length=1024)):
        here = Path(getattr(request.app.state, "fw_dir", None) or fw)
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        try:
            system = System(Path(workspace.root) / "systems" / f"{run['system']}.yaml")
        except (OSError, SystemError) as e:
            raise HTTPException(404, f"system '{run['system']}': {e}")
        if board not in system.boards:
            raise HTTPException(404, f"no board '{board}' in {run['system']}")
        elf = board_elfs(run, system, here).get(board)
        if elf is None:
            raise HTTPException(404, f"no image for {board}")
        try:
            root = checkout_of(elf, here)
            rel, lines = read_source(root, path)
        except SourceError as e:
            raise HTTPException(e.status, e.detail)
        return {"board": board, "root": root.name, "path": rel, "lines": lines}

    return r
