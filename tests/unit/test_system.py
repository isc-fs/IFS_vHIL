"""Host-only checks of the catalogue, the systems and the generator."""
import json
import shutil
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
    # The H73x ADC3 model is compiled once, before any machine loads its repl.
    assert s.index("include @") < s.index('mach create "ecu"')
    assert s.count("models/renode/Stm32H7Adc3.cs") == 1
    for controller, bus in (("fdcan1_h7", "can_inv"), ("fdcan2_h7", "can_acu"), ("fdcan3_h7", "can_dash")):
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
    assert carrier["can"] == {"sysbus.fdcan1_h7": "can_inv", "sysbus.fdcan2_h7": "can_acu",
                              "sysbus.fdcan3_h7": "can_dash"}
    assert {(r["dac"], r["channel"], r["adc_channel"]) for r in cfg["dac_routes"]} == \
        {(0, 0, 3), (0, 1, 7), (0, 2, 2)}


def test_ecu_ams_share_the_acu_bus_and_power_separately():
    """M3 (#11): one ACU bus, two carriers; each relay powers only its own
    board's CAN controllers (vhil/broker.py)."""
    system = System(REPO / "systems" / "ecu-ams.yaml")
    assert system.buses["can_acu"]["nodes"] == ["ecu.FDCAN2", "ams.FDCAN1"]
    cfg = system.bench_config()
    carriers = {c["machine"]: c for c in cfg["carriers"]}
    assert carriers["ecu"]["relay"]["pin"] == 3 and carriers["ecu"]["ina_addr"] == 0x45
    assert carriers["ams"]["relay"]["pin"] == 1 and carriers["ams"]["ina_addr"] == 0x41
    assert carriers["ams"]["can"] == {"sysbus.fdcan1_h7": "can_acu"}
    assert set(carriers["ecu"]["can"].values()) == {"can_inv", "can_acu", "can_dash"}
    assert {(r["machine"], r["dac"], r["channel"], r["adc_channel"]) for r in cfg["dac_routes"]
            if r["machine"] == "ams"} == {("ams", 3, 0, 3), ("ams", 3, 1, 7)}


def test_gaps_name_systems_that_exist():
    ids = {yaml.safe_load(p.read_text())["id"] for p in SYSTEMS}
    for gap in yaml.safe_load((REPO / "configs" / "gaps.yaml").read_text()) or []:
        assert set(gap.get("systems", [])) <= ids, gap["path"]


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
    assert s.count("models/renode/IsoSpi.cs") == 1        # shared by both models, once
    bridge = s.index("isospi: SPI.Ltc6820 @ spi1")
    assert "9 -> isospi@0" in s
    positions = [s.index(f"cells{i}: SPI.Ltc6811 @ isospi {i}") for i in range(10)]
    assert bridge < positions[0] and positions == sorted(positions)
    assert "sd: SD.SDCard @ sdmmc" in s


AMS_SYSTEMS = [p for p in SYSTEMS if "ams" in System(p).boards]


@pytest.mark.parametrize("path", AMS_SYSTEMS, ids=lambda p: p.name)
def test_every_ams_has_its_imu_on_i2c2(path):
    """The MLC's BMI088: accelerometer 0x18, gyroscope 0x68 on I2C2 (#59),
    after the I2C model its dies build on."""
    s = System(path).render_renode()
    assert "imu_acc: Sensors.Bmi088Accelerometer @ i2c2_h7 0x18" in s
    assert "imu_gyr: Sensors.Bmi088Gyroscope @ i2c2_h7 0x68" in s
    assert s.index("models/renode/Stm32H7I2c.cs") < s.index("models/renode/Bmi088.cs")


def test_ams_current_sensors_hold_their_zero_from_load():
    """The car's sensors at 0 A: SSA-2 legs at the 1.44 V common mode, the
    ACS758 at Vcc/2 (ams_config.hpp, current sensor calibration)."""
    s = System(REPO / "systems" / "ams.yaml").render_renode()
    for line in ("sysbus.adc3_h73x SetVoltage 1440000 3",    # PF7 = INP3
                 "sysbus.adc3_h73x SetVoltage 1440000 7",    # PF8 = INN3's pin
                 "sysbus.adc3_h73x SetVoltage 1650000 11"):  # PC1 = INP11
        assert line in s
    assert s.index(line) < s.index("macro reset")   # outside the board: not reset


@pytest.mark.parametrize("amps, p_v, n_v", [
    (100, 1.69, 1.19),        # +/-2.5 mV/A per leg
    (-100, 1.19, 1.69),
    (1000, 2.75, 0.13),       # clipped at +/-1.31 V per leg (2.62 V differential)
])
def test_ssa_2_legs_follow_the_current(tmp_path, amps, p_v, n_v):
    s = System(_ams(tmp_path, "  i: {model: ssa-2-250a, outputs: {out_p: ams.PF7, out_n: ams.PF8},"
                              f" params: {{current_A: {amps}}}}}\n"))
    levels = s.analog_levels("i")
    assert levels["ams.PF7"] == pytest.approx(p_v) and levels["ams.PF8"] == pytest.approx(n_v)


def test_port_signals_must_point_at_the_right_kind(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("kind: system\nid: t\nboards:\n  ecu: {board: mlc-carrier, firmware: ecu}\n"
                 "port:\n  step_ms: 10\n  signals:\n    apps1: {analog: ecu.PB5}\n")
    with pytest.raises(SystemError, match="is gpio, not analog"):
        System(p)
    p.write_text("kind: system\nid: t\nboards:\n  ecu: {board: mlc-carrier, firmware: ecu}\n"
                 "port:\n  step_ms: 10\n  signals:\n    x: {can_tx: {bus: nope, id: 1}}\n")
    with pytest.raises(SystemError, match="no bus 'nope'"):
        System(p)


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
     "  c: {model: ltc6811, attach: b, params: {bogus: 1}}\n", "unknown params"),    ("  i: {model: ssa-2-250a, outputs: {out_p: ams.PF7}}\n", "drives outputs"),
    ("  i: {model: acs758lcb-050b, outputs: {out: ams.FDCAN1}}\n", "not an analog input"),
    ("  imu: {model: bmi088}\n", "needs 'i2c'"),
    ("  imu: {model: bmi088, i2c: ams.SDMMC1}\n", "is sdmmc, not i2c"),
    ("  sd: {model: sd-card, sdmmc: ams.SDMMC1, i2c: ams.I2C2}\n", "takes no 'i2c'"),
])
def test_bad_devices_are_rejected_with_a_reason(tmp_path, devices, message):
    with pytest.raises(SystemError, match=message):
        System(_ams(tmp_path, devices))


def test_each_board_sets_its_image_inside_its_own_machine(tmp_path):
    """A Renode variable set while a machine is selected is local to it, so
    each $elf_<board> must follow its own `mach create`."""
    p = tmp_path / "s.yaml"
    p.write_text("kind: system\nid: t\nboards:\n"
                 "  ecu: {board: mlc-carrier, firmware: ecu}\n"
                 "  ams: {board: mlc-carrier, firmware: ams}\n"
                 "buses:\n  can_acu: {kind: can, nodes: [ecu.FDCAN2, ams.FDCAN1]}\n")
    lines = System(p).render_renode({"ecu": Path("/fw/ecu.elf"), "ams": Path("/fw/ams.elf")}).splitlines()
    for board in ("ecu", "ams"):
        assert lines.index(f"$elf_{board}=@/fw/{board}.elf") == lines.index(f'mach create "{board}"') + 1


def test_an_i2c_model_must_place_its_targets(tmp_path):
    """An I2C device is one or more targets at addresses: a model claiming
    the interface without them is refused."""
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    (catalog / "models" / "no-targets.yaml").write_text(
        "kind: model\nid: no-targets\ninterface: {i2c: true}\nbackend: renode\n"
        "renode: {type: Sensors.Nothing}\n")
    with pytest.raises(SystemError, match="an i2c model gives renode targets"):
        System(_ams(tmp_path, "  x: {model: no-targets, i2c: ams.I2C2}\n"), catalog=catalog)


# -- option bytes ---------------------------------------------------------------

def _wp_system(tmp_path, sectors):
    p = tmp_path / "wp.yaml"
    p.write_text("kind: system\nid: wp\nboards:\n"
                 f"  ecu: {{board: mlc-carrier, firmware: ecu, write_protect: {sectors}}}\n")
    return p


def test_write_protect_burns_the_option_bytes_once_before_boot(tmp_path):
    """A board's write_protect is option-byte state: set once after its
    platform loads, not in the reset macro, so it outlives every reset."""
    lines = System(_wp_system(tmp_path, [0, 2])).render_renode(
        {"ecu": Path("/fw/ecu.elf")}).splitlines()
    burn = "sysbus.flashController_h7 WriteProtectedSectors 0x05"
    assert lines.count(burn) == 1
    assert lines.index(burn) > next(i for i, line in enumerate(lines)
                                    if "LoadPlatformDescription @" in line)
    assert lines.index(burn) < lines.index("macro reset")
    assert "WriteProtectedSectors" not in System(_system(tmp_path, "")).render_renode()


def test_write_protect_needs_a_platform_with_option_bytes(tmp_path):
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    platform = catalog / "platforms" / "stm32h733.yaml"
    doc = yaml.safe_load(platform.read_text())
    del doc["renode"]["write_protect"]
    platform.write_text(yaml.safe_dump(doc))
    with pytest.raises(SystemError, match="no option bytes"):
        System(_wp_system(tmp_path, [0]), catalog=catalog)
