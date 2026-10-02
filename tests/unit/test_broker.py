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
    backend.tca_write_pin(0x20, 0, 1, True)          # K2 = MLC2 = AMS
    assert {m for m, _ in monitor.commands} == {"ams"}
    assert ("ams", "connector Connect sysbus.fdcan1_h7 can_acu") in monitor.commands
    assert backend.ina_current(0x41) == 0.12 and backend.ina_current(0x45) == 0.0


def test_power_on_wipes_the_backup_domain_without_vbat():
    """An MLC carrier has no VBAT: a power cut clears the RTC backup
    registers, where the AMS keeps its sticky ErrorLatch (BKP1R)."""
    backend, monitor = _backend("ecu-ams.yaml")
    monitor.commands.clear()
    backend.tca_write_pin(0x20, 0, 1, True)
    cmds = [c for _, c in monitor.commands]
    wipes = [c for c in cmds if c.startswith("sysbus WriteDoubleWord 0x580040")]
    assert len(wipes) == 20 and "sysbus WriteDoubleWord 0x58004054 0x0" in wipes
    assert cmds.index(wipes[-1]) < cmds.index("machine Reset")
