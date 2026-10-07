"""Scenario expects: what a run must show, checked against its trace in
virtual time (docs/scenarios.md).

An expect names a signal and a window of virtual time, [at_ms, until_ms]
(until_ms defaults to the end of the run), and one check:

    eventually  the signal's value meets `op value` at some time in the window
    always      it meets it at every time in the window
    never       it meets it at no time in the window
    period      the gaps between a frame's arrivals stay within
                [min_ms, max_ms]; with max_ms, so do the gaps from the
                window's start to the first and from the last to its end
    count       the frame arrives between min and max times (max 0: never)

Signals, as text (the grammar a firmware's state view uses too, vhil/stateview.py):

    frame:<bus>.<message>.<field>   a decoded field of the firmware's .def
                                    contract; <message> by name or 0x id
    frame:<bus>.<message>           the frame itself (period, count)
    symbol:<board>.<name>           a firmware global, sampled (vhil/worker.py)
    pin:<board>.<pin>               a GPIO, its edges watched from power-on

A signal's value at a time is its last observation at or before it: a
frame's field from the last such frame, a symbol's last sample, a pin's
level after its last edge. The value carried into the window counts as
observed at the window's start. Frames the scenario sent itself (`src:
stimulus`) are not observations: an expect checks what the boards did.

Values: a number (a field's physical value, a symbol's raw value, a pin's
0/1), a boolean, "high"/"low" for a pin, or a label, compared with its raw
value (== and != only): of the field's value table (CAN_VAL), or, for a
symbol, of the enum the contract's `labels` give it (its DWARF enum or a
state view's, vhil/stateview.py): `== Precharge`.

Pure: no Renode, no server. The worker (vhil/worker.py) and the scenario
suite (tests/scenarios/) evaluate a finished trace with evaluate_trace().
"""
from __future__ import annotations

import json
import operator
import re
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

from vhil import candef

CHECKS = ("eventually", "always", "never", "period", "count")
VALUE_CHECKS = frozenset({"eventually", "always", "never"})
FRAME_CHECKS = frozenset({"period", "count"})
OPS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
       ">": operator.gt, ">=": operator.ge}

_ID = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
SIGNAL = re.compile(
    rf"^(?:frame:(?P<bus>{_ID})\.(?P<msg>0x[0-9A-Fa-f]{{1,8}}|{_ID})(?:\.(?P<field>{_ID}))?"
    rf"|symbol:(?P<sboard>{_ID})\.(?P<symbol>[A-Za-z_]\w{{0,127}})"
    rf"|pin:(?P<pboard>{_ID})\.(?P<pin>[A-Za-z0-9_]{{1,64}}))\Z", re.ASCII)
# A value given as text: a value-table label, or high/low (fullmatch).
LABEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_ .+:/-]{0,63}")


class Signal(NamedTuple):
    kind: str           # frame | symbol | pin
    owner: str          # the bus (frame) or board (symbol, pin)
    item: str           # the message (name or 0x id), symbol or pin
    field: str = ""     # a frame's field ("" = the frame itself)

    def __str__(self) -> str:
        return f"{self.kind}:{self.owner}.{self.item}" + (f".{self.field}" if self.field else "")


def parse_signal(text: str) -> Signal:
    m = SIGNAL.match(text or "")
    if not m:
        raise ValueError(f"{text!r} is not a signal: frame:<bus>.<message>[.<field>], "
                         "symbol:<board>.<name> or pin:<board>.<pin>")
    if m["bus"]:
        return Signal("frame", m["bus"], m["msg"], m["field"] or "")
    if m["sboard"]:
        return Signal("symbol", m["sboard"], m["symbol"])
    return Signal("pin", m["pboard"], m["pin"])


# -- the contract (vhil/server/decode.py system_contract's JSON) -----------------------

def find_message(contract: Optional[dict], bus: str, item: str) -> Optional[dict]:
    """The contract's message on `bus` named `item` (a name or a 0x id)."""
    msgs = ((contract or {}).get("buses") or {}).get(bus) or {}
    if item.lower().startswith("0x"):
        return msgs.get(str(int(item, 16)))
    return next((m for m in msgs.values() if m.get("name") == item), None)


def message_id(contract: Optional[dict], bus: str, item: str) -> Optional[int]:
    if item.lower().startswith("0x"):
        return int(item, 16)
    msg = find_message(contract, bus, item)
    return msg["id"] if msg else None


def find_field(msg: Optional[dict], name: str) -> Optional[dict]:
    return next((f for f in (msg or {}).get("fields", []) if f["name"] == name), None)


def field_raw(data: bytes, f: dict) -> Optional[int]:
    """A field's raw value in `data` (signed if the field is), or None when
    the frame is too short for it (candef bit numbering)."""
    bits = list((candef.be_bits if f["be"] else candef.le_bits)(f["start"], f["length"]))
    if max(bits) >= 8 * len(data):
        return None
    v = (candef.get_be if f["be"] else candef.get_le)(data, f["start"], f["length"])
    n = f["length"]
    return v - (1 << n) if f.get("signed") and v >> (n - 1) else v


def signal_labels(contract: Optional[dict], sig: Signal) -> dict:
    """{raw (as text): label} the contract gives a signal beyond its field's
    value table: a symbol's DWARF enum, a state view's table
    (vhil/stateview.py labels, the contract's `labels`)."""
    return dict(((contract or {}).get("labels") or {}).get(str(sig)) or {})


def label_raw(f: Optional[dict], label: str) -> Optional[int]:
    for raw, text in ((f or {}).get("values") or {}).items():
        if text == label:
            return int(raw)
    return None


def field_range(f: dict) -> tuple[float, float]:
    """The physical values a field can carry: (min, max)."""
    n = f["length"]
    lo, hi = (-(1 << (n - 1)), (1 << (n - 1)) - 1) if f.get("signed") else (0, (1 << n) - 1)
    a, b = lo * f["factor"] + f["offset"], hi * f["factor"] + f["offset"]
    return (min(a, b), max(a, b))


# -- evaluation ------------------------------------------------------------------------

class _Check:
    """One expect over the records of a trace, in time order."""

    def __init__(self, index: int, spec: dict, contract: Optional[dict], end_us: int):
        self.index, self.spec = index, spec
        self.check = spec["check"]
        self.a = int(round(float(spec.get("at_ms") or 0) * 1000))
        until = spec.get("until_ms")
        self.b = end_us if until is None else int(round(float(until) * 1000))
        self.error = ""
        self.signal: Optional[Signal] = None
        self.can_id: Optional[int] = None
        self.field: Optional[dict] = None
        self.pred = None
        self.label = ""
        self.table: dict = {}          # raw (as text) -> label, for a label value
        try:
            self.signal = parse_signal(spec["signal"])
            self._compile(contract)
        except (ValueError, KeyError) as e:
            self.error = str(e)
        # value checks
        self.carried = None          # (t, value) last observed before the window
        self.entered = False
        self.seen = 0
        self.hit: Optional[tuple[int, object]] = None       # eventually: first match
        self.bad: Optional[tuple[int, object]] = None       # always / never: first violation
        self.last: Optional[tuple[int, object]] = None      # last value seen in the window
        # frame checks
        self.times: list[int] = []
        self.count = 0
        self.first: Optional[int] = None
        self.prev: Optional[int] = None
        self.worst: Optional[tuple[float, int]] = None      # (gap µs, at t µs)

    def _compile(self, contract: Optional[dict]) -> None:
        sig, spec = self.signal, self.spec
        if self.check in FRAME_CHECKS:
            if sig.kind != "frame" or sig.field:
                raise ValueError(f"{self.check} takes a frame: frame:<bus>.<message>, not {sig}")
            self.can_id = message_id(contract, sig.owner, sig.item)
            if self.can_id is None:
                raise ValueError(f"no message {sig.item} on {sig.owner} in the firmware's contract")
            return
        if sig.kind == "frame":
            if not sig.field:
                raise ValueError(f"{self.check} needs a field: frame:<bus>.<message>.<field>")
            msg = find_message(contract, sig.owner, sig.item)
            self.can_id = msg["id"] if msg else message_id(contract, sig.owner, sig.item)
            if msg is None:
                raise ValueError(f"no message {sig.item} on {sig.owner} in the firmware's contract")
            self.field = find_field(msg, sig.field)
            if self.field is None:
                raise ValueError(f"no field {sig.field} in {msg['name']}")
        op = OPS[spec.get("op") or "=="]
        value = spec.get("value")
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, str):
            if sig.kind == "pin" and value.lower() in ("high", "low"):
                value = int(value.lower() == "high")
            else:
                self.table = {**(signal_labels(contract, sig) if sig.kind != "pin" else {}),
                              **((self.field or {}).get("values") or {})}
                raw = label_raw({"values": self.table}, value)
                if raw is None:
                    raise ValueError(f"{value!r} is not a label of {sig}")
                if spec.get("op", "==") not in ("==", "!="):
                    raise ValueError(f"a label compares with == or != only, not {spec['op']}")
                self.label, value = spec["value"], raw
        if value is None:
            raise ValueError(f"{self.check} needs a value")
        target = value
        self.pred = lambda v: v is not None and op(v, target)

    # -- what a record says about this signal ----------------------------------------

    def observe(self, rec: dict):
        """(t_us, value) this record observes for the signal, or None."""
        sig, kind = self.signal, rec.get("kind")
        if sig.kind == "frame":
            if kind != "frame" or rec.get("src") or rec.get("bus") != sig.owner \
                    or rec.get("id") != self.can_id:
                return None
            if self.field is None:
                return rec["t_us"], None
            data = bytes.fromhex(rec.get("data") or "")
            raw = field_raw(data, self.field)
            if raw is None:
                return None
            if self.label:
                return rec["t_us"], raw
            return rec["t_us"], _num(raw * self.field["factor"] + self.field["offset"])
        if sig.kind == "symbol":
            if kind == "sample" and rec.get("board") == sig.owner and rec.get("name") == sig.item:
                return rec["t_us"], rec.get("value")
            return None
        if kind == "edge" and rec.get("board") == sig.owner and rec.get("pin") == sig.item:
            return rec["t_us"], int(rec.get("level", 0))
        return None

    def feed(self, rec: dict) -> None:
        if self.error:
            return
        got = self.observe(rec)
        if got is None:
            return
        t, v = got
        if self.check in FRAME_CHECKS:
            self._frame(t)
            return
        if t <= self.a:             # the value at the window's start: the last at or before it
            self.carried = (t, v)
            return
        if t > self.b:
            return
        self._enter()
        self._value(t, v)

    def _enter(self) -> None:
        if not self.entered:
            self.entered = True
            if self.carried is not None:
                self._value(self.a, self.carried[1])

    def _value(self, t: int, v) -> None:
        self.seen += 1
        self.last = (t, v)
        ok = self.pred(v)
        if self.check == "eventually" and ok and self.hit is None:
            self.hit = (t, v)
        elif self.check == "always" and not ok and self.bad is None:
            self.bad = (t, v)
        elif self.check == "never" and ok and self.bad is None:
            self.bad = (t, v)

    def _frame(self, t: int) -> None:
        if t < self.a or t > self.b:
            return
        self.count += 1
        if self.first is None:
            self.first = t
        if self.check == "period" and self.prev is not None:
            self._gap(t - self.prev, t)
        self.prev = t

    def _gap(self, gap: int, t: int, edge: bool = False) -> None:
        """A gap ending at t; at the window's `edge` (its start to the first
        frame, the last to its end) only a gap too long counts."""
        lo = None if edge else self.spec.get("min_ms")
        hi = self.spec.get("max_ms")
        over = (hi is not None and gap > hi * 1000) or (lo is not None and gap < lo * 1000)
        if over and (self.worst is None or abs(gap - _mid(lo, hi)) > abs(self.worst[0] - _mid(lo, hi))):
            self.worst = (gap, t)

    # -- the verdict ------------------------------------------------------------

    def result(self) -> dict:
        spec = self.spec
        out = {"index": self.index, "name": spec.get("name") or "", "check": self.check,
               "signal": spec.get("signal", ""), "at_us": self.a, "until_us": self.b,
               "passed": False, "t_us": None, "value": None, "detail": ""}
        if self.error:
            out["detail"] = self.error
            return out
        if self.check in VALUE_CHECKS:
            self._enter()
            want = f"{spec.get('op') or '=='} {spec.get('value')}"
            if self.check == "eventually":
                if self.hit:
                    out.update(passed=True, t_us=self.hit[0], value=self._shown(self.hit[1]),
                               detail=f"{want} at {_ms(self.hit[0])}")
                elif self.last:
                    out.update(t_us=self.last[0], value=self._shown(self.last[1]),
                               detail=f"never {want} in the window; last "
                                      f"{self._shown(self.last[1])} at {_ms(self.last[0])}")
                else:
                    out["detail"] = f"no value of {spec['signal']} in the window"
            elif self.bad:
                why = f"not {want}" if self.check == "always" else want
                out.update(t_us=self.bad[0], value=self._shown(self.bad[1]),
                           detail=f"{self._shown(self.bad[1])} at {_ms(self.bad[0])}: {why}")
            elif self.check == "always" and not self.seen:
                out["detail"] = f"no value of {spec['signal']} in the window"
            else:
                last = self.last
                out.update(passed=True, t_us=last[0] if last else None,
                           value=self._shown(last[1]) if last else None,
                           detail=f"{'always' if self.check == 'always' else 'never'} {want} "
                                  f"({self.seen} value{'s' if self.seen != 1 else ''})")
            return out
        if self.check == "count":
            lo, hi = spec.get("min"), spec.get("max")
            ok = (lo is None or self.count >= lo) and (hi is None or self.count <= hi)
            out.update(passed=ok, t_us=self.first, value=self.count,
                       detail=f"{self.count} frame{'s' if self.count != 1 else ''}"
                              + (f", the first at {_ms(self.first)}" if self.first is not None else ""))
            return out
        # period
        if self.count < 2:
            out.update(value=self.count, t_us=self.first,
                       detail=f"{self.count} frame{'s' if self.count != 1 else ''} in the window: "
                              "a period needs two")
            return out
        if spec.get("max_ms") is not None:
            self._gap(self.first - self.a, self.first, edge=True)
            self._gap(self.b - self.prev, self.b, edge=True)
        if self.worst:
            out.update(t_us=self.worst[1], value=_num(self.worst[0] / 1000),
                       detail=f"a gap of {self.worst[0] / 1000:g} ms ending at {_ms(self.worst[1])}")
        else:
            out.update(passed=True, value=self.count,
                       detail=f"{self.count} frames, every gap in range")
        return out

    def _shown(self, v):
        if self.label:
            return self.table.get(str(v)) or v
        return v


def _num(v: float):
    return int(v) if float(v).is_integer() else round(v, 9)


def _mid(lo, hi) -> float:
    if lo is None:
        return 0.0
    if hi is None:
        return float(lo) * 1000
    return (lo + hi) * 500.0


def _ms(t_us: Optional[int]) -> str:
    return "–" if t_us is None else f"{t_us / 1000:g} ms"


class Evaluation:
    """Every expect of a scenario over one trace: feed() the records in
    virtual-time order, then results()."""

    def __init__(self, expects: Iterable[dict], contract: Optional[dict], end_us: int):
        self.checks = [_Check(i, e, contract, end_us) for i, e in enumerate(expects)]

    def feed(self, rec: dict) -> None:
        for c in self.checks:
            c.feed(rec)

    def results(self) -> list[dict]:
        return [c.result() for c in self.checks]


def evaluate(expects: Iterable[dict], records: Iterable[dict], contract: Optional[dict],
             end_us: int) -> list[dict]:
    ev = Evaluation(expects, contract, end_us)
    for rec in records:
        ev.feed(rec)
    return ev.results()


_KINDS = (b'"frame"', b'"edge"', b'"sample"')


def evaluate_trace(path: Path, expects: list[dict], contract: Optional[dict],
                   end_us: int) -> list[dict]:
    """evaluate() over a trace file, read line by line (a long run's trace
    is hundreds of MiB): only frame, edge and sample records are parsed."""
    ev = Evaluation(expects, contract, end_us)
    if expects:
        with open(path, "rb") as f:
            for line in f:
                if any(k in line for k in _KINDS) and line.endswith(b"\n"):
                    ev.feed(json.loads(line))
    return ev.results()


def summary(results: list[dict]) -> dict:
    passed = sum(1 for r in results if r["passed"])
    return {"expects": results, "expects_passed": passed, "expects_failed": len(results) - passed}


def pins(expects: Iterable[dict]) -> list[tuple[str, str]]:
    """(board, pin) of every pin an expect reads: the worker watches them."""
    out = []
    for e in expects:
        try:
            s = parse_signal(e.get("signal", ""))
        except ValueError:
            continue
        if s.kind == "pin" and (s.owner, s.item) not in out:
            out.append((s.owner, s.item))
    return out


def symbols(expects: Iterable[dict]) -> list[tuple[str, str]]:
    """(board, name) of every symbol an expect reads: the worker samples them."""
    out = []
    for e in expects:
        try:
            s = parse_signal(e.get("signal", ""))
        except ValueError:
            continue
        if s.kind == "symbol" and (s.owner, s.item) not in out:
            out.append((s.owner, s.item))
    return out
