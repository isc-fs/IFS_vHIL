"""Virtual hil-broker: IFS_HIL's broker protocol, backed by Renode.

IFS_HIL's tests talk to the bench through `hil-broker` (newline-delimited
JSON-RPC on a Unix socket). This serves the same protocol with IFS_HIL's own
server and its in-memory FakeHardwareManager, and overrides only the calls
that power a board (the system's `bench.power` maps the bench's relays and
current monitors to its boards):

  tca.write_pin on a board's relay   -> power: open = its CAN controllers
                                        leave their buses, close = machine
                                        Reset and back on the buses
  ina.current on a board's monitor   -> its nominal draw while powered, else 0
  dac.set_voltage on a routed channel -> `<adc> SetVoltage <uV> <ch>`
  tca.* on a pin a gpio_route wires   -> the board's GPIO input driven with
                                        the pin's level (low unless output)
  adc.read* on an adc_route channel   -> the board's GPIO output level now,
                                        0 or 3.3 V (0 V while unpowered)

Everything else (PSU, health, unrouted DACs and ADC channels, other TCA pins)
keeps the fake's behaviour, which is what an off-bench run already relies on.

  python -m vhil.broker --ifs-hil ../IFS_HIL --renode-port 1234 \
      --socket /tmp/hil-broker.sock [--system systems/ecu.yaml]
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import time
from pathlib import Path


from vhil import renode as rn
from vhil.renode import RenodeMonitor

log = logging.getLogger("vhil.broker")
REPO = Path(__file__).resolve().parent.parent

# RTC backup registers: RTC_BASE 0x58004000 (stm32h733xx.h:2394), BKP0R at
# offset 0x50 (stm32h733xx.h:1331). Renode's STM32F4_RTC holds the F4's 20,
# BKP0R..BKP19R; the H733's BKP20R..BKP31R (to 0xCC) are unmodelled, so they
# hold nothing to wipe.
RTC_BKP = range(0x58004050, 0x58004050 + 20 * 4, 4)


# After a power-on, where a CPU must not be (#125), and the system-control
# registers that say why it is there (ARMv7-M ARM B3.2: ICSR, SHCSR, CFSR,
# HFSR, MMFAR, BFAR).
BOOT_CHECK_S = 0.5
FAULT_HANDLERS = ("HardFault_Handler", "MemManage_Handler", "BusFault_Handler",
                  "UsageFault_Handler", "Default_Handler")
SCB_FAULT_REGS = (("ICSR", 0xE000ED04), ("SHCSR", 0xE000ED24), ("CFSR", 0xE000ED28),
                  ("HFSR", 0xE000ED2C), ("MMFAR", 0xE000ED34), ("BFAR", 0xE000ED38))


# The bench's MCP3208s: 12-bit, VREF 3.3 V (IFS_HIL broker/fake_bus.py,
# adc_read_voltage: counts * 3.3 / 4095).
ADC_FULL_SCALE = 4095
ADC_VREF_V = 3.3
PROBE_SOURCE = REPO / "models" / "renode" / "VhilProbe.cs"


def probe_name(machine: str) -> str:
    """The GPIO probe of a board, as vhil.sim names it."""
    return f"vhil_gpio_{rn.ident(machine)}"


def probe_commands(config: dict) -> list[str]:
    """Monitor commands, run once the system's script is loaded, that give
    every board a routed GPIO touches its GPIO probe (models/renode/
    VhilProbe.cs) and watch each output an ADC route reads. None for a
    system without GPIO or ADC routes."""
    routes = config.get("gpio_routes", []) + config.get("adc_routes", [])
    if not routes:
        return []
    out = [f"include {rn.file_arg(PROBE_SOURCE)}"]
    for machine in dict.fromkeys(r["machine"] for r in routes):
        out += [f"mach set {rn.quote(rn.ident(machine))}",
                f"emulation CreateVhilGpioProbe {rn.quote(probe_name(machine))} "
                f"{rn.quote(machine)}"]
        for r in config.get("adc_routes", []):
            if r["machine"] == machine:
                out.append(f"{probe_name(machine)} Watch {rn.quote(rn.path(r['gpio_port']))} "
                           f"{int(r['gpio_pin'])}")
    return out


def power_on_commands(machine: str, vbat: bool) -> list[str]:
    """Monitor commands that power a board on, with its machine selected.
    Shared by the virtual broker and vhil.sim, so both mean the same thing.

    machine Reset keeps the backup domain, as a warm reset does. Without VBAT
    a power cut wipes it (the AMS's sticky ErrorLatch must not outlive the
    board's power), so it is cleared first, before the booting firmware can
    read the old value: the RTC backup registers, and the 4 KB backup SRAM
    at 0x38800000 that the CAN bootloader keeps its DTC log in (RM0468:
    retained only from the backup domain supply). The reset macro points VTOR
    at the bootloader in sector 0, which boots the app; flash keeps what it
    held, as on the chip.
    Renode 1.17: Reset zeroes BASEPRI as read, but not the masking it applies
    (renode/renode#1021); a cut inside a FreeRTOS critical section left the
    next boot unable to take the TIM23 HAL tick, hanging in HAL_Delay. (The
    board's reset macro clears it as well, on every reset, the watchdog's
    included: catalog/platforms/stm32h733.yaml, renode.reset.)
    The reset-flag model is told the reset is a power-on, so RCC_RSR reads
    POR (models/renode/VhilResetFlags.cs)."""
    wipe = [] if vbat else ([f"sysbus WriteDoubleWord {addr:#x} 0x0" for addr in RTC_BKP]
                            + ["sysbus.backupSram ZeroAll"])
    return wipe + [f"vhil_reset_{rn.ident(machine)} PowerOn", "machine Reset",
                   'cpu SetRegister "BasePri" 0x0']


def make_backend(fake_cls, monitor: RenodeMonitor, config: dict, boot_check: bool = False):
    """Build the backend as a subclass of IFS_HIL's FakeHardwareManager."""

    relays = {(c["relay"]["addr"], c["relay"]["port"], c["relay"]["pin"]): c
                for c in config.get("power", [])}
    vbat = {c["machine"]: c.get("vbat", True) for c in config.get("power", [])}
    by_ina = {c["ina_addr"]: c for c in config.get("power", [])}
    routes = {(r["dac"], r["channel"]): r for r in config.get("dac_routes", [])}
    gpio_routes = {(r["tca"], r["port"], r["pin"]): r for r in config.get("gpio_routes", [])}
    adc_routes = {(r["adc"], r["channel"]): r for r in config.get("adc_routes", [])}
    # The broker server is threaded; `mach set` + the command must not interleave.
    lock = threading.Lock()
    can_of = {c["machine"]: c.get("can", {}) for c in config.get("power", [])}

    def checked(command: str) -> str:
        """Run a power-path command and log anything Renode says back: these
        commands print nothing when they work, so any reply is a clue (#63)."""
        reply = monitor.execute(command).strip()
        if reply:
            log.warning("monitor: %s -> %s", command, reply)
        return reply

    def mach_set(machine: str) -> None:
        monitor.execute(f"mach set {rn.quote(rn.ident(machine))}")

    def connector(verb: str, controller: str, hub: str) -> str:
        return f"connector {verb} {rn.path(controller)} {rn.ident(hub)}"

    def set_buses(machine: str, connect: bool) -> None:
        verb = "Connect" if connect else "Disconnect"
        with lock:
            mach_set(machine)
            for controller, hub in can_of[machine].items():
                checked(connector(verb, controller, hub))

    class VirtualHardwareManager(fake_cls):
        def __init__(self) -> None:
            super().__init__()
            self._powered = {c["machine"]: False for c in relays.values()}
            self._tca_mirror: dict = {}     # (addr, port) -> output/direction registers
            # (addr, port, pin) -> level on the line: undriven, the pull-down's
            self._driven: dict = {key: False for key in gpio_routes}

        def _on(self, machine: str, command: str) -> str:
            with lock:
                mach_set(machine)
                return monitor.execute(command)

        def _power_pin(self, addr, port, pin, value):
            super().tca_write_pin(addr, port, pin, value)
            entry = relays.get((addr, port, pin))
            if entry is None:
                return
            machine, value = entry["machine"], bool(value)
            if value == self._powered[machine]:
                return
            # Power is the board's CAN controllers on/off its buses, not
            # machine Pause: Renode paces virtual time to host time only while
            # a CPU executes, and repays a paused interval by running fast
            # afterwards (3x seen in CI), which breaks IFS_HIL's wall-clock
            # checks. Unpowered, the firmware keeps running unheard; power-on
            # is a full reset, so it boots cold.
            #
            # On power-on the buses connect BEFORE the reset releases the
            # firmware, as a real transceiver is on the bus from the first
            # instant (#63). Renode's MCAN, unconnected, keeps each TX request
            # pending; a booted firmware that fills its TX FIFO before the
            # Connect lands (FDCAN2's 16 slots: ~80 ms of 0x100 + 0x506) is
            # refused from then on and never writes TXBAR again, so that bus
            # stays silent until the next power cycle. The board's machine is
            # paused (only it; the others run on) for the Connect and the
            # reset, so the old firmware can't queue frames in between, and
            # started after: `machine Reset` keeps a paused machine paused.
            # Not `cpu IsHalted`: halting mid-exception-entry let a pending
            # PendSV survive the reset (the AMS then entered it with PSP = 0)
            # and could leave Reset waiting on the halted CPU thread (#125).
            # The pause lasts the few commands, far too short for the pacing
            # repay above to matter.
            if value:
                with lock:
                    mach_set(machine)
                    checked("machine Pause")
                    try:
                        for controller, hub in can_of[machine].items():
                            checked(connector("Connect", controller, hub))
                        for command in power_on_commands(machine, vbat[machine]):
                            checked(command)
                    finally:
                        checked("machine Start")
            else:
                set_buses(machine, connect=False)
                # Where the CPU was when power went: the first clue when a
                # boot never reaches the bus.
                pc = self._on(machine, "cpu PC").strip()
                log.info("%s power cut at %s %s", machine, pc, self._symbol(machine, pc))
            self._powered[machine] = value
            log.info("%s %s", machine, "powered" if value else "unpowered")
            if value and boot_check:
                timer = threading.Timer(BOOT_CHECK_S, self._check_boot, args=(machine,))
                timer.daemon = True
                timer.start()

        def _symbol(self, machine: str, pc: str) -> str:
            """The symbol at a PC Renode printed; '?' if the reply is no address."""
            try:
                address = int(pc.split()[-1], 0)
            except (IndexError, ValueError):
                return "?"
            return self._on(machine, f"sysbus FindSymbolAt {address:#x}").strip()

        def _check_boot(self, machine):
            """#125: a board found in a fault handler shortly after power-on
            leaves its fault state in the log, while it is still there."""
            try:
                if not self._powered.get(machine):
                    return
                pc = self._on(machine, "cpu PC").strip()
                sym = self._symbol(machine, pc)
                if not any(h in sym for h in FAULT_HANDLERS):
                    return
                regs = {name: self._on(machine, f"sysbus ReadDoubleWord {addr:#x}").strip()
                        for name, addr in SCB_FAULT_REGS}
                log.warning("%s in %s (pc %s) %.1f s after power-on: %s", machine, sym, pc,
                            BOOT_CHECK_S, " ".join(f"{k}={v}" for k, v in regs.items()))
            except Exception as e:                    # never take the broker down
                log.warning("%s boot check failed: %s", machine, e)

        def ina_current(self, addr):
            entry = by_ina.get(addr)
            if entry is None:
                return super().ina_current(addr)
            self._tick()
            return float(entry["current_A"]) if self._powered[entry["machine"]] else 0.0

        def dac_set_voltage(self, idx, channel, volts):
            super().dac_set_voltage(idx, channel, volts)
            route = routes.get((idx, channel))
            if route is None:
                return
            uv = max(0, int(round(float(volts) * 1e6)))
            self._on(route["machine"],
                     f"{rn.path(route['adc'])} SetVoltage {uv} {int(route['adc_channel'])}")

        # -- TCA pins wired to GPIO inputs (bench gpio_routes) ---------------
        # A TCA9555 pin drives its line only while its direction bit is 0
        # (output); as an input it floats and the board's pull decides (the
        # AMS's TSMS and DASH_CHG: GPIO_PULLDOWN, IFS08-CE-AMS main.c:722-725).
        # Output and direction registers start as the fake's: 0 and all inputs.

        def _tca_regs(self, addr, port):
            return self._tca_mirror.setdefault((addr, port), {"out": 0, "dir": 0xFF})

        def _drive_routes(self, addr, port):
            if not gpio_routes:
                return
            regs = self._tca_regs(addr, port)
            driven = self._driven
            for (a, p, pin), route in gpio_routes.items():
                if (a, p) != (addr, port):
                    continue
                level = bool(regs["out"] >> pin & 1) and not regs["dir"] >> pin & 1
                if driven.get((a, p, pin)) == level:
                    continue
                self._on(route["machine"], f"{probe_name(route['machine'])} Drive "
                         f"{rn.quote(rn.path(route['gpio_port']))} {int(route['gpio_pin'])} "
                         f"{'true' if level else 'false'}")
                driven[(a, p, pin)] = level

        def tca_write_pin(self, addr, port, pin, value):
            self._power_pin(addr, port, pin, value)
            regs = self._tca_regs(addr, port)
            regs["out"] = (regs["out"] | 1 << pin) if value else (regs["out"] & ~(1 << pin))
            self._drive_routes(addr, port)

        def tca_write_port(self, addr, port, value):
            super().tca_write_port(addr, port, value)
            self._tca_regs(addr, port)["out"] = int(value) & 0xFF
            self._drive_routes(addr, port)

        def tca_write_all(self, addr, p0, p1):
            super().tca_write_all(addr, p0, p1)
            for port, value in ((0, p0), (1, p1)):
                self._tca_regs(addr, port)["out"] = int(value) & 0xFF
                self._drive_routes(addr, port)

        def tca_set_direction(self, addr, port, mask):
            super().tca_set_direction(addr, port, mask)
            self._tca_regs(addr, port)["dir"] = int(mask) & 0xFF
            self._drive_routes(addr, port)

        def tca_set_all_inputs(self, addr):
            super().tca_set_all_inputs(addr)
            for port in (0, 1):
                self._tca_regs(addr, port)["dir"] = 0xFF
                self._drive_routes(addr, port)

        def tca_set_all_outputs(self, addr):
            super().tca_set_all_outputs(addr)
            for port in (0, 1):
                self._tca_regs(addr, port)["dir"] = 0x00
                self._drive_routes(addr, port)

        # -- ADC channels tapping GPIO outputs (bench adc_routes) -------------
        # The MCP3208 reads the pin's push-pull level, 0 or VDD (3.3 V), at
        # the instant of the read: the probe's level of the pin now, in
        # virtual time. An unpowered board drives nothing.

        def _routed_volts(self, route) -> float:
            machine = route["machine"]
            if machine in self._powered and not self._powered[machine]:
                return 0.0
            pin = f"{route['gpio_port']}:{int(route['gpio_pin'])}"
            reply = self._on(machine, f"{probe_name(machine)} Level {rn.quote(pin)}").strip()
            return ADC_VREF_V if reply == "True" else 0.0

        def adc_read_voltage(self, idx, channel):
            route = adc_routes.get((idx, channel))
            if route is None:
                return super().adc_read_voltage(idx, channel)
            self._tick()
            return self._routed_volts(route)

        def adc_read(self, idx, channel):
            route = adc_routes.get((idx, channel))
            if route is None:
                return super().adc_read(idx, channel)
            self._tick()
            return ADC_FULL_SCALE if self._routed_volts(route) else 0

        def adc_read_all(self, idx):
            values = super().adc_read_all(idx)
            for (i, channel), route in adc_routes.items():
                if i == idx and channel < len(values):
                    values[channel] = ADC_FULL_SCALE if self._routed_volts(route) else 0
            return values

        def health(self):
            h = super().health()
            h["backend"] = "virtual"
            return h

    backend = VirtualHardwareManager()
    backend.monitor_lock = lock
    return backend


def virtual_seconds(info: str) -> float:
    """Elapsed virtual time from `emulation GetTimeSourceInfo`."""
    h, m, s = re.search(r"Elapsed Virtual Time: (\d+):(\d+):([\d.]+)", info).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def watch_pacing(monitor: RenodeMonitor, lock: threading.Lock,
                 period_s: float = 0.25, stall_s: float = 0.15,
                 iterations: int | None = None) -> None:
    """Log whenever virtual time falls behind host time by more than stall_s
    within one period. IFS_HIL's suites time the bench by the wall clock;
    when Renode stops advancing (host starved, a long monitor command) and
    later repays the interval by running fast, they see a silent bus and then
    a burst (#63). The log line pins a wall-clock failure on the emulation."""
    last_wall = last_virt = None
    failed = False
    while iterations is None or iterations > 0:
        if iterations is not None:
            iterations -= 1
        time.sleep(period_s)
        try:
            with lock:
                virt = virtual_seconds(monitor.execute("emulation GetTimeSourceInfo"))
        except Exception as e:                 # monitor busy or emulation gone
            if not failed:
                log.warning("pacing watch: cannot read virtual time (%s)", e)
                failed = True
            continue
        wall = time.monotonic()
        if last_wall is not None:
            lag = (wall - last_wall) - (virt - last_virt)
            if lag > stall_s:
                log.warning("emulation fell %.0f ms behind host time (%.0f ms wall, "
                            "%.0f ms virtual)", lag * 1000, (wall - last_wall) * 1000,
                            (virt - last_virt) * 1000)
        last_wall, last_virt = wall, virt


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
    for command in probe_commands(config):
        monitor.execute(command)
    backend = make_backend(FakeHardwareManager, monitor, config, boot_check=True)
    threading.Thread(target=watch_pacing, args=(monitor, backend.monitor_lock),
                     name="pacing", daemon=True).start()
    log.info("virtual broker on %s (Renode monitor :%d)", args.socket, args.renode_port)
    serve(backend, args.socket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
