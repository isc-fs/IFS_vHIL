"""Scripted plants for the co-simulation port (vhil/cosim.py, #14).

Each is a small model of something outside the electronics, written from
what the car's hardware and the firmware record about it. None replaces a
board: a stimulus standing in for a missing board says so.

  Inverter      the traction inverter and motor on the ECU's FDCAN1
  Pedals        the driver: APPS1/APPS2/brake voltages and the START button
  AcuStimulus   NOT a plant: the AMS's frames for a system without an AMS
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


# -- inverter -----------------------------------------------------------------

def _e2e(dlc: int, cnt: int, fill: Callable[[bytearray], None]) -> bytes:
    """E2E Profile-1 framing: byte 0 CRC, byte 1 low nibble counter. The ECU
    does not check either (vehicle_service.cpp), so the CRC is left 0."""
    buf = bytearray(dlc)
    buf[1] = cnt & 0x0F
    fill(buf)
    return bytes(buf)


@dataclass
class Inverter:
    """The traction inverter (EMC, A16 configuration) and its motor.

    Application state machine as the ECU firmware's bench notes describe it
    (IFS08-CE-ECU control.cpp WaitInvStandby/Active/fault recovery,
    ecu_config.hpp Inv*State): Standby(3) --Ready(0x04)--> Ready(4)
    --TorqueEnable(0x06)--> TorqueEnable(6); Off(0x01) returns a running
    inverter to Standby, and from Off(0) it climbs Off then Ready; a soft
    fault (10) clears on Fault(0x13), HardFaultReset(0x0D), Off; a hard fault
    (11) on HardFaultReset then Off. Shutdown(13) takes only Off.

    Frames (vehicle_service.cpp decoders): 0x461 App_State_App in byte 4
    (7 bits); 0x466 DCBus_Voltage_V, 10 bits LE from bit 16; 0x463
    EMachine_Speed_erpm, 20 bits signed from bit 44. Setpoints in: 0x360
    App_State_Req = byte 2 bits 0-6, Flt_Clear bit 7; 0x362 Torque_Nm_Req =
    bytes 2-3 LE signed, negative for forward drive (the motor's mounting).

    Motor: a first-order speed model, d(rpm)/dt = gain * torque - drag * rpm,
    a placeholder for the vehicle MingoCIL will provide.
    """
    dc_bus_v: int = 350
    state: int = 3                       # boots in Standby with the DC bus up
    period_ms: float = 10.0
    gain_rpm_per_nm_s: float = 20.0
    drag_per_s: float = 0.2
    rpm: float = 0.0
    torque_nm: int = 0
    history: list = field(default_factory=list)   # (t_us, state) on each change
    _cnt: int = 0
    _clear: list = field(default_factory=list)    # fault-recovery words seen
    _last_tx_us: int = -1_000_000

    STANDBY, READY, TORQUE, SOFT, HARD, SHUTDOWN, OFF = 3, 4, 6, 10, 11, 13, 0
    W_OFF, W_READY, W_TORQUE, W_FAULT, W_HFR = 0x01, 0x04, 0x06, 0x13, 0x0D

    def _go(self, state: int, t_us: int) -> None:
        if state != self.state:
            self.state = state
            self.history.append((t_us, state))

    def _command(self, word: int, t_us: int) -> None:
        s = self.state
        if s in (self.SOFT, self.HARD):
            self._clear.append(word)
            need = [self.W_FAULT, self.W_HFR, self.W_OFF] if s == self.SOFT else [self.W_HFR, self.W_OFF]
            if self._clear[-len(need):] == need:
                self._clear.clear()
                self._go(self.STANDBY, t_us)
        elif word == self.W_OFF:
            self._go(self.OFF if s == self.SHUTDOWN else
                     self.STANDBY if s in (self.READY, self.TORQUE) else s, t_us)
        elif word == self.W_READY and s in (self.STANDBY, self.OFF, self.TORQUE):
            self._go(self.READY, t_us)
        elif word == self.W_TORQUE and s == self.READY:
            self._go(self.TORQUE, t_us)

    def fault(self, hard: bool, t_us: int = 0) -> None:
        """Trip the inverter (test API)."""
        self._clear.clear()
        self._go(self.HARD if hard else self.SOFT, t_us)

    def step(self, port, t_us: int) -> None:
        for f in port.received("inv_cmd"):
            if len(f.data) >= 3:
                self._command(f.data[2] & 0x7F, t_us)
        for f in port.received("inv_torque"):
            if len(f.data) >= 4:
                self.torque_nm = int.from_bytes(f.data[2:4], "little", signed=True)
        dt = port.step_ms / 1000
        drive = -self.torque_nm if self.state == self.TORQUE else 0   # forward = negative
        self.rpm += (self.gain_rpm_per_nm_s * drive - self.drag_per_s * self.rpm) * dt
        if t_us - self._last_tx_us >= self.period_ms * 1000:
            self._last_tx_us = t_us
            self._cnt = (self._cnt + 1) & 0x0F
            port.send("inv_state", _e2e(7, self._cnt, lambda b: b.__setitem__(4, self.state & 0x7F)))
            v = self.dc_bus_v & 0x3FF
            port.send("inv_vdc", _e2e(6, self._cnt, lambda b: (b.__setitem__(2, v & 0xFF),
                                                                b.__setitem__(3, v >> 8))))
            raw = int(round(self.rpm)) & 0xFFFFF
            port.send("inv_rpm", bytes([0, 0, 0, 0, 0, (raw & 0x0F) << 4, (raw >> 4) & 0xFF,
                                        (raw >> 12) & 0xFF]))


# -- pedals ---------------------------------------------------------------------

# The car's pedal sensors as calibrated on it (IFS08-CE-ECU ecu_config.hpp:95-98
# "rest"/"full" readings; ADC3 12-bit, 3.3 V reference) and its brake sensor
# (released ~580, R2D arm 750 at 4.1 bar, ecu_config.hpp:116-123).
APPS1_REST, APPS1_FULL = 2476, 3363
APPS2_REST, APPS2_FULL = 2332, 3037
BRAKE_RELEASED, BRAKE_FIRM = 580, 1500
COUNTS_TO_V = 3.3 / 4095


@dataclass
class Pedals:
    """A driver following a script: profile(t_s) -> (throttle 0..1, brake
    0..1, start pressed)."""
    profile: Callable[[float], tuple[float, float, bool]]
    _last: tuple | None = None

    def step(self, port, t_us: int) -> None:
        throttle, brake, start = self.profile(t_us / 1e6)
        throttle, brake = min(max(throttle, 0.0), 1.0), min(max(brake, 0.0), 1.0)
        if (throttle, brake, start) == self._last:
            return                          # inputs hold their value: nothing to drive
        self._last = (throttle, brake, start)
        port.set_voltage("apps1", (APPS1_REST + throttle * (APPS1_FULL - APPS1_REST)) * COUNTS_TO_V)
        port.set_voltage("apps2", (APPS2_REST + throttle * (APPS2_FULL - APPS2_REST)) * COUNTS_TO_V)
        port.set_voltage("brake", (BRAKE_RELEASED + brake * (BRAKE_FIRM - BRAKE_RELEASED)) * COUNTS_TO_V)
        port.set_level("start", start)


# -- the AMS, for a system without one ------------------------------------------

@dataclass
class AcuStimulus:
    """NOT a plant: stands in for the AMS on a system that has none, sending
    what the ECU gates on (vehicle_service.cpp): 0x020 ok_precharge (byte 0
    non-zero) and 0x4A0 status (state, ams_ok), every 50 ms, inside the ECU's
    200 ms AmsStaleMs. systems/ecu-ams.yaml has the real AMS instead."""
    ok_precharge: bool = True
    period_ms: float = 50.0
    _last_us: int = -1_000_000

    def step(self, port, t_us: int) -> None:
        if t_us - self._last_us >= self.period_ms * 1000:
            self._last_us = t_us
            port.send("ams_ok_precharge", bytes([1 if self.ok_precharge else 0]))
            port.send("ams_status", bytes([0, 1, 0x1F, 0, 0x0E, 0x74, 0x0E, 0x74]))
