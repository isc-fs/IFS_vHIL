"""The car around an AMS, scripted: the ECU's 0x100 heartbeat (DC-link volts,
discharge state), the TSMS switch and the DASH_CHG button, and the FSM read
back from the firmware's telemetry globals. Shared by the ams-fsm-* and
ams-contactors suites.

AMS facts (IFS08-CE-AMS vcu_heartbeat.def, main.h, safety_task.cpp):
  0x100 every 10 ms: LE u16 dc_bus_V, bit 16 discharge_engaged, bit 17
  dc_bus_valid. A DLC >= 3 frame marks the ECU discharge-capable, which arms
  the re-arm gate (link <= DcBusDischargedV = 60 V to start a precharge).
  TSMS PF9 (held switch), DASH_CHG PF10 (momentary press, rising edge),
  sampled every 10 ms; the FSM steps every 20 ms.
  g_state_telemetry: 0 Start, 1 Precharge, 2 Transition, 3 Run, 4 Charge,
  5 Error; g_fault_reason_telemetry; g_mode_locked_telemetry: 0 Undecided,
  1 Car, 2 Charger.
"""
from vhil.sim import Sim

START, PRECHARGE, TRANSITION, RUN, CHARGE, ERROR = range(6)
GPIOB, GPIOF = "sysbus.gpioPortB", "sysbus.gpioPortF"
AMS_OK, AIR_P, AIR_N, PRECHARGE_RELAY = 4, 5, 6, 7
TSMS, DASH_CHG = 9, 10
VCU, CHARGE_REQ = 0x100, 0x101
PACK_V = 95 * 3.7                       # the models' default pack: 351.5 V


def vcu_frame(volts: int, valid: bool = True, discharge: bool = False) -> bytes:
    return int(volts).to_bytes(2, "little") + bytes([(discharge << 0) | (valid << 1)])


class Car:
    def __init__(self, sim: Sim):
        self.sim = sim
        self.can = sim.can("can_acu")
        self.io = sim.io("ams")
        self._vcu = False

    # -- the ECU ----------------------------------------------------------
    def vcu(self, volts: int, valid: bool = True, discharge: bool = False) -> None:
        data = vcu_frame(volts, valid, discharge)
        if self._vcu:
            self.can.update_periodic("vcu", data)
        else:
            self.can.send_periodic("vcu", VCU, data, period_ms=10)
            self._vcu = True

    def vcu_silent(self) -> None:
        if self._vcu:
            self.can.stop_periodic("vcu")
            self._vcu = False

    def charger(self) -> None:
        """The charger's 0x101 "CHRG" request at 2 Hz."""
        self.can.send_periodic("chrg", CHARGE_REQ, b"CHRG", period_ms=500)

    # -- the cockpit ------------------------------------------------------
    def tsms(self, on: bool) -> None:
        self.io.set_input(GPIOF, TSMS, on)

    def dash(self, on: bool) -> None:
        self.io.set_input(GPIOF, DASH_CHG, on)

    def press(self, hold_ms: int = 50) -> None:
        self.dash(True)
        self.sim.run_for(ms=hold_ms)
        self.dash(False)

    # -- the AMS ----------------------------------------------------------
    def state(self) -> int:
        return self.sim.read_symbol("ams", "g_state_telemetry")

    def reason(self) -> int:
        return self.sim.read_symbol("ams", "g_fault_reason_telemetry")

    def mode(self) -> int:
        return self.sim.read_symbol("ams", "g_mode_locked_telemetry")

    def wait_for(self, state: int, limit_ms: int, step_ms: int = 5):
        """Virtual ms until the FSM reports state, or None past limit_ms."""
        for elapsed in range(step_ms, limit_ms + step_ms, step_ms):
            self.sim.run_for(ms=step_ms)
            if self.state() == state:
                return elapsed
        return None

    def arm(self) -> None:
        """Start -> Precharge in Car mode: a drained, valid link, TSMS on,
        one press."""
        self.vcu(0)
        self.tsms(True)
        self.sim.run_for(ms=100)
        self.press()
        assert self.wait_for(PRECHARGE, 100) is not None or self.state() == PRECHARGE, \
            f"no Precharge after TSMS + press (state {self.state()})"

    def to_run(self) -> None:
        """Arm, then let the link follow the precharge to the pack voltage."""
        self.arm()
        self.vcu(round(PACK_V))
        assert self.wait_for(RUN, 200) is not None, f"no Run (state {self.state()})"
