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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

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

    # -- lock-step ------------------------------------------------------------

    def run(self, plants: Iterable[Plant], ms: float) -> int:
        """Run plants and system together for ms of virtual time; returns the
        virtual time reached (us)."""
        plants = list(plants)
        end = self.sim.now_us() + int(ms * 1000)
        while self.sim.now_us() < end:
            t = self.sim.now_us()
            for plant in plants:
                plant.step(self, t)
            for bus in dict.fromkeys(b for b, _, _ in self._pending):
                self.sim.can(bus).send_batch([(i, d) for b, i, d in self._pending if b == bus])
            self._pending.clear()
            self._inbox.clear()
            self.sim.run_for(ms=self.step_ms)
        return self.sim.now_us()
