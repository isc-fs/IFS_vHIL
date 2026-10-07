"""A firmware's state view: what the state panel shows of a board running
it (docs/state-view.md; feature 2 of docs/architecture/editor-workspace.md).

The catalogue firmware lists it, next to its CAN contract, as an ordered
`state_view` of items {label, source, kind, unit, enum, values, period_ms}.
A source is a signal in the scenario expects' grammar (vhil/expect.py)
without the system's names, so one catalogue entry serves every system:

    frame:<connector>.<message>.<field>   a field of the .def contract, on the
                                          bus the board's connector is on
    symbol:<name>                         a firmware global, sampled
    pin:<pin>                             a board GPIO, its edges watched

Placed on a board of a system, each becomes that system's signal
(`frame:can_acu.AMS_status.min_cell_mV`, `symbol:ams.g_state_telemetry`,
`pin:ams.PB5`), which is what the trace records and an expect reads.

Labels: a symbol's values are labelled by the DWARF enumeration the item
names (`enum`, read from the board's ELF by vhil/elf.py), or the one its own
type is; a frame field's by its .def value table (CAN_VAL); an item's
`values` table, cited in the firmware, goes over either. `labels()` gives
every labelled signal of a system, which the contract carries
(vhil/server/decode.py) so expects and the scenario editor accept a label
(`== Precharge`) where a number was needed.

The worker samples every view symbol and watches every view pin in each run
(`watches()`), so a run's trace holds what its state panel shows.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple, Optional

from vhil import elf as velf
from vhil import expect as vexpect

KINDS = ("state", "relay", "fault", "value")
# How often a view symbol is sampled unless its item says: the FSM state and
# faults at the firmware's own 10 ms control period, values at 50 ms.
DEFAULT_PERIOD_MS = {"state": 10, "relay": 10, "fault": 10, "value": 50}

_ID = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
SOURCE = re.compile(
    rf"^(?:frame:(?P<conn>{_ID})\.(?P<msg>0x[0-9A-Fa-f]{{1,8}}|{_ID})\.(?P<field>{_ID})"
    rf"|symbol:(?P<symbol>[A-Za-z_][A-Za-z0-9_]{{0,127}})"
    rf"|pin:(?P<pin>[A-Za-z0-9_]{{1,64}}))\Z", re.ASCII)


class Item(NamedTuple):
    label: str
    kind: str
    source: str             # as the catalogue gives it
    signal: str             # in the system: what the trace records
    unit: str
    enum: str
    values: dict[int, str]  # the catalogue's own labels
    period_ms: float


def catalogue_items(firmware: dict) -> list[dict]:
    """The firmware's state view as the catalogue gives it (schema-checked
    when the catalogue loads)."""
    return list(firmware.get("state_view") or [])


def _connectors(system, board: str) -> dict[str, str]:
    """The board's CAN connectors -> the bus each is on."""
    out = {}
    for bus, spec in system.buses.items():
        if spec.get("kind") != "can":
            continue
        for node in spec["nodes"]:
            name, connector = node.split(".", 1)
            if name == board:
                out[connector] = bus
    return out


def resolve(system, board: str) -> tuple[list[Item], list[str]]:
    """(the board's view in the system's signals, errors): an item whose
    connector is on no bus or whose pin is not a GPIO of the board is an
    error and left out."""
    b = system.boards[board]
    conns = _connectors(system, board)
    items, errors = [], []
    for i, raw in enumerate(catalogue_items(b.firmware)):
        where = f"{b.firmware['id']} state_view[{i}] ({raw.get('label', '?')})"
        m = SOURCE.match(raw.get("source") or "")
        if not m:
            errors.append(f"{where}: {raw.get('source')!r} is not a source: "
                          "frame:<connector>.<message>.<field>, symbol:<name> or pin:<pin>")
            continue
        if m["conn"]:
            bus = conns.get(m["conn"])
            if bus is None:
                errors.append(f"{where}: {board}.{m['conn']} is on no CAN bus of {system.id}")
                continue
            signal = f"frame:{bus}.{m['msg']}.{m['field']}"
        elif m["symbol"]:
            signal = f"symbol:{board}.{m['symbol']}"
        else:
            try:
                _, kind, _ = system.resolve(f"{board}.{m['pin']}")
            except Exception as e:  # noqa: BLE001 - SystemError: no such pin
                errors.append(f"{where}: {e}")
                continue
            if kind != "gpio":
                errors.append(f"{where}: {board}.{m['pin']} is {kind}, not gpio")
                continue
            signal = f"pin:{board}.{m['pin']}"
        kind = raw.get("kind") or "value"
        items.append(Item(raw["label"], kind, raw["source"], signal, raw.get("unit") or "",
                          raw.get("enum") or "",
                          {int(k): str(v) for k, v in (raw.get("values") or {}).items()},
                          float(raw.get("period_ms") or DEFAULT_PERIOD_MS[kind])))
    return items, errors


def watches(system) -> tuple[list[tuple[str, str, float]], list[tuple[str, str]]]:
    """([(board, symbol, period_ms)], [(board, pin)]) the system's state views
    read: the worker samples and watches them in every run."""
    symbols, pins = [], []
    for board in system.boards:
        items, _ = resolve(system, board)
        for it in items:
            sig = vexpect.parse_signal(it.signal)
            if sig.kind == "symbol":
                known = next((s for s in symbols if s[:2] == (board, sig.item)), None)
                if known is None:
                    symbols.append((board, sig.item, it.period_ms))
                elif it.period_ms < known[2]:
                    symbols[symbols.index(known)] = (board, sig.item, it.period_ms)
            elif sig.kind == "pin" and (board, sig.item) not in pins:
                pins.append((board, sig.item))
    return symbols, pins


def inputs(system) -> dict[str, list[dict]]:
    """{board: [{pin, kind: gpio | analog, label}]}: what a live session
    drives on each board (docs/live-session.md), in the role's order. The
    pins its role's backplane routes with a car signal (the role's `pins`)
    that are GPIOs or analog inputs, but for the state view's own pins (the
    relays and lines the firmware drives, which the card shows), the spares,
    and a device's chip select."""
    selects = {dev["cs"] for dev in system.devices.values() if dev.get("cs")}
    out = {}
    for name, b in system.boards.items():
        items, _ = resolve(system, name)
        shown = {vexpect.parse_signal(it.signal).item for it in items
                 if it.signal.startswith("pin:")}
        rows = []
        for pin, label in ((b.role_spec or {}).get("pins") or {}).items():
            label = str(label)
            if label.startswith("SPARE") or pin in shown or f"{name}.{pin}" in selects:
                continue
            try:
                kind, _ = b.endpoint(pin)
            except Exception:  # noqa: BLE001 - SystemError: not emulated
                continue
            if kind in ("gpio", "analog"):
                rows.append({"pin": pin, "kind": kind, "label": label})
        out[name] = rows
    return out


# -- labels ----------------------------------------------------------------------------

class Labels(NamedTuple):
    values: dict[int, str]
    source: str             # dwarf | contract | table | none
    note: str = ""


def _dwarf(path: Optional[Path]) -> tuple[Optional[velf.Enums], str]:
    if path is None or not Path(path).is_file():
        return None, "firmware not built here"
    try:
        found = velf.enums(path)
    except (OSError, ValueError) as e:
        return None, str(e)
    if not found.types:
        return None, f"{Path(path).name} has no DWARF debug info (built without -g)"
    return found, ""


def item_labels(item: Item, elf: Optional[Path], contract: Optional[dict]) -> Labels:
    """An item's value labels, and where they came from (module doc)."""
    sig = vexpect.parse_signal(item.signal)
    base: dict[int, str] = {}
    source, note = "none", ""
    if sig.kind == "frame":
        f = vexpect.find_field(vexpect.find_message(contract, sig.owner, sig.item), sig.field)
        if f and f.get("values"):
            base, source = {int(k): v for k, v in f["values"].items()}, "contract"
    elif sig.kind == "symbol":
        found, why = _dwarf(elf)
        name = item.enum or (found.variables.get(sig.item) if found else None)
        if found is not None and name:
            qual = velf.find_enum(found, name)
            if qual:
                base, source = dict(found.types[qual]), "dwarf"
            else:
                note = f"no enum {name} in the image's DWARF"
        elif item.enum:
            note = f"enum {item.enum}: {why}"
    if item.values:
        base = {**base, **item.values}
        if source == "none":
            source = "table"
    return Labels(dict(sorted(base.items())), source, note)


def view(system, elfs: dict[str, Path], contract: Optional[dict]) -> dict:
    """{board: {firmware, items, errors}}: each board's resolved view with its
    labels, as the state panel takes it (the contract's `state`)."""
    out = {}
    for board, b in system.boards.items():
        items, errors = resolve(system, board)
        rows = []
        for it in items:
            lab = item_labels(it, elfs.get(board), contract)
            row = {"label": it.label, "kind": it.kind, "source": it.source, "signal": it.signal,
                   "unit": it.unit, "period_ms": it.period_ms, "labels_from": lab.source}
            if it.enum:
                row["enum"] = it.enum
            if lab.values:
                row["values"] = {str(k): v for k, v in lab.values.items()}
            if lab.note:
                row["note"] = lab.note
            rows.append(row)
        out[board] = {"firmware": b.firmware["id"], "items": rows, "errors": errors}
    return out


def labels(system, elfs: dict[str, Path], contract: Optional[dict]) -> dict[str, dict[str, str]]:
    """{signal: {raw: label}} of every labelled symbol and view frame of the
    system: the DWARF enums its globals are typed with, then each state
    view's labels over them."""
    out: dict[str, dict[str, str]] = {}
    for board in system.boards:
        found, _ = _dwarf(elfs.get(board))
        if found is not None:
            for var, qual in found.variables.items():
                if vexpect.SIGNAL.match(f"symbol:{board}.{var}"):
                    out[f"symbol:{board}.{var}"] = {str(k): v for k, v in found.types[qual].items()}
        items, _ = resolve(system, board)
        for it in items:
            lab = item_labels(it, elfs.get(board), contract)
            if lab.values and (lab.source != "contract" or it.values):
                out[it.signal] = {str(k): v for k, v in lab.values.items()}
    return out


def enums_of(path: Path) -> dict:
    """GET /api/firmware/<id>/enums for one image: its DWARF enums, or why
    there are none."""
    found, why = _dwarf(path)
    if found is None:
        return {"source": "none", "note": why, "enums": {}, "variables": {}}
    return {"source": "dwarf", "enums": {q: {str(k): v for k, v in vals.items()}
                                         for q, vals in sorted(found.types.items())},
            "variables": dict(sorted(found.variables.items()))}
