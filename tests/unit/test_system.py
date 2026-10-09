"""Host-only checks of the catalogue, the systems and the generator."""
import json
import shutil
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from vhil.system import REPO, System, SystemError

# Catalogue entries and systems; catalog/pin-models/ holds vendored pin
# models, not catalogue documents (tests/unit/test_pin_model.py).
DOCS = (sorted(p for p in (REPO / "catalog").glob("*/*.yaml") if p.parent.name != "pin-models")
        + sorted((REPO / "systems").glob("*.yaml")))
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
        create = "CreateVhilCanBus" if system.arbitrated(bus) else "CreateCANHub"
        assert f'emulation {create} "{bus}"' in script


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_a_lockstep_run_of_several_boards_gives_each_its_own_time_source(path):
    """#209: in vhil.sim, several boards each get a time source of their own
    (models/renode/VhilMachine.cs), compiled before the first is created;
    one board, and the wall-clock bench, keep `mach create`."""
    system = System(path)
    lockstep = system.render_renode({b: Path(f"/fw/{b}.elf") for b in system.boards}, lockstep=True)
    bench = system.render_renode({b: Path(f"/fw/{b}.elf") for b in system.boards})
    assert "VhilMachine.cs" not in bench and "CreateVhilMachine" not in bench
    for name in system.boards:
        if len(system.boards) > 1:
            assert f'emulation CreateVhilMachine "{name}"\nmach set "{name}"' in lockstep
            assert lockstep.index("VhilMachine.cs") < lockstep.index(f'CreateVhilMachine "{name}"')
            assert "mach create" not in lockstep
        else:
            assert f'mach create "{name}"' in lockstep and "VhilMachine" not in lockstep


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
    # The provisioned flash, booted from the bootloader's reset vector: the
    # bootloader sets VTOR to the app when it jumps, not the script.
    assert "sysbus LoadBinary $flash_ecu 0x08000000" in s
    assert "cpu VectorTableOffset 0x08000000" in s
    assert "0x08020000" not in s
    for bus, netdev in (("can_inv", "can0"), ("can_dash", "can1"), ("can_acu", "can2")):
        assert f'machine CreateSocketCANBridge "br_{bus}" "{netdev}"' in s
    assert "CreateSocketCANBridge" not in System(REPO / "systems" / "ecu.yaml").render_renode()


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_every_reset_clears_basepri(path):
    """Each board's reset macro, which Renode runs on every reset (watchdog
    and software too, not only `machine Reset`), clears BASEPRI's stale mask
    (renode/renode#1021; catalog/platforms/stm32h733.yaml)."""
    s = System(path).render_renode()
    macros = s.split("macro reset")[1:]
    assert len(macros) == len(System(path).boards)
    for macro in macros:
        assert 'cpu SetRegister "BasePri" 0x0' in macro.split('"""')[1]


def test_ecu_bench_wiring():
    cfg = System(REPO / "systems" / "ecu.yaml").bench_config()
    (entry,) = cfg["power"]
    assert entry["machine"] == "ecu" and entry["relay"] == {"addr": 0x20, "port": 0, "pin": 3}
    assert entry["ina_addr"] == 0x45
    assert entry["can"] == {"sysbus.fdcan1_h7": "can_inv", "sysbus.fdcan2_h7": "can_acu",
                              "sysbus.fdcan3_h7": "can_dash"}
    assert {(r["dac"], r["channel"], r["adc_channel"]) for r in cfg["dac_routes"]} == \
        {(0, 0, 3), (0, 1, 7), (0, 2, 2)}


def test_ecu_ams_share_the_acu_bus_and_power_separately():
    """M3 (#11): one ACU bus, two boards; each relay powers only its own
    board's CAN controllers (vhil/broker.py)."""
    system = System(REPO / "systems" / "ecu-ams.yaml")
    assert system.buses["can_acu"]["nodes"] == ["ecu.FDCAN2", "ams.FDCAN1"]
    cfg = system.bench_config()
    power = {c["machine"]: c for c in cfg["power"]}
    assert power["ecu"]["relay"]["pin"] == 3 and power["ecu"]["ina_addr"] == 0x45
    assert power["ams"]["relay"]["pin"] == 1 and power["ams"]["ina_addr"] == 0x41
    assert power["ams"]["can"] == {"sysbus.fdcan1_h7": "can_acu"}
    assert set(power["ecu"]["can"].values()) == {"can_inv", "can_acu", "can_dash"}
    assert {(r["machine"], r["dac"], r["channel"], r["adc_channel"]) for r in cfg["dac_routes"]
            if r["machine"] == "ams"} == {("ams", 3, 0, 3), ("ams", 3, 1, 7)}


def test_gpio_and_adc_routes_resolve_to_the_pins(tmp_path):
    cfg = System(_system(tmp_path, "bench:\n  gpio_routes:\n"
                         "    - {tca: 0x22, port: 1, pin: 0, to: ecu.PB5}\n"
                         "  adc_routes:\n    - {adc: 0, channel: 0, from: ecu.PB4}\n")).bench_config()
    assert cfg["gpio_routes"] == [{"tca": 0x22, "port": 1, "pin": 0, "machine": "ecu",
                                   "gpio_port": "sysbus.gpioPortB", "gpio_pin": 5}]
    assert cfg["adc_routes"] == [{"adc": 0, "channel": 0, "machine": "ecu",
                                  "gpio_port": "sysbus.gpioPortB", "gpio_pin": 4}]


def test_gaps_name_systems_that_exist():
    ids = {yaml.safe_load(p.read_text())["id"] for p in SYSTEMS}
    for gap in yaml.safe_load((REPO / "configs" / "gaps.yaml").read_text()) or []:
        assert set(gap.get("systems", [])) <= ids, gap["path"]


def test_gaps_replaced_by_native_tests_that_exist():
    """Every replaced_by names a test in tests/sim: a top-level test function
    of that file, and, with a [param id], one of its parametrize ids."""
    import ast
    import re

    from vhil.pytest_plugin import gap_reason

    gaps = yaml.safe_load((REPO / "configs" / "gaps.yaml").read_text()) or []
    replaced = [g for g in gaps if "replaced_by" in g]
    assert replaced, "no gap names a native replacement"
    for gap in replaced:
        assert gap["replaced_by"] and gap["why"], gap["path"]
        for ref in gap["replaced_by"]:
            m = re.fullmatch(r"(tests/sim/test_\w+\.py)::(test_\w+)(?:\[([\w.-]+)\])?", ref)
            assert m, f"{gap['path']}: malformed {ref}"
            path, name, param = m.groups()
            source = (REPO / path).read_text()
            tests = {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}
            assert name in tests, f"{gap['path']}: no {name} in {path}"
            if param is not None:
                marks = ast.get_source_segment(source, tests[name].decorator_list[0]) \
                    if tests[name].decorator_list else ""
                assert f'"{param}"' in marks, f"{gap['path']}: {name} has no param id {param}"
        assert all(r in gap_reason(gap) for r in gap["replaced_by"])


def _system(tmp_path, body):
    p = tmp_path / "s.yaml"
    p.write_text("kind: system\nid: t\nboards:\n  ecu: {board: mainlite, role: ecu}\n" + body)
    return p


@pytest.mark.parametrize("body, message", [
    ("buses:\n  b: {kind: can, nodes: [ecu.FDCAN9]}\n", "no connector or pin 'FDCAN9'"),
    ("buses:\n  b: {kind: can, nodes: [ams.FDCAN1]}\n", "no board instance 'ams'"),
    ("buses:\n  b: {kind: can, nodes: [ecu.PF7]}\n", "is analog"),
    ("buses:\n  a: {kind: can, nodes: [ecu.FDCAN1]}\n  b: {kind: can, nodes: [ecu.FDCAN1]}\n",
     "on both"),
    ("bench:\n  dac_routes:\n    - {dac: 0, channel: 0, to: ecu.FDCAN1}\n", "not an analog input"),
    ("bench:\n  gpio_routes:\n    - {tca: 0x22, port: 1, pin: 0, to: ecu.PF7}\n",
     "GPIO route to 'ecu.PF7': analog, not a GPIO"),
    ("bench:\n  gpio_routes:\n    - {tca: 0x22, port: 1, pin: 0, to: ecu.PB5}\n"
     "    - {tca: 0x22, port: 1, pin: 0, to: ecu.PB4}\n", "routed twice"),
    ("bench:\n  power:\n    - {board: ecu, relay: {addr: 0x20, port: 0, pin: 3}, ina_addr: 0x45,"
     " current_A: 0.1}\n  gpio_routes:\n    - {tca: 0x20, port: 0, pin: 3, to: ecu.PB5}\n",
     "power relay"),
    ("bench:\n  gpio_routes:\n    - {tca: 0x50, port: 1, pin: 0, to: ecu.PB5}\n", "not valid"),
    ("bench:\n  gpio_routes:\n    - {tca: 0x22, port: 2, pin: 0, to: ecu.PB5}\n", "not valid"),
    ("bench:\n  adc_routes:\n    - {adc: 0, channel: 0, from: ecu.FDCAN1}\n",
     "ADC route from 'ecu.FDCAN1': can, not a GPIO"),
    ("bench:\n  adc_routes:\n    - {adc: 0, channel: 0, from: ecu.PB4}\n"
     "    - {adc: 0, channel: 0, from: ecu.PB5}\n", "routed twice"),
    ("bench:\n  adc_routes:\n    - {adc: 0, channel: 8, from: ecu.PB4}\n", "not valid"),
    ("bench:\n  cell_stimulus: {device: isospi, cell_mV: 3750, temp_dC: 250}\n",
     "not an isoSPI bridge"),
    ("bench:\n  cell_stimulus: {device: isospi, cell_mV: 9000, temp_dC: 250}\n", "not valid"),
])
def test_bad_systems_are_rejected_with_a_reason(tmp_path, body, message):
    with pytest.raises(SystemError, match=message):
        System(_system(tmp_path, body))


def test_an_arbitrated_bus_renders_the_bus_model_after_the_fdcan(tmp_path):
    """#174: every bus is VhilCanBus unless it says `arbitration: false`,
    compiled once after the platform's FDCAN (it builds on
    IVhilCanController) and created before any controller connects to it;
    an opted-out bus stays Renode's hub."""
    s = System(_system(tmp_path, "buses:\n  a: {kind: can, nodes: [ecu.FDCAN1], arbitration: true}\n"
                                 "  b: {kind: can, nodes: [ecu.FDCAN2]}\n"
                                 "  c: {kind: can, nodes: [ecu.FDCAN3], arbitration: false}\n"))
    assert [s.arbitrated(b) for b in "abc"] == [True, True, False]
    text = s.render_renode()
    assert 'emulation CreateCANHub "a"' not in text and 'emulation CreateCANHub "b"' not in text
    assert 'emulation CreateCANHub "c"' in text
    assert text.count("models/renode/VhilCanBus.cs") == 1
    assert (text.index("models/renode/Stm32H7Fdcan.cs") < text.index("models/renode/VhilCanBus.cs")
            < text.index('emulation CreateVhilCanBus "a"') < text.index("connector Connect"))
    assert text.count("CreateVhilCanBus") == 2


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_every_bus_of_a_committed_system_is_arbitrated(path):
    """Arbitration is the default (#174): a committed system's script creates
    the bus model for each of its buses and no Renode hub, unless a bus
    opts out with `arbitration: false`. This changed every rendered script:
    `emulation CreateCANHub` per bus became `include VhilCanBus.cs` and
    `emulation CreateVhilCanBus` per bus, after the platform's models."""
    system = System(path)
    text = system.render_renode(socketcan=True)
    for bus in system.buses:
        hub = system.buses[bus].get("arbitration") is False
        assert (f'emulation CreateCANHub "{bus}"' in text) == hub
        assert (f'emulation CreateVhilCanBus "{bus}"' in text) == (not hub)
    assert ("models/renode/VhilCanBus.cs" in text) == any(map(system.arbitrated, system.buses))


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_render_can_hub_puts_renodes_hub_on_every_bus(tmp_path, path):
    """`render --can-hub` is for renode-test's CAN Tester keywords (the smoke
    suites), which attach only to Renode's CANHub."""
    from vhil.system import main
    out = tmp_path / "s.resc"
    assert main(["render", str(path), "--can-hub", "-o", str(out)]) == 0
    text = out.read_text()
    assert "VhilCanBus" not in text
    for bus in System(path).buses:
        assert f'emulation CreateCANHub "{bus}"' in text


@pytest.mark.parametrize("value", ["yes", 1, None, "true"])
def test_arbitration_is_a_boolean(tmp_path, value):
    body = "buses:\n  a: {kind: can, nodes: [ecu.FDCAN1], arbitration: %s}\n" % (
        "null" if value is None else repr(value) if isinstance(value, str) else value)
    with pytest.raises(SystemError, match="arbitration must be true or false|not valid"):
        System(_system(tmp_path, body))


def test_a_firmware_can_contract_must_name_a_can_connector_of_its_board(tmp_path):
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    fw = catalog / "firmware" / "ecu.yaml"
    fw.write_text(fw.read_text().replace("contract: [FDCAN2]", "contract: [FDCAN9]"))
    with pytest.raises(SystemError, match="CAN contract rides FDCAN9, which mainlite lacks"):
        System(REPO / "systems" / "ecu.yaml", catalog)
    fw.write_text(fw.read_text().replace("contract: [FDCAN9]", "contract: []"))
    with pytest.raises(SystemError, match="'contract': \\[\\]"):   # schema: minItems 1
        System(REPO / "systems" / "ecu.yaml", catalog)


def test_unknown_catalogue_entries_are_rejected(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("kind: system\nid: t\nboards:\n  x: {board: no-such-board, firmware: ecu}\n")
    with pytest.raises(SystemError, match="no board 'no-such-board'"):
        System(p)


# -- devices ------------------------------------------------------------------

# -- GPIO devices: the ECU's nRF24L01+ (#193) ------------------------------------

RADIO = ("  radio: {model: nrf24l01p, pins: {csn: ecu.PB0, sck: ecu.PA5, mosi: ecu.PA7, "
         "miso: ecu.PA6, ce: ecu.PC5, irq: ecu.PC4}}\n")


def test_a_gpio_device_renders_its_pins():
    """The pins the model drives come from its GPIO properties, the ones the
    MCU drives go from each port's pin to the model's input
    (catalog/models/nrf24l01p.yaml gpio_in / gpio_out)."""
    system = System(REPO / "systems" / "ecu.yaml")
    assert system.radios() == [("radio", "ecu")]
    block = system.render_renode().split("# device radio: nrf24l01p\n")[1].split('"""')[1]
    assert block == ("\nradio: Wireless.Nrf24l01p @ sysbus\n"
                     "    Miso -> gpioPortA@6\n"
                     "    Irq -> gpioPortC@4\n\n"
                     "gpioPortB:\n    0 -> radio@0\n\n"
                     "gpioPortA:\n    5 -> radio@1\n    7 -> radio@2\n\n"
                     "gpioPortC:\n    5 -> radio@3\n")
    assert "include @" + str(REPO / "models/renode/Nrf24l01p.cs") in system.render_renode()


@pytest.mark.parametrize("name", ["ecu.yaml", "ecu-ams.yaml"])
def test_the_radio_changes_the_ecu_scripts_only_by_itself(tmp_path, name):
    """systems/ecu.yaml and ecu-ams.yaml render as they did before the radio
    (#193), plus its source and its device block, and nothing else."""
    import difflib

    path = REPO / "systems" / name
    doc = yaml.safe_load(path.read_text())
    del doc["devices"]["radio"]
    if not doc["devices"]:
        del doc["devices"]
    before = tmp_path / name
    before.write_text(yaml.safe_dump(doc, sort_keys=False))
    old = System(before).render_renode().replace(f"from {name}", "from X").splitlines()
    new = System(path).render_renode().replace(f"from {name}", "from X").splitlines()
    added = [line for line in difflib.ndiff(old, new) if line.startswith(("+ ", "- "))]
    assert all(line.startswith("+ ") for line in added), added
    assert [line[2:] for line in added] == [
        f"include @{REPO / 'models/renode/Nrf24l01p.cs'}",
        "# device radio: nrf24l01p", 'machine LoadPlatformDescriptionFromString """',
        "radio: Wireless.Nrf24l01p @ sysbus", "    Miso -> gpioPortA@6", "    Irq -> gpioPortC@4",
        "", "gpioPortB:", "    0 -> radio@0", "", "gpioPortA:", "    5 -> radio@1",
        "    7 -> radio@2", "", "gpioPortC:", "    5 -> radio@3", '"""']


@pytest.mark.parametrize("pins, message", [
    (RADIO.replace("ce: ecu.PC5, ", ""), "wires pins .* got"),
    (RADIO.replace("ecu.PA5", "ecu.PF7"), "pin sck to 'ecu.PF7', which is analog, not a GPIO"),
    (RADIO.replace("ecu.PA5", "ecu.PA7"), "two of its pins on one board GPIO"),
    (RADIO.replace("ecu.PA5", "ecu.PA9"), "PA9"),
])
def test_a_gpio_device_needs_each_pin_on_its_own_gpio(tmp_path, pins, message):
    with pytest.raises(SystemError, match=message):
        System(_system(tmp_path, "devices:\n" + pins))


def test_ams_renders_the_isospi_chain_in_order():
    s = System(REPO / "systems" / "ams.yaml").render_renode()
    assert s.count("models/renode/IsoSpi.cs") == 1        # shared by both models, once
    bridge = s.index("isospi: SPI.Ltc6820 @ spi1")
    assert "9 -> isospi@0" in s
    positions = [s.index(f"cells{i}: SPI.Ltc6811 @ isospi {i}") for i in range(10)]
    assert bridge < positions[0] and positions == sorted(positions)
    assert "sd: SD.VhilSDCard @ sdmmc" in s
    assert s.count("models/renode/Stm32H7Sdmmc.cs") == 1     # platform and card, once


AMS_SYSTEMS = [p for p in SYSTEMS if "ams" in System(p).boards]


@pytest.mark.parametrize("path", AMS_SYSTEMS, ids=lambda p: p.name)
def test_every_ams_has_its_imu_on_i2c2(path):
    """The MainLite's BMI088: accelerometer 0x18, gyroscope 0x68 on I2C2 (#59),
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
    p.write_text("kind: system\nid: t\nboards:\n  ecu: {board: mainlite, role: ecu}\n"
                 "port:\n  step_ms: 10\n  signals:\n    apps1: {analog: ecu.PB5}\n")
    with pytest.raises(SystemError, match="is gpio, not analog"):
        System(p)
    p.write_text("kind: system\nid: t\nboards:\n  ecu: {board: mainlite, role: ecu}\n"
                 "port:\n  step_ms: 10\n  signals:\n    x: {can_tx: {bus: nope, id: 1}}\n")
    with pytest.raises(SystemError, match="no bus 'nope'"):
        System(p)


def _ams(tmp_path, devices):
    p = tmp_path / "s.yaml"
    p.write_text("kind: system\nid: t\nboards:\n  ams: {board: mainlite, role: ams}\n"
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
                 "  ecu: {board: mainlite, role: ecu}\n"
                 "  ams: {board: mainlite, role: ams}\n"
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
                 f"  ecu: {{board: mainlite, role: ecu, write_protect: {sectors}}}\n")
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


# -- per-system firmware refs (#116) ------------------------------------------
def _clones(monkeypatch, system, refs=None):
    """The refs build_firmware clones each image at, without cloning."""
    import vhil.system as vs
    cloned = {}

    def run(cmd, **kw):
        if cmd[:2] == ["git", "clone"]:
            cloned[Path(cmd[-1]).name] = cmd[cmd.index("-b") + 1]
    monkeypatch.setattr(vs.subprocess, "run", run)
    system.build_firmware(Path("/nonexistent"), refs, log=lambda m: None)
    return cloned


def test_a_board_without_firmware_ref_builds_the_catalogue_ref(tmp_path, monkeypatch):
    s = System(_system(tmp_path, ""))
    assert s.boards["ecu"].ref() == "dev"
    assert _clones(monkeypatch, s) == {"ecu@dev": "dev", "can-bootloader@v1.7.0": "v1.7.0"}


def test_firmware_ref_overrides_the_catalogue_and_build_ref_overrides_both(tmp_path, monkeypatch):
    p = tmp_path / "r.yaml"
    p.write_text("kind: system\nid: r\nboards:\n"
                 "  ecu: {board: mainlite, role: ecu, firmware_ref: feat/x,"
                 " bootloader_ref: v1.6.2}\n")
    s = System(p)
    assert s.boards["ecu"].ref() == "feat/x" and s.boards["ecu"].ref("bootloader") == "v1.6.2"
    assert _clones(monkeypatch, s) == {"ecu@feat_x": "feat/x", "can-bootloader@v1.6.2": "v1.6.2"}
    assert _clones(monkeypatch, s, {"ecu": "dev"})["ecu@dev"] == "dev"


def _catalog_with(tmp_path, old, new):
    """A copy of the catalogue whose mainlite has `old` replaced by `new`."""
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    path = catalog / "boards" / "mainlite.yaml"
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))
    return catalog


def test_a_bootloader_ref_needs_a_board_that_carries_one(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("kind: system\nid: r\nboards:\n"
                 "  ecu: {board: mainlite, role: ecu, bootloader_ref: v1.6.2}\n")
    System(p)                                   # mainlite carries one
    with pytest.raises(SystemError, match="bootloader_ref needs a bootloader"):
        System(p, _catalog_with(tmp_path, "bootloader: can-bootloader\n", ""))


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_every_mainlite_boots_through_the_can_bootloader(path):
    """Every MainLite carries the CAN bootloader and is placed in a role, which
    gives its node ID and flash bus (catalog/boards/mainlite.yaml; owner,
    2026-10-05; PROVISIONING.md step 1.3). No system boots its app directly."""
    system = System(path)
    roles = {"ecu": (1, "FDCAN2"), "ams": (2, "FDCAN1"), "udv": (3, "FDCAN2")}
    for b in system.boards.values():
        assert b.bootloader["id"] == "can-bootloader"
        assert f"{b.name}.bootloader" in system.images()
        assert (b.node_id, b.flash_bus) == roles[b.role]
        assert system.flash_bus(b.name) == "can_acu"
    script = system.render_renode()
    assert "LoadELF" not in script and "0x08020000" not in script


def test_the_mainlite_roles_are_the_cars():
    roles = yaml.safe_load((REPO / "catalog" / "boards" / "mainlite.yaml").read_text())["roles"]
    assert {r: (v["node_id"], v["flash_bus"], v["firmware"]) for r, v in roles.items()} == {
        "ecu": (1, "FDCAN2", "ecu"), "ams": (2, "FDCAN1", "ams"), "udv": (3, "FDCAN2", "udv")}


# The pins each backplane routes, with the car signal each carries
# (docs/backplanes/{ecu,ams,udv}.md, pin map tables). Display only.
PIN_LABELS = {
    "ecu": {"FDCAN1": "CAN_INV", "FDCAN2": "CAN_ACU", "FDCAN3": "CAN_DASH", "SPI1": "NRF24",
            "PB4": "RTDS", "PB5": "START", "PF7": "S_BRAKE", "PF8": "APPS_1", "PF9": "APPS_2",
            "PB9": "SPARE_J3", "PC1": "SPARE_J3", "PB6": "DISCHARGE", "PD5": "S_TEMP_REFRI",
            "USART10": "GPS", "PB0": "NRF24_CS", "PC5": "NRF24_CE", "PC4": "NRF24_IRQ",
            "PA5": "NRF24_SCK", "PA6": "NRF24_MISO", "PA7": "NRF24_MOSI", "PB7": "SPARE_J3", "PB8": "SPARE_J3", "PF10": "SPARE_J3", "PC0": "SPARE_J3",
            "PC2_C": "SPARE_J3"},
    "ams": {"FDCAN1": "CAN_ACU", "SPI1": "LTC6820", "PB9": "LTC6820_CS", "PB4": "AMS_OK",
            "PB5": "AIR_P", "PF7": "S_CURRENT_P", "PF8": "S_CURRENT_N", "PF9": "TSMS",
            "PC1": "S_CURRENT_DCDC", "PB6": "AIR_N", "PB7": "PRECHARGE", "PF10": "RST_PIL",
            "PC0": "S_TEMP_DCDC", "PB8": "SPARE", "PB0": "SPARE", "PC2_C": "SPARE"},
    "udv": {"FDCAN1": "CAN_DV", "FDCAN2": "CAN_ACU", "PB4": "EBS_VALVE1", "PB5": "EBS_VALVE2",
            "PB9": "ASSI_BLUE", "PF7": "EBS_PRES1", "PF8": "EBS_PRES2", "PF9": "EBS_24V",
            "PC1": "DEBUG_LED2", "PB6": "DEBUG_LED1", "PB7": "SDC_CTRL", "PB8": "ASSI_YELLOW",
            "PF10": "SDC_SENSE", "PC0": "RES_IN", "PC2_C": "DEBUG_LED3"},
}


def test_each_role_labels_the_pins_its_backplane_routes():
    board = yaml.safe_load((REPO / "catalog" / "boards" / "mainlite.yaml").read_text())
    for role, labels in PIN_LABELS.items():
        assert board["roles"][role]["pins"] == labels, role
        assert board["roles"][role]["backplane"]["doc"] == f"docs/backplanes/{role}.md"
        assert (REPO / board["roles"][role]["backplane"]["doc"]).is_file()
    assert board["onboard"] == {"SDMMC1": "microSD", "I2C2": "BMI088"}


def test_the_role_sets_the_firmware(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("kind: system\nid: r\nboards:\n  a: {board: mainlite, role: ams}\n"
                 "  b: {board: mainlite, role: ecu}\n")
    s = System(p)
    assert s.boards["a"].firmware["id"] == "ams" and s.boards["b"].firmware["id"] == "ecu"


@pytest.mark.parametrize("firmware", ["ecu", "ams"])
def test_a_system_names_no_firmware_for_a_board_in_a_role(tmp_path, firmware):
    """Even one that agrees: the role is the only place it is said."""
    p = tmp_path / "r.yaml"
    p.write_text(f"kind: system\nid: r\nboards:\n  a: {{board: mainlite, role: ecu, "
                 f"firmware: {firmware}}}\n")
    with pytest.raises(SystemError, match="the ecu role sets the firmware \\(ecu\\)"):
        System(p)


def test_a_role_without_catalogue_firmware_is_refused_by_name(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("kind: system\nid: r\nboards:\n  dv: {board: mainlite, role: udv}\n")
    with pytest.raises(SystemError, match="role udv has no firmware in the catalogue yet"):
        System(p)


def test_a_board_without_roles_names_its_firmware(tmp_path):
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    board = yaml.safe_load((catalog / "boards" / "mainlite.yaml").read_text())
    for key in ("roles", "bootloader"):
        del board[key]
    board["id"] = "plain"
    (catalog / "boards" / "plain.yaml").write_text(yaml.safe_dump(board))
    p = tmp_path / "r.yaml"
    p.write_text("kind: system\nid: r\nboards:\n  a: {board: plain, firmware: ams}\n")
    assert System(p, catalog).boards["a"].firmware["id"] == "ams"
    p.write_text("kind: system\nid: r\nboards:\n  a: {board: plain}\n")
    with pytest.raises(SystemError, match="has no roles, so it needs a firmware"):
        System(p, catalog)


@pytest.mark.parametrize("path", SYSTEMS, ids=lambda p: p.name)
def test_the_systems_wire_only_routed_pins(path):
    assert System(path).warnings == []


def test_an_unrouted_pin_is_a_warning_not_an_error(tmp_path, capsys):
    from vhil.system import main
    p = _ams(tmp_path, "  i: {model: acs758lcb-050b, outputs: {out: ams.PF8}}\n")
    p.write_text(p.read_text() + "buses:\n  x: {kind: can, nodes: [ams.FDCAN3]}\n"
                 "  y: {kind: can, nodes: [ams.FDCAN2]}\n")
    s = System(p)
    assert s.warnings == [
        "ams: FDCAN3 is not connected on the AMS backplane (docs/backplanes/ams.md)",
        "ams: FDCAN2 is not connected on the AMS backplane (docs/backplanes/ams.md)"]
    assert main(["validate", str(p)]) == 0
    out = capsys.readouterr()
    assert ": OK (ams)" in out.out
    assert "warning: " in out.err and "FDCAN3 is not connected on the AMS backplane" in out.err


def test_onboard_connectors_are_routed_in_every_role(tmp_path):
    s = System(_ams(tmp_path, "  sd: {model: sd-card, sdmmc: ams.SDMMC1}\n"
                              "  imu: {model: bmi088, i2c: ams.I2C2}\n"))
    assert s.warnings == []


def test_a_role_rekinds_an_analog_pin_as_gpio(tmp_path):
    """On the AMS backplane PF9 is TSMS, a digital input: a GPIO there, an
    analog input on the ECU (APPS_2)."""
    p = tmp_path / "r.yaml"
    p.write_text("kind: system\nid: r\nboards:\n  ams: {board: mainlite, role: ams}\n"
                 "  ecu: {board: mainlite, role: ecu}\n"
                 "port:\n  step_ms: 10\n  signals:\n    tsms: {gpio_in: ams.PF9}\n"
                 "    apps2: {analog: ecu.PF9}\n")
    s = System(p)
    assert s.resolve("ams.PF9")[1:] == ("gpio", {"port": "sysbus.gpioPortF", "pin": 9})
    assert s.resolve("ecu.PF9")[1] == "analog"
    p.write_text("kind: system\nid: r\nboards:\n  ams: {board: mainlite, role: ams}\n"
                 "devices:\n  i: {model: acs758lcb-050b, outputs: {out: ams.PF9}}\n")
    with pytest.raises(SystemError, match="which is gpio, not an analog input"):
        System(p)


@pytest.mark.parametrize("old, new, message", [
    # The schema refuses a label, a firmware id or a page that isn't one
    # (test_injection checks System() refuses them without it); System()
    # what the schema can't see.
    ("      PF8: APPS_1 ", "      PF8: 'APPS 1' ", "not valid under any"),
    ("  SDMMC1: microSD", "  SDMMC1: 'micro\"SD'", "not valid under any"),
    ("    firmware: udv\n", "    firmware: ../udv\n", "not valid under any"),
    ("doc: docs/backplanes/udv.md", "doc: ../../etc/passwd", "not valid under any"),
    ("      PF8: APPS_1 ", "      PX9: APPS_1 ", "role ecu labels 'PX9', which its pin model "
                                                 "v1.0 lacks"),
    ("      PF9: {port: sysbus.gpioPortF, pin: 9}", "      PB4: {port: sysbus.gpioPortB, pin: 4}",
     "makes 'PB4' a GPIO, but the board models no analog input"),
])
def test_the_roles_table_is_checked(tmp_path, old, new, message):
    with pytest.raises(SystemError, match=message):
        System(REPO / "systems" / "ecu.yaml", _catalog_with(tmp_path, old, new))


def test_built_images_name_the_source_dir_by_ref(tmp_path):
    from vhil.system import built_images, image_path
    fw = {"id": "ecu", "build": {"elf": "build/ECU08.elf"}}
    elf = image_path(tmp_path, fw, "feat/x")
    assert elf == tmp_path / "ecu@feat_x" / "build" / "ECU08.elf"
    assert built_images(tmp_path) == {}
    (tmp_path / "built.txt").write_text(f"ecu={elf}\nnot a line\n")
    assert built_images(tmp_path) == {elf.resolve(): "ecu"}


@pytest.mark.parametrize("entry, message", [
    ("{board: mainlite}", "needs a role \\(ecu, ams, udv\\)"),
    ("{board: mainlite, role: dash}", "'role': 'dash'"),
])
def test_a_mainlite_needs_one_of_its_roles(tmp_path, entry, message):
    p = tmp_path / "r.yaml"
    p.write_text(f"kind: system\nid: r\nboards:\n  ecu: {entry}\n")
    with pytest.raises(SystemError, match=message):
        System(p)


def test_node_id_is_the_roles_not_a_system_field(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("kind: system\nid: r\nboards:\n"
                 "  ecu: {board: mainlite, role: ecu, node_id: 5}\n")
    with pytest.raises(SystemError, match="node_id"):        # schema: no such field
        System(p)


def test_a_roles_flash_bus_must_be_a_can_connector(tmp_path):
    catalog = _catalog_with(tmp_path, "    flash_bus: FDCAN1\n", "    flash_bus: PB4\n")
    with pytest.raises(SystemError, match="role ams's flash_bus 'PB4' is not one of its CAN"):
        System(REPO / "systems" / "ecu.yaml", catalog)


def test_an_unprovisioned_board_leaves_the_seed_erased():
    from vhil import flash_image as fi
    image = fi.build(b"\x00" * 64, b"\x01" * 64, None)
    assert image[fi.SEED_ADDR - fi.FLASH_BASE:][:32] == b"\xFF" * 32


def test_an_empty_firmware_ref_is_a_schema_error(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("kind: system\nid: r\nboards:\n"
                 "  ecu: {board: mainlite, role: ecu, firmware_ref: ''}\n")
    with pytest.raises(SystemError):
        System(p)
