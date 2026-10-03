"""Scripted plants on a fake port: their logic without Renode."""
from vhil.plants import Inverter, Pedals, APPS1_REST, APPS1_FULL, COUNTS_TO_V
from vhil.sim import Frame


class FakePort:
    step_ms = 10.0

    def __init__(self):
        self.inbox, self.sent, self.volts, self.levels = {}, [], {}, {}

    def received(self, name):
        return self.inbox.pop(name, [])

    def send(self, name, data):
        self.sent.append((name, bytes(data)))

    def set_voltage(self, name, v):
        self.volts[name] = v

    def set_level(self, name, level):
        self.levels[name] = level


def _cmd(port, inv, *words, t=0):
    for w in words:
        port.inbox["inv_cmd"] = [Frame(t, 0x360, False, bytes([0, 0, w]))]
        inv.step(port, t)


def test_inverter_climbs_standby_ready_torque():
    port, inv = FakePort(), Inverter()
    _cmd(port, inv, Inverter.W_TORQUE)                 # not from Standby
    assert inv.state == Inverter.STANDBY
    _cmd(port, inv, Inverter.W_READY, Inverter.W_TORQUE)
    assert inv.state == Inverter.TORQUE
    _cmd(port, inv, Inverter.W_OFF)
    assert inv.state == Inverter.STANDBY


def test_faults_clear_only_on_their_word_sequence():
    port, inv = FakePort(), Inverter()
    inv.fault(hard=True)
    _cmd(port, inv, Inverter.W_OFF)                    # Off alone: still faulted
    assert inv.state == Inverter.HARD
    _cmd(port, inv, Inverter.W_HFR, Inverter.W_OFF)
    assert inv.state == Inverter.STANDBY
    inv.fault(hard=False)
    _cmd(port, inv, Inverter.W_HFR, Inverter.W_OFF)    # missing the Fault word
    assert inv.state == Inverter.SOFT
    _cmd(port, inv, Inverter.W_FAULT, Inverter.W_HFR, Inverter.W_OFF)
    assert inv.state == Inverter.STANDBY


def test_shutdown_takes_only_off_then_climbs():
    port, inv = FakePort(), Inverter(state=Inverter.SHUTDOWN)
    _cmd(port, inv, Inverter.W_READY)
    assert inv.state == Inverter.SHUTDOWN
    _cmd(port, inv, Inverter.W_OFF, Inverter.W_READY)
    assert inv.state == Inverter.READY


def test_inverter_frames_carry_state_vdc_and_speed():
    port, inv = FakePort(), Inverter(dc_bus_v=355, state=Inverter.TORQUE)
    port.inbox["inv_torque"] = [Frame(0, 0x362, False, bytes([0, 0]) + (-100).to_bytes(2, "little", signed=True))]
    inv.step(port, 0)
    sent = dict(port.sent)
    assert sent["inv_state"][4] == Inverter.TORQUE
    assert sent["inv_vdc"][2] | (sent["inv_vdc"][3] & 0x3) << 8 == 355
    assert inv.torque_nm == -100 and inv.rpm > 0       # forward = negative torque


def test_pedals_map_throttle_onto_the_calibrated_span():
    port = FakePort()
    Pedals(lambda t: (1.0, 0.0, True)).step(port, 0)
    assert abs(port.volts["apps1"] - APPS1_FULL * COUNTS_TO_V) < 1e-9
    Pedals(lambda t: (0.0, 0.0, False)).step(port, 0)
    assert abs(port.volts["apps1"] - APPS1_REST * COUNTS_TO_V) < 1e-9 and port.levels["start"] is False
