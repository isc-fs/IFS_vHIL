"""Host-only checks of the virtual broker's wiring, against a stand-in for
IFS_HIL's FakeHardwareManager and a monitor that records commands."""
from vhil.broker import make_backend
from vhil.system import REPO, System


class _Fake:
    """The FakeHardwareManager calls the broker overrides."""

    def __init__(self):
        self.dac = {}

    def tca_write_pin(self, addr, port, pin, value):
        pass

    def tca_write_port(self, addr, port, value):
        pass

    def tca_write_all(self, addr, p0, p1):
        pass

    def tca_set_direction(self, addr, port, mask):
        pass

    def tca_set_all_inputs(self, addr):
        pass

    def tca_set_all_outputs(self, addr):
        pass

    def adc_read(self, idx, channel):
        return 7

    def adc_read_all(self, idx):
        return [7] * 8

    def adc_read_voltage(self, idx, channel):
        return 0.5

    def dac_set_voltage(self, idx, channel, volts):
        self.dac[(idx, channel)] = volts

    def ina_current(self, addr):
        return -1.0

    def _tick(self):
        pass

    def health(self):
        return {}


class _Monitor:
    def __init__(self):
        self.commands = []
        self.machine = None

    def execute(self, command):
        if command.startswith("mach set"):
            self.machine = command.split('"')[1]
        else:
            self.commands.append((self.machine, command))
        return "0x0"


def _backend(system):
    monitor = _Monitor()
    backend = make_backend(_Fake, monitor, System(REPO / "systems" / system).bench_config())
    return backend, monitor


def test_each_relay_powers_only_its_own_board():
    backend, monitor = _backend("ecu-ams.yaml")
    monitor.commands.clear()
    backend.tca_write_pin(0x20, 0, 1, True)          # slot 2 = AMS
    assert {m for m, _ in monitor.commands} == {"ams"}
    assert ("ams", "connector Connect sysbus.fdcan1_h7 can_acu") in monitor.commands
    assert backend.ina_current(0x41) == 0.12 and backend.ina_current(0x45) == 0.0


def test_power_on_wipes_the_backup_domain_without_vbat():
    """The MainLite has no VBAT: a power cut clears the RTC backup
    registers, where the AMS keeps its sticky ErrorLatch (BKP1R)."""
    backend, monitor = _backend("ecu-ams.yaml")
    monitor.commands.clear()
    backend.tca_write_pin(0x20, 0, 1, True)
    cmds = [c for _, c in monitor.commands]
    wipes = [c for c in cmds if c.startswith("sysbus WriteDoubleWord 0x580040")]
    assert len(wipes) == 20 and "sysbus WriteDoubleWord 0x58004054 0x0" in wipes
    assert cmds.index(wipes[-1]) < cmds.index("machine Reset")
    assert cmds.index("sysbus.backupSram ZeroAll") < cmds.index("machine Reset")


def test_virtual_seconds_parses_the_time_source_info():
    from vhil.broker import virtual_seconds
    info = ("Elapsed Virtual Time: 00:01:02.503125\nElapsed Host Time: 00:01:03.0\n"
            "Current load: 1.0")
    assert abs(virtual_seconds(info) - 62.503125) < 1e-9


def test_the_pacing_watch_flags_a_stalled_emulation(monkeypatch, caplog):
    """Virtual time standing still while the host clock runs is logged; a
    paced interval is not."""
    import threading
    import vhil.broker as broker
    wall = iter([0.0, 0.25, 0.50, 0.75])
    virt = iter([10.0, 10.25, 10.25, 10.50])        # stalls in the 2nd interval

    class Monitor:
        def execute(self, command):
            return f"Elapsed Virtual Time: 00:00:{next(virt):09.6f}\n"

    monkeypatch.setattr(broker.time, "sleep", lambda s: None)
    monkeypatch.setattr(broker.time, "monotonic", lambda: next(wall))
    with caplog.at_level("WARNING", logger="vhil.broker"):
        broker.watch_pacing(Monitor(), threading.Lock(), iterations=4)
    stalls = [r for r in caplog.records if "behind host time" in r.getMessage()]
    assert len(stalls) == 1 and "250 ms behind" in stalls[0].getMessage()


def test_power_on_connects_the_buses_before_the_reset():
    """#63: the firmware must never run unconnected after power-on. The
    board's machine is paused, the buses connected, the board reset, then
    the machine started (#125: no CPU halt)."""
    backend, monitor = _backend("ecu.yaml")
    monitor.commands.clear()
    backend.tca_write_pin(0x20, 0, 3, True)          # slot 4 = ECU
    cmds = [c for _, c in monitor.commands]
    connects = [i for i, c in enumerate(cmds) if c.startswith("connector Connect")]
    assert len(connects) == 3
    assert cmds.index("machine Pause") < min(connects)
    assert max(connects) < cmds.index("machine Reset") < cmds.index("machine Start")
    assert not any("IsHalted" in c for c in cmds)


def test_a_board_in_a_fault_handler_after_power_on_is_logged(caplog):
    """#125: half a second after power-on, a CPU parked in a fault handler is
    reported with its system-control fault registers."""
    import vhil.broker as broker

    class FaultMonitor(_Monitor):
        def execute(self, command):
            super().execute(command)
            if command == "cpu PC":
                return "0x80216BA"
            if command.startswith("sysbus FindSymbolAt"):
                return "HardFault_Handler"
            return "0x0"

    monitor = FaultMonitor()
    backend = make_backend(_Fake, monitor, System(REPO / "systems" / "ecu-ams.yaml").bench_config())
    backend._powered["ams"] = True
    with caplog.at_level("WARNING", logger="vhil.broker"):
        backend._check_boot("ams")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("HardFault_Handler" in m and "CFSR=" in m and "ICSR=" in m for m in msgs), msgs


def test_a_healthy_boot_logs_nothing(caplog):
    monitor = _Monitor()                     # FindSymbolAt answers "0x0": no handler
    backend = make_backend(_Fake, monitor, System(REPO / "systems" / "ecu-ams.yaml").bench_config())
    backend._powered["ams"] = True
    with caplog.at_level("WARNING", logger="vhil.broker"):
        backend._check_boot("ams")
    assert not caplog.records


# -- bench GPIO and ADC routes --------------------------------------------------

ROUTES = {
    "power": [{"machine": "ams", "relay": {"addr": 0x20, "port": 0, "pin": 1},
               "ina_addr": 0x41, "current_A": 0.12, "can": {}, "vbat": False}],
    "gpio_routes": [
        {"tca": 0x21, "port": 1, "pin": 0, "machine": "ams",
         "gpio_port": "sysbus.gpioPortF", "gpio_pin": 9},
        {"tca": 0x21, "port": 1, "pin": 1, "machine": "ams",
         "gpio_port": "sysbus.gpioPortF", "gpio_pin": 10}],
    "adc_routes": [
        {"adc": 2, "channel": 0, "machine": "ams", "gpio_port": "sysbus.gpioPortB", "gpio_pin": 4}],
}


class _LevelMonitor(_Monitor):
    """Answers a probe's Level with the level set for it."""

    def __init__(self, high=False):
        super().__init__()
        self.high = high

    def execute(self, command):
        super().execute(command)
        if " Level " in command:
            return "True" if self.high else "False"
        return ""


def _drives(monitor):
    return [c for _, c in monitor.commands if " Drive " in c]


def test_a_tca_pin_drives_its_gpio_only_as_an_output():
    """The TCA9555 drives a line only while its direction bit is 0; as an
    input the line floats to the board's pull-down (low)."""
    monitor = _Monitor()
    backend = make_backend(_Fake, monitor, ROUTES)
    backend.tca_write_pin(0x21, 1, 0, True)            # still an input: nothing driven
    assert _drives(monitor) == []
    backend.tca_set_direction(0x21, 1, 0xFC)           # P10, P11 outputs
    assert _drives(monitor) == ['vhil_gpio_ams Drive "sysbus.gpioPortF" 9 true']
    monitor.commands.clear()
    backend.tca_write_pin(0x21, 1, 0, True)            # unchanged: not driven again
    backend.tca_write_pin(0x21, 0, 0, True)            # another port: not routed
    assert _drives(monitor) == []
    backend.tca_write_port(0x21, 1, 0x02)
    assert _drives(monitor) == ['vhil_gpio_ams Drive "sysbus.gpioPortF" 9 false',
                                'vhil_gpio_ams Drive "sysbus.gpioPortF" 10 true']
    monitor.commands.clear()
    backend.tca_set_all_inputs(0x21)
    assert _drives(monitor) == ['vhil_gpio_ams Drive "sysbus.gpioPortF" 10 false']
    assert {m for m, c in monitor.commands if " Drive " in c} == {"ams"}


def test_a_routed_adc_channel_reads_the_pin_level_now():
    monitor = _LevelMonitor(high=True)
    backend = make_backend(_Fake, monitor, ROUTES)
    assert backend.adc_read_voltage(2, 0) == 0.0       # unpowered: drives nothing
    backend._powered["ams"] = True
    assert backend.adc_read_voltage(2, 0) == 3.3
    assert backend.adc_read(2, 0) == 4095
    assert backend.adc_read_all(2)[:2] == [4095, 7]
    assert ("ams", 'vhil_gpio_ams Level "sysbus.gpioPortB:4"') in monitor.commands
    monitor.high = False
    assert backend.adc_read_voltage(2, 0) == 0.0 and backend.adc_read(2, 0) == 0
    assert backend.adc_read_voltage(2, 1) == 0.5       # unrouted: the fake's
    assert backend.adc_read(1, 0) == 7


def test_probe_commands_watch_each_routed_output():
    from vhil.broker import probe_commands
    cmds = probe_commands(ROUTES)
    assert cmds[0].startswith("include ") and "VhilProbe.cs" in cmds[0]
    assert 'emulation CreateVhilGpioProbe "vhil_gpio_ams" "ams"' in cmds
    assert 'vhil_gpio_ams Watch "sysbus.gpioPortB" 4' in cmds
    assert probe_commands({"power": [], "dac_routes": []}) == []
