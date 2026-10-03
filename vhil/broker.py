"""Virtual hil-broker: IFS_HIL's broker protocol, backed by Renode.

IFS_HIL's tests talk to the bench through `hil-broker` (newline-delimited
JSON-RPC on a Unix socket). This serves the same protocol with IFS_HIL's own
server and its in-memory FakeHardwareManager, and overrides only the calls
that reach a carrier:

  tca.write_pin on a carrier relay   -> power: open = its CAN controllers
                                        leave their buses, close = machine
                                        Reset and back on the buses
  ina.current on a carrier's monitor -> its nominal draw while powered, else 0
  dac.set_voltage on a routed channel -> `<adc> SetVoltage <uV> <ch>`

Everything else (PSU, health, unrouted DACs, other TCA pins) keeps the fake's
behaviour, which is what an off-bench run already relies on.

  python -m vhil.broker --ifs-hil ../IFS_HIL --renode-port 1234 \
      --socket /tmp/hil-broker.sock [--system systems/ecu.yaml]
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path


from vhil.renode import RenodeMonitor

log = logging.getLogger("vhil.broker")
REPO = Path(__file__).resolve().parent.parent

# RTC backup registers: RTC_BASE 0x58004000 (stm32h733xx.h:2394), BKP0R at
# offset 0x50 (stm32h733xx.h:1331). Renode's STM32F4_RTC holds the F4's 20,
# BKP0R..BKP19R; the H733's BKP20R..BKP31R (to 0xCC) are unmodelled, so they
# hold nothing to wipe.
RTC_BKP = range(0x58004050, 0x58004050 + 20 * 4, 4)


def power_on_commands(vbat: bool) -> list[str]:
    """Monitor commands that power a board on, with its machine selected.
    Shared by the virtual broker and vhil.sim, so both mean the same thing.

    machine Reset keeps the backup domain, as a warm reset does. Without VBAT
    a power cut wipes it (the AMS's sticky ErrorLatch must not outlive the
    carrier's power), so it is cleared first, before the booting firmware can
    read the old value. The reset macro reloads the image and sets VTOR.
    Renode 1.17: Reset zeroes BASEPRI as read, but not the masking it applies
    (renode/renode#1021); a cut inside a FreeRTOS critical section left the
    next boot unable to take the TIM23 HAL tick, hanging in HAL_Delay."""
    wipe = [] if vbat else [f"sysbus WriteDoubleWord {addr:#x} 0x0" for addr in RTC_BKP]
    return wipe + ["machine Reset", 'cpu SetRegister "BasePri" 0x0']


def make_backend(fake_cls, monitor: RenodeMonitor, config: dict):
    """Build the backend as a subclass of IFS_HIL's FakeHardwareManager."""

    carriers = {(c["relay"]["addr"], c["relay"]["port"], c["relay"]["pin"]): c
                for c in config.get("carriers", [])}
    vbat = {c["machine"]: c.get("vbat", True) for c in config.get("carriers", [])}
    by_ina = {c["ina_addr"]: c for c in config.get("carriers", [])}
    routes = {(r["dac"], r["channel"]): r for r in config.get("dac_routes", [])}
    # The broker server is threaded; `mach set` + the command must not interleave.
    lock = threading.Lock()
    can_of = {c["machine"]: c.get("can", {}) for c in config.get("carriers", [])}

    def set_buses(machine: str, connect: bool) -> None:
        verb = "Connect" if connect else "Disconnect"
        with lock:
            monitor.execute(f'mach set "{machine}"')
            for controller, hub in can_of[machine].items():
                monitor.execute(f"connector {verb} {controller} {hub}")

    class VirtualHardwareManager(fake_cls):
        def __init__(self) -> None:
            super().__init__()
            self._powered = {c["machine"]: False for c in carriers.values()}

        def _on(self, machine: str, command: str) -> str:
            with lock:
                monitor.execute(f'mach set "{machine}"')
                return monitor.execute(command)

        def tca_write_pin(self, addr, port, pin, value):
            super().tca_write_pin(addr, port, pin, value)
            carrier = carriers.get((addr, port, pin))
            if carrier is None:
                return
            machine, value = carrier["machine"], bool(value)
            if value == self._powered[machine]:
                return
            # Power is the carrier's CAN controllers on/off its buses, not
            # machine Pause: Renode paces virtual time to host time only while
            # a CPU executes, and repays a paused interval by running fast
            # afterwards (3x seen in CI), which breaks IFS_HIL's wall-clock
            # checks. Unpowered, the firmware keeps running unheard; power-on
            # is a full reset, so it boots cold.
            if value:
                with lock:
                    monitor.execute(f'mach set "{machine}"')
                    for command in power_on_commands(vbat[machine]):
                        monitor.execute(command)
                set_buses(machine, connect=True)
            else:
                set_buses(machine, connect=False)
                # Where the CPU was when power went: the first clue when a
                # boot never reaches the bus.
                pc = self._on(machine, "cpu PC").strip()
                log.info("%s power cut at %s %s", machine, pc,
                         self._on(machine, f"sysbus FindSymbolAt {pc}").strip())
            self._powered[machine] = value
            log.info("%s %s", machine, "powered" if value else "unpowered")

        def ina_current(self, addr):
            carrier = by_ina.get(addr)
            if carrier is None:
                return super().ina_current(addr)
            self._tick()
            return float(carrier["current_A"]) if self._powered[carrier["machine"]] else 0.0

        def dac_set_voltage(self, idx, channel, volts):
            super().dac_set_voltage(idx, channel, volts)
            route = routes.get((idx, channel))
            if route is None:
                return
            uv = max(0, int(round(float(volts) * 1e6)))
            self._on(route["machine"],
                     f"{route['adc']} SetVoltage {uv} {route['adc_channel']}")

        def health(self):
            h = super().health()
            h["backend"] = "virtual"
            return h

    return VirtualHardwareManager()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ifs-hil", required=True, type=Path,
                   help="IFS_HIL checkout (provides broker.server + fake_bus)")
    p.add_argument("--renode-port", type=int, default=1234)
    p.add_argument("--socket", default="/tmp/hil-broker.sock")
    p.add_argument("--system", type=Path, default=REPO / "systems" / "ecu.yaml",
                   help="system file whose bench wiring to serve")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    sys.path.insert(0, str(args.ifs_hil.resolve()))
    from broker.fake_bus import FakeHardwareManager
    from broker.server import serve

    from vhil.system import System
    config = System(args.system).bench_config()
    monitor = RenodeMonitor(args.renode_port)
    backend = make_backend(FakeHardwareManager, monitor, config)
    log.info("virtual broker on %s (Renode monitor :%d)", args.socket, args.renode_port)
    serve(backend, args.socket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
