"""A run's CAN contract for the browser's frame decoder (M5.3, #115).

    GET /api/runs/{id}/contract -> {buses: {bus: {id: message}}, boards, conflicts,
                                    state, labels}

The contract is the firmware's own: the .def files of the source each board's
image was built from (vhil/candef.py), found next to the ELF the worker ran
(the run summary's `firmware`, or, before the run has finished, the path the
worker's FirmwareResolver gives the run's refs). A .def does not say which
bus a frame rides; the catalogue firmware does (`can.contract`: the board
connectors the contract rides, System.contract_buses), so a board's
messages apply on those buses only: the ECU's on its ACU bus, not on the
dash bus, whose own 0x510/0x511 would otherwise decode as the uDV's. A
firmware that names none applies on every bus the board sits on. Where two
boards on one bus declare the same id, the
sender's declaration wins: each repo carries the frames it consumes "so the
generated DBC documents the whole contract", but the sender owns the layout
(IFS08-CE-ECU all_messages.inc). A board's sender name is the one most of its
own messages carry (ECU "VCU", AMS "AMS"). Declarations that disagree are
listed under `conflicts`: that is drift between two firmwares' contracts.

Parsing is cached per firmware source (candef.load), so the browser can fetch
this for every replayed run.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from vhil import candef, stateview
from vhil.server.runs import RunStore
from vhil.system import System, SystemError


def board_elfs(run: dict, system: System, fw_dir: Path) -> dict[str, Path]:
    """Board -> the application ELF the run used (or will use)."""
    ran = (run.get("summary") or {}).get("firmware") or {}
    out = {k: Path(v) for k, v in ran.items() if "." not in k and k in system.boards}
    missing = [b for b in system.boards if b not in out]
    if missing:
        from vhil.worker import FirmwareResolver
        expected = FirmwareResolver(fw_dir, build=False).expected(system, run.get("firmware") or {})
        out.update({b: expected[b][1] for b in missing if b in expected})
    return out


def _sender(messages: dict[int, candef.Message]) -> str:
    counts = Counter(m.sender for m in messages.values())
    return counts.most_common(1)[0][0] if counts else ""


def run_contract(run: dict, workspace: Path, fw_dir: Path) -> dict:
    path = Path(workspace) / "systems" / f"{run['system']}.yaml"
    try:
        system = System(path)
    except (OSError, SystemError) as e:
        return {"buses": {}, "boards": {}, "conflicts": [], "state": {}, "labels": {},
                "inputs": {}, "error": f"system '{run['system']}': {e}"}
    return system_contract(system, board_elfs(run, system, fw_dir))


def system_contract(system: System, elfs: dict[str, Path]) -> dict:
    """{buses: {bus: {id: message}}, boards, conflicts}: the contract each
    board's application ELF (`elfs`, board -> ELF) speaks, merged per bus as
    the module doc says. A board whose ELF's source has no .def files (not
    built here) is listed with its error and decodes nothing."""
    out: dict = {"buses": {}, "boards": {}, "conflicts": []}
    contracts: dict[str, dict[int, candef.Message]] = {}
    for board, elf in elfs.items():
        try:
            contracts[board] = candef.load(elf.parent)
        except (OSError, ValueError) as e:
            out["boards"][board] = {"elf": str(elf), "error": str(e)}
            continue
        out["boards"][board] = {"elf": str(elf), "source": str(candef.messages_dir(elf.parent)),
                                "sender": _sender(contracts[board]),
                                "messages": len(contracts[board]),
                                "buses": system.contract_buses(board)}
    def owns(entry: tuple[str, candef.Message]) -> bool:
        board, msg = entry
        return msg.sender == out["boards"][board]["sender"]

    for bus in system.buses:
        merged: dict[int, tuple[str, candef.Message]] = {}
        for board in system.boards:
            if board not in contracts or bus not in out["boards"][board]["buses"]:
                continue
            for can_id, msg in contracts[board].items():
                win, lose = merged.get(can_id), (board, msg)
                if win is None:
                    merged[can_id] = lose
                    continue
                if not owns(win) and owns(lose):
                    win, lose = lose, win
                merged[can_id] = win
                if (win[1].dlc, win[1].fields) != (lose[1].dlc, lose[1].fields):
                    out["conflicts"].append({"bus": bus, "id": can_id, "name": win[1].name,
                                             "kept": win[0], "dropped": lose[0]})
        out["buses"][bus] = {str(i): {**m.to_json(), "board": b}
                             for i, (b, m) in sorted(merged.items())}
    # Each board's state view (vhil/stateview.py) and the value labels of the
    # system's symbols and view frames: what the state panel shows, and the
    # labels expects and the scenario editor accept.
    out["state"] = stateview.view(system, elfs, out)
    out["labels"] = stateview.labels(system, elfs, out)
    # What a live session's pin switches and analog inputs drive.
    out["inputs"] = stateview.inputs(system)
    # Where a stimulus takes effect: the next sync point (docs/scenarios.md,
    # "Times"); the scenario editor shows a time between two moved there.
    out["sync_quantum_us"] = system.sync_quantum_us()
    return out


def router(settings, workspace, fw_dir: Optional[Path] = None) -> APIRouter:
    from vhil.worker import DEFAULT_FW_DIR

    store = RunStore(settings.db)
    fw = Path(fw_dir or DEFAULT_FW_DIR)
    r = APIRouter(prefix="/api/runs", tags=["runs"])

    @r.get("/{run_id}/contract")
    def contract(run_id: int):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        return run_contract(run, workspace.root, fw)

    return r
