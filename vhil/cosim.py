"""The co-simulation port (M6, #14): plants exchange named signals with a
system at a fixed virtual-time step, in lock-step with the emulation.

A plant is something outside the electronics that the system senses and
drives: an inverter and its motor, a driver's pedals, later the vehicle
(MingoCIL). The system file declares the port:

    port:
      step_ms: 10
      signals:
        apps1:     {analog: ecu.PF8}               # plant drives a pin voltage
        start:     {gpio_in: ecu.PB5}              # plant drives a GPIO input
        rtds:      {gpio_out: ecu.PB4}             # plant reads a GPIO output
        inv_cmd:   {can_rx: {bus: can_inv, id: 0x360}}   # system -> plant
        inv_state: {can_tx: {bus: can_inv, id: 0x461}}   # plant -> system

Each step, in this order, deterministically:
  1. every plant's step(port, t_us) runs, in the order given, at virtual time
     t_us: it reads what the system did during the last step (frames
     received, output levels) and writes what it drives (voltages, input
     levels, frames to send);
  2. the system runs for step_ms of virtual time; frames a plant sent go out
     at the start of that step.
Nothing advances while a plant computes, so a plant may take as long as it
likes: virtual time stands still. The contract for an external engine
(MingoCIL) is the same exchange over a socket; docs/cosim.md.

An event-driven plant (`event_driven = True`, e.g. vhil/plant_ts.py's) is
stepped only when something can change for it, instead of every step: at the
run's start, when a gpio_out signal it names in `triggers` changes level
(the run returns at the next sync point, Sim.run_until_trigger), at its own
`next_us`, and at every lock-step plant's step. It reads its inputs' exact
times (`edges`) and hands its outputs' future values to the emulation in one
call (`schedule`), so how often it is stepped doesn't change what it
computes, and a run stops for it only a few times (docs/m8-plan.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from vhil.sim import Frame, Sim


class Plant(Protocol):
    def step(self, port: "Port", t_us: int) -> None:
        """Read inputs, write outputs; called once per step at virtual t_us."""


@dataclass(frozen=True)
class Signal:
    name: str
    kind: str          # analog | gpio_in | gpio_out | can_rx | can_tx
    spec: object


class Port:
    """One system's co-simulation port, over a running Sim."""

    def __init__(self, sim: Sim):
        doc = sim.system.doc.get("port")
        if not doc:
            raise ValueError(f"system {sim.system.id} declares no port")
        self.sim, self.step_ms = sim, float(doc["step_ms"])
        self.signals = {}
        for name, spec in doc["signals"].items():
            (kind, value), = spec.items()
            self.signals[name] = Signal(name, kind, value)
            if kind == "gpio_out":
                board, pin = value.split(".", 1)
                _, _, target = sim.system.resolve(value)
                sim.io(board).watch(target["port"], target["pin"])
        self._since: dict[str, int] = {}
        self._pending: list[tuple[str, int, bytes]] = []
        self._inbox: dict[str, list[Frame]] = {}      # this step's frames per bus
        self._armed: set[str] = set()                 # gpio_out signals that end a run
        self._periodic: dict[str, tuple[int, int]] = {}   # can_tx name -> (start_us, period_us)
        self._seen: dict[str, set] = {}               # gpio_out name -> edges already handed out
        self._edges_at: dict[str, int] = {}           # gpio_out name -> when edges() last looked

    def _signal(self, name: str, *kinds: str) -> Signal:
        s = self.signals.get(name)
        if s is None or s.kind not in kinds:
            raise KeyError(f"no {'/'.join(kinds)} signal '{name}' on the port")
        return s

    # -- plant -> system ----------------------------------------------------

    def set_voltage(self, name: str, volts: float) -> None:
        s = self._signal(name, "analog")
        board, pin = s.spec.split(".", 1)
        self.sim.io(board).set_voltage(pin, volts)

    def set_level(self, name: str, level: bool) -> None:
        s = self._signal(name, "gpio_in")
        board, _ = s.spec.split(".", 1)
        _, _, target = self.sim.system.resolve(s.spec)
        self.sim.io(board).set_input(target["port"], target["pin"], level)

    def send(self, name: str, data: bytes) -> None:
        s = self._signal(name, "can_tx")
        self._pending.append((s.spec["bus"], s.spec["id"], bytes(data)))

    def schedule(self, name: str, entries, period_ms: float) -> tuple[int, int]:
        """A can_tx signal sent every period_ms by the emulation itself, with
        its payload changing at future times: entries [(t_us, data or None)]
        in time order (None: no frame from then on) replace any still pending
        at or after the first (CanBus.schedule_periodic). The first call
        starts the sender, its first tick one period from now. Returns
        (first tick, period) in us, the grid every tick is on."""
        s = self._signal(name, "can_tx")
        bus = self.sim.can(s.spec["bus"])
        key = f"port_{name}"
        if name not in self._periodic:
            period_us = int(round(period_ms * 1000))
            start = self.sim.now_us() + period_us
            first = next(((t, d) for t, d in entries if t <= start), None)
            bus.send_periodic(key, s.spec["id"], first[1] if first and first[1] else b"",
                              period_ms, start_us=start)
            self._periodic[name] = (start, period_us)
        bus.schedule_periodic(key, list(entries))
        return self._periodic[name]

    def ticks(self, name: str) -> Optional[tuple[int, int]]:
        """(first tick, period) of a scheduled can_tx signal, None before."""
        return self._periodic.get(name)

    # -- system -> plant ----------------------------------------------------

    def received(self, name: str) -> list[Frame]:
        """Frames of this signal since the last call (the last step)."""
        s = self._signal(name, "can_rx")
        bus, since = s.spec["bus"], self._since.get(name, 0)
        if bus not in self._inbox:            # one query per bus per step
            first = min((self._since.get(n, 0) for n, x in self.signals.items()
                         if x.kind == "can_rx" and x.spec["bus"] == bus), default=0)
            ids = sorted({x.spec["id"] for x in self.signals.values()
                          if x.kind == "can_rx" and x.spec["bus"] == bus})
            self._inbox[bus] = self.sim.can(bus).frames(ids, since_us=first)
        self._since[name] = self.sim.now_us() + 1
        return [f for f in self._inbox[bus] if f.id == s.spec["id"] and f.t_us >= since]

    def level(self, name: str) -> bool:
        s = self._signal(name, "gpio_out")
        _, _, target = self.sim.system.resolve(s.spec)
        return self.sim.io(s.spec.split(".", 1)[0]).level(f"{target['port']}:{target['pin']}")

    def _pin(self, s: Signal) -> tuple[str, str]:
        _, _, target = self.sim.system.resolve(s.spec)
        return s.spec.split(".", 1)[0], f"{target['port']}:{target['pin']}"

    def edges(self, name: str) -> list[tuple[int, bool]]:
        """A gpio_out signal's level changes since the last call, [(t_us,
        level)] in order, each handed out once. t_us is the start of the sync
        quantum the board wrote the pin in (vhil/sim.py Edge)."""
        s = self._signal(name, "gpio_out")
        board, pin = self._pin(s)
        seen = self._seen.setdefault(name, set())
        now = self.sim.now_us()
        last = self._edges_at.get(name)
        since = 0 if last is None else max(0, last - 2 * self.sim.system.sync_quantum_us())
        self._edges_at[name] = now
        out = []
        for e in self.sim.io(board).edges(pin, since_us=since):
            key = (e.t_us, e.level, e.instructions)
            if key not in seen:
                seen.add(key)
                out.append((e.t_us, e.level))
        return out

    def driven(self, name: str) -> bool:
        """The level held on a gpio_in signal (by a plant, a scenario row or
        a test); False when nothing drives it."""
        s = self._signal(name, "gpio_in")
        board, _ = s.spec.split(".", 1)
        _, _, target = self.sim.system.resolve(s.spec)
        return self.sim.io(board).driven(target["port"], target["pin"])

    def arm(self, name: str) -> None:
        """A gpio_out signal whose edges end a run at the next sync point."""
        if name in self._armed:
            return
        s = self._signal(name, "gpio_out")
        board, pin = self._pin(s)
        self.sim.io(board).trigger(pin, True)
        self._armed.add(name)

    # -- lock-step ------------------------------------------------------------

    def run(self, plants: Iterable[Plant], ms: float) -> int:
        """Run plants and system together for ms of virtual time; returns the
        virtual time reached (us). Lock-step plants are stepped every step_ms
        from the run's start; event-driven ones at the start, at their
        triggers, at their next_us and with the lock-step ones."""
        plants = list(plants)
        lock = [p for p in plants if not getattr(p, "event_driven", False)]
        events = [p for p in plants if getattr(p, "event_driven", False)]
        for p in events:
            for name in p.triggers(self):
                self.arm(name)
        start = self.sim.now_us()
        end = start + int(ms * 1000)
        step_us = int(round(self.step_ms * 1000))
        next_lock = start
        while self.sim.now_us() < end:
            t = self.sim.now_us()
            for plant in plants:
                if plant in events or t >= next_lock:
                    plant.step(self, t)
            if t >= next_lock:
                next_lock += step_us * max(1, -(-(t - next_lock + 1) // step_us))
            for bus in dict.fromkeys(b for b, _, _ in self._pending):
                self.sim.can(bus).send_batch([(i, d) for b, i, d in self._pending if b == bus])
            self._pending.clear()
            self._inbox.clear()
            target = min([end] + ([next_lock] if lock else [])
                         + [n for n in (getattr(p, "next_us", None) for p in events)
                            if n is not None and n > t])
            if self._armed:
                self.sim.run_until_trigger(target - t)
            else:
                self.sim.run_for(us=target - t)
        return self.sim.now_us()
