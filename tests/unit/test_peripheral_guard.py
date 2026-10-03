"""The peripheral guard reads Renode's log the way Renode writes it, on one
machine and on several."""
from vhil import peripheral_guard as guard

ONE = """\
[12:00:00.0001] [WARNING] sdmmc: Unhandled write to offset 0x2C. Unhandled bits: [0]
[12:00:00.0002] [WARNING] sysbus: [cpu: 0x803B2FE] (tag: 'CAN_CCU') ReadDoubleWord from non existing peripheral at 0x4000A810, returning 0x00000000.
[12:00:00.0003] [WARNING] sysbus: [cpu: 0x8000000] WriteDoubleWord to non existing peripheral at 0x40001000, value 0x0.
"""
# Real lines from an ecu-ams run: the source carries the machine.
MANY = """\
[21:32:27.8306] [WARNING] ams/spi1: Unhandled write to offset 0x8. Unhandled bits: [28-30] when writing value 0x70000007. Tags: MBR (0x7).
[21:32:27.8322] [WARNING] ams/dma1: Unhandled write to offset 0x8. Unhandled bits: [0-4] when writing value 0x3F.
[20:28:13.6682] [WARNING] ams/sysbus: [cpu: 0x803B2FE] (tag: 'CAN_CCU') ReadDoubleWord from non existing peripheral at 0x4000A810, returning 0x00000000.
[20:28:13.6683] [WARNING] ecu/fdcan1: Unhandled read from offset 0x108.
"""


def test_one_machine_lines_are_found():
    found = guard.scan(ONE)
    assert ("peripheral", "sdmmc", 0x2C, None) in found
    assert ("tag", "CAN_CCU", None, None) in found
    assert ("address", None, 0x40001000, None) in found


def test_machine_prefixed_lines_are_found():
    found = guard.scan(MANY)
    assert set(found) == {("peripheral", "spi1", 0x8, "ams"), ("peripheral", "dma1", 0x8, "ams"),
                          ("tag", "CAN_CCU", None, "ams"), ("peripheral", "fdcan1", 0x108, "ecu")}


def test_rules_apply_to_any_machine_unless_they_name_one():
    rules = [{"peripheral": "fdcan1", "offsets": [0x108]}, {"tag": "CAN_CCU"},
             {"peripheral": "spi1", "machine": "ecu"}]
    left = {k for k, _ in guard.unexplained(MANY, rules)}
    assert left == {("peripheral", "spi1", 0x8, "ams"), ("peripheral", "dma1", 0x8, "ams")}
    assert "ams: spi1 register offset 0x8" in guard.report(guard.unexplained(MANY, rules))
