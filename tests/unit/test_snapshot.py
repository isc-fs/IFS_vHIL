"""vhil/snapshot.py against a tiny fake Sim: registers, FreeRTOS lists in a
fake RAM, the trace ring, and the failure paths (nothing raises)."""
import re
import struct

from vhil import snapshot

from tests.unit.test_elf import _elf

REGS = """------------------------------------------
| Name      | Index | Value              |
------------------------------------------
| R0        | 0     | 0x58020000         |
| SP / R13  | 13    | 0x24005580         |
| LR / R14  | 14    | 0x8020121          |
| PC / R15  | 15    | 0x8020104          |
| CPSR      | 25    | 0x1000003          |
| D5        | 47    | 0x4600000000000000 |
------------------------------------------"""

CURRENT, IDLE = 0x24001000, 0x24001100
DELAYED = 0x24000100


def _ram():
    mem = bytearray(0x4000)          # 0x24000000..0x24004000
    base = 0x24000000

    def put(addr, data):
        mem[addr - base:addr - base + len(data)] = data

    def u32(addr, v):
        put(addr, struct.pack("<I", v))

    def tcb(addr, name, prio, stack, fill):
        u32(addr + 16, addr)                       # xStateListItem.pvOwner
        u32(addr + 44, prio)
        u32(addr + 48, stack)
        put(addr + 52, name.encode() + b"\0")
        put(stack, b"\xA5" * fill + b"\x01")

    u32(0x24000000, CURRENT)                       # pxCurrentTCB
    tcb(CURRENT, "CanTask", 3, 0x24002000, 100)
    tcb(IDLE, "IDLE", 0, 0x24003000, 0)
    # xDelayedTaskList1: one item, IDLE's xStateListItem (TCB + 4)
    u32(DELAYED, 1)
    u32(DELAYED + 12, IDLE + 4)                    # xListEnd.pxNext
    u32(IDLE + 4 + 4, DELAYED + 8)                 # item.pxNext -> end marker
    u32(IDLE + 4 + 12, IDLE)                       # item.pvOwner
    return mem, base


class FakeSystem:
    id = "ecu"
    boards = {"ecu": None}


class FakeSim:
    def __init__(self, image, trace_blocks=0, broken=False):
        self.system = FakeSystem()
        self.firmware = {"ecu": image}
        self.trace_blocks = trace_blocks
        self.broken = broken
        self._mach = "ecu"
        self.last_activity = 0
        self.mem, self.base = _ram()
        self.commands = []

    def images_of(self, board):
        return [self.firmware[board]]

    def monitor(self, cmd, board=None):
        self.commands.append(cmd)
        if self.broken:
            raise RuntimeError("monitor gone")
        if cmd == "emulation GetTimeSourceInfo":
            return "Elapsed Virtual Time: 00:00:01.500000\nOther: x"
        if cmd == "cpu GetRegistersValues":
            return REGS
        if cmd.startswith("sysbus ReadDoubleWord"):
            return "0x00000000"
        m = re.match(r"sysbus ReadBytes 0x([0-9A-F]+) (\d+)", cmd)
        if m:
            a, n = int(m.group(1), 16) - self.base, int(m.group(2))
            if a < 0 or a + n > len(self.mem):
                return "[\n]"
            return "[\n" + ", ".join(f"0x{b:02X}" for b in self.mem[a:a + n]) + ", \n]"
        if cmd.startswith("vhil_trace_ecu Ring"):
            return "0x8020100 2\n0x8020200 1\n0x8020100 2\n0x8020200 1\n0x8020104 3\n"
        raise AssertionError(f"unexpected command {cmd}")


def _image(tmp_path):
    return _elf(tmp_path / "ECU08.elf", [
        ("main", 0x08020101, 0x40, 2), ("vTaskDelay", 0x08020201, 0x20, 2),
        ("pxCurrentTCB", 0x24000000, 4, 1), ("xDelayedTaskList1", DELAYED, 20, 1)])


def test_parsers():
    regs = snapshot.parse_registers(REGS)
    assert regs["SP"] == 0x24005580 and regs["PC"] == 0x8020104 and regs["D5"] == 0x46 << 56
    assert snapshot.parse_bytes("[\n0x0A, 0x00, 0xFF, \n]") == b"\x0a\x00\xff"
    assert snapshot.parse_time_us("Elapsed Virtual Time: 00:01:02.000250") == 62_000_250
    assert snapshot.parse_time_us("nothing") is None
    assert snapshot.exception_name(0) == "thread mode"
    assert snapshot.exception_name(3) == "HardFault"
    assert snapshot.exception_name(16 + 19) == "IRQ19"


def test_ram_bounds():
    assert snapshot.in_ram(0x24000000, 4)
    assert not snapshot.in_ram(0x2404FFFE, 4)
    assert not snapshot.in_ram(0x40000000)                # a peripheral
    assert not snapshot.in_ram(0x08020000)


def test_snapshot_of_a_board(tmp_path):
    sim = FakeSim(_image(tmp_path), trace_blocks=4096)
    [snap] = snapshot.take_all(sim)
    assert snap.errors == []
    assert snap.time_us == 1_500_000
    assert snap.current_task == "CanTask"
    assert [(t.name, t.state, t.priority, t.stack_free) for t in snap.tasks] == [
        ("CanTask", "running", 3, 100), ("IDLE", "blocked", 0, 0)]
    summary = snap.summary()
    assert "ecu @ 1.500000 s: PC 0x08020104 main+0x4 LR main+0x20 in HardFault task 'CanTask'" in summary
    assert "[main -> vTaskDelay] x 2" in summary
    text = snap.text()
    assert "CanTask" in text and "IDLE" in text and "100 B free" in text
    assert "0x08020200    1  vTaskDelay" in text
    assert "one per line" not in snap.text(full_ring=False)
    # Only reads: no RunFor, no writes, nothing outside RAM.
    assert not any(c.startswith(("emulation RunFor", "sysbus Write")) for c in sim.commands)


def test_a_broken_monitor_is_reported_not_raised(tmp_path):
    sim = FakeSim(_image(tmp_path), trace_blocks=16, broken=True)
    [snap] = snapshot.take_all(sim)
    assert snap.registers == {} and snap.tasks == []
    assert any("registers: RuntimeError: monitor gone" in e for e in snap.errors)
    assert "snapshot step(s) failed" in snap.summary()


def test_an_image_without_freertos(tmp_path):
    image = _elf(tmp_path / "CAN_BL.elf", [("main", 0x08000201, 0x40, 2)])
    [snap] = snapshot.take_all(FakeSim(image))
    assert snap.current_task is None
    assert any(e.startswith("FreeRTOS: LookupError") for e in snap.errors)


def test_an_unknown_tcb_layout_is_refused(tmp_path):
    sim = FakeSim(_image(tmp_path))
    off = CURRENT + 16 - sim.base
    sim.mem[off:off + 4] = b"\0\0\0\0"            # pvOwner no longer points back
    [snap] = snapshot.take_all(sim)
    assert snap.tasks == []
    assert any("unknown TCB layout" in e for e in snap.errors)


def test_a_dead_renode_is_not_queried(tmp_path):
    class Exited:
        returncode = -9

        def poll(self):
            return -9
    sim = FakeSim(_image(tmp_path))
    sim._proc = Exited()
    [snap] = snapshot.take_all(sim)
    assert snap.errors == ["Renode exited (-9)"] and sim.commands == []
