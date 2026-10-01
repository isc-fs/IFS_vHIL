"""Host-only checks of the catalogue, the systems and the generator."""
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from vhil.system import REPO, System, SystemError

DOCS = sorted((REPO / "catalog").glob("*/*.yaml")) + sorted((REPO / "systems").glob("*.yaml"))
SYSTEMS = sorted((REPO / "systems").glob("*.yaml"))


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_document_matches_the_schema(path):
    schema = json.loads((REPO / "schemas" / "vhil.schema.json").read_text())
    errors = list(Draft202012Validator(schema).iter_errors(yaml.safe_load(path.read_text())))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_every_system_resolves_and_renders(path):
    system = System(path)
    script = system.render_renode({b: Path(f"/fw/{b}.elf") for b in system.boards},
                                  socketcan=True)
    for name in system.boards:
        assert f'mach create "{name}"' in script
        assert f"$elf_{name}=@/fw/{name}.elf" in script
    for bus in system.buses:
        assert f'emulation CreateCANHub "{bus}"' in script


def test_ecu_system_renders_the_bench_setup():
    """systems/ecu.yaml must reproduce what the hand-written ecu.resc did."""
    s = System(REPO / "systems" / "ecu.yaml").render_renode(socketcan=True)
    assert 'emulation SetGlobalQuantum "0.0005"' in s
    assert "stm32h733.repl" in s
    assert "logLevel 3 sysbus.adc3" in s
    for controller, bus in (("fdcan1", "can_inv"), ("fdcan2", "can_acu"), ("fdcan3", "can_dash")):
        assert f"connector Connect sysbus.{controller} {bus}" in s
    assert "sysbus LoadELF $elf_ecu" in s
    assert "cpu VectorTableOffset 0x08020000" in s
    for bus, netdev in (("can_inv", "can0"), ("can_dash", "can1"), ("can_acu", "can2")):
        assert f'machine CreateSocketCANBridge "br_{bus}" "{netdev}"' in s
    assert "CreateSocketCANBridge" not in System(REPO / "systems" / "ecu.yaml").render_renode()


def test_ecu_bench_wiring():
    cfg = System(REPO / "systems" / "ecu.yaml").bench_config()
    (carrier,) = cfg["carriers"]
    assert carrier["machine"] == "ecu" and carrier["relay"] == {"addr": 0x20, "port": 0, "pin": 3}
    assert carrier["ina_addr"] == 0x45
    assert carrier["can"] == {"sysbus.fdcan1": "can_inv", "sysbus.fdcan2": "can_acu",
                              "sysbus.fdcan3": "can_dash"}
    assert {(r["dac"], r["channel"], r["adc_channel"]) for r in cfg["dac_routes"]} == \
        {(0, 0, 3), (0, 1, 7), (0, 2, 2)}


def _system(tmp_path, body):
    p = tmp_path / "s.yaml"
    p.write_text("kind: system\nid: t\nboards:\n  ecu: {board: mlc-carrier, firmware: ecu}\n" + body)
    return p


@pytest.mark.parametrize("body, message", [
    ("buses:\n  b: {kind: can, nodes: [ecu.FDCAN9]}\n", "no connector or pin 'FDCAN9'"),
    ("buses:\n  b: {kind: can, nodes: [ams.FDCAN1]}\n", "no board instance 'ams'"),
    ("buses:\n  b: {kind: can, nodes: [ecu.PF7]}\n", "is analog"),
    ("buses:\n  a: {kind: can, nodes: [ecu.FDCAN1]}\n  b: {kind: can, nodes: [ecu.FDCAN1]}\n",
     "on both"),
    ("bench:\n  dac_routes:\n    - {dac: 0, channel: 0, to: ecu.FDCAN1}\n", "not an analog input"),
])
def test_bad_systems_are_rejected_with_a_reason(tmp_path, body, message):
    with pytest.raises(SystemError, match=message):
        System(_system(tmp_path, body))


def test_unknown_catalogue_entries_are_rejected(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("kind: system\nid: t\nboards:\n  x: {board: no-such-board, firmware: ecu}\n")
    with pytest.raises(SystemError, match="no board 'no-such-board'"):
        System(p)


# -- devices ------------------------------------------------------------------

def test_ams_renders_the_isospi_chain_in_order():
    s = System(REPO / "systems" / "ams.yaml").render_renode()
    assert s.count("include @") == 1 and "models/renode/IsoSpi.cs" in s
    bridge = s.index("isospi: SPI.Ltc6820 @ spi1")
    assert "9 -> isospi@0" in s
    positions = [s.index(f"cells{i}: SPI.Ltc6811 @ isospi {i}") for i in range(10)]
    assert bridge < positions[0] and positions == sorted(positions)
    assert "sd: SD.SDCard @ sdmmc" in s


def _ams(tmp_path, devices):
    p = tmp_path / "s.yaml"
    p.write_text("kind: system\nid: t\nboards:\n  ams: {board: mlc-carrier, firmware: ams}\n"
                 "devices:\n" + devices)
    return p


@pytest.mark.parametrize("devices, message", [
    ("  b: {model: ltc6820, spi: ams.SPI1}\n", "needs 'cs'"),
    ("  b: {model: ltc6820, spi: ams.PB9, cs: ams.PB9}\n", "is gpio, not spi"),
    ("  c: {model: ltc6811}\n", "must attach to a device"),
    ("  sd: {model: sd-card, sdmmc: ams.SDMMC1}\n  c: {model: ltc6811, attach: sd}\n",
     "provides no isospi port"),
    ("  sd: {model: sd-card, sdmmc: ams.SDMMC1, count: 2}\n", "does not attach"),
    ("  sd: {model: sd-card, sdmmc: ams.SDMMC1, spi: ams.SPI1}\n", "takes no 'spi'"),
    ("  c: {model: ltc6811, attach: nope}\n", "must attach to a device"),
    ("  b: {model: ltc6820, spi: ams.SPI1, cs: ams.PB9}\n"
     "  c: {model: ltc6811, attach: b, params: {bogus: 1}}\n", "unknown params"),
])
def test_bad_devices_are_rejected_with_a_reason(tmp_path, devices, message):
    with pytest.raises(SystemError, match=message):
        System(_ams(tmp_path, devices))
