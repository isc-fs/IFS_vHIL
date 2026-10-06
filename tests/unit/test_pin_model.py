"""The MainLite's vendored pin model, and the board, roles and systems checked
against it (vhil/pin_model.py, catalog/boards/mainlite.yaml `pin_model`)."""
import hashlib
import re
import shutil

import pytest
import yaml

from vhil import pin_model as pm
from vhil.system import REPO, System, SystemError

MODEL = REPO / "catalog" / "pin-models" / "mainlite.pins.yaml"
BOARD = REPO / "catalog" / "boards" / "mainlite.yaml"


@pytest.fixture(scope="module")
def model():
    return pm.load(MODEL)


def _board():
    return yaml.safe_load(BOARD.read_text())


def test_the_vendored_model_is_the_release_asset_it_records():
    """The body after the header hashes to the header's sha256: the file is
    the release's mainlite.pins.yaml, byte for byte (scripts/update-pin-model.sh)."""
    fields, body = pm.split(MODEL.read_text())
    assert fields["repo"] == "isc-fs/IFS08-ES-MainLite"
    assert re.fullmatch(r"pin-model-v\d+\.\d+", fields["tag"])
    assert hashlib.sha256(body.encode()).hexdigest() == fields["sha256"]
    assert fields["url"].endswith(f"/releases/download/{fields['tag']}/mainlite.pins.yaml")


def test_the_model_is_the_mainlite_s(model):
    doc = model.doc
    assert (doc["schema"], doc["schema_version"]) == ("isc-fs/mainlite-pins", 1)
    assert doc["mcu"]["part"] == "STM32H733ZGTx" and len(doc["pins"]) == 144
    io = [p for p in doc["pins"] if p["type"] == "io"]
    counts = {c: sum(p["class"] == c for p in io) for c in ("backplane", "onboard", "nc")}
    assert len(io) == 114 and counts == doc["summary"]["io_by_class"]
    assert model.label == "pin model v1.0" and model.board_name == "MainLite"


def test_hse_is_the_model_s_crystal(model):
    """Invariant 2: the platform's HSE is the MainLite's 24 MHz crystal."""
    repl = (REPO / "platforms" / "cpus" / "stm32h733.repl").read_text()
    hse = int(re.search(r"hseFrequency:\s*(\d+)", repl).group(1))
    assert hse == model.doc["interfaces"]["hse"]["frequency_hz"] == 24_000_000


def test_the_board_has_every_pin_that_leaves_the_module(model):
    """Every backplane pin of the model is a pin of the board, or one of a
    peripheral's (SPI1's PA5/PA6/PA7, USART10's PG11/PG12); nothing else is
    but the on-board SDMMC1 and I2C2."""
    board = _board()
    names = [c for s in ("can", "spi", "uart", "sdmmc", "i2c", "gpio", "analog_in", "unwired")
             for c in board.get(s) or {}]
    ports = {p["port"] for n in names for p in model.pins_of(n)}
    backplane = {p["port"] for p in model.backplane_pins()}
    assert len(backplane) == 22 and backplane <= ports
    off = {n for n in names if model.status(n).cls != "backplane"}
    assert off == set(board["onboard"]) == {"SDMMC1", "I2C2"}
    pins = {n for n in names if n in model.by_port}
    assert len(pins) == 17 and len(backplane - pins) == 3 + 2      # SPI1, USART10
    System.check_board_roles(board)


def _catalog_with(tmp_path, old, new, path="boards/mainlite.yaml"):
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    f = catalog / path
    assert old in f.read_text()
    f.write_text(f.read_text().replace(old, new, 1))
    return catalog


def _system(tmp_path, body, role="ams"):
    p = tmp_path / "s.yaml"
    p.write_text(f"kind: system\nid: s\nboards:\n  {role}: {{board: mainlite, role: {role}}}\n"
                 + body)
    return p


@pytest.mark.parametrize("pin, message", [
    ("PF3", "\\(mainlite\\): PF3 is not routed off the MainLite \\(pin model v1.0: class nc\\)"),
    ("PE3", "PE3 is not routed off the MainLite \\(pin model v1.0: class onboard, MICROSD_DET\\)"),
    ("PX9", "has no connector or pin 'PX9' \\(nor has its pin model v1.0\\)"),
])
def test_a_system_wires_only_pins_that_leave_the_mainlite(tmp_path, pin, message):
    p = _system(tmp_path, f"devices:\n  i: {{model: acs758lcb-050b, outputs: {{out: ams.{pin}}}}}\n")
    with pytest.raises(SystemError, match=message):
        System(p)


# The backplane pins the board once listed as `unwired`, now wired: each
# resolves to its GPIO or ADC3 channel (catalog/boards/mainlite.yaml).
WIRED_PINS = {
    "PB6": ("gpio", {"port": "sysbus.gpioPortB", "pin": 6}),
    "PB7": ("gpio", {"port": "sysbus.gpioPortB", "pin": 7}),
    "PB8": ("gpio", {"port": "sysbus.gpioPortB", "pin": 8}),
    "PD5": ("gpio", {"port": "sysbus.gpioPortD", "pin": 5}),
    "PB0": ("gpio", {"port": "sysbus.gpioPortB", "pin": 0}),
    "PC5": ("gpio", {"port": "sysbus.gpioPortC", "pin": 5}),
    "PC4": ("gpio", {"port": "sysbus.gpioPortC", "pin": 4}),
    "PF10": ("analog", {"adc": "sysbus.adc3_h73x", "channel": 6}),
    "PC0": ("analog", {"adc": "sysbus.adc3_h73x", "channel": 10}),
    "PC2_C": ("analog", {"adc": "sysbus.adc3_h73x", "channel": 0}),
    "USART10": ("uart", "sysbus.usart10"),
}
# What a role makes of them: a digital line where the board has an ADC input.
ROLE_KINDS = {"ams": {"PF10": ("gpio", {"port": "sysbus.gpioPortF", "pin": 10})},
              "udv": {"PC2_C": ("gpio", {"port": "sysbus.gpioPortC", "pin": 2})}}


@pytest.mark.parametrize("role", ["ecu", "ams", "udv"])
def test_every_backplane_pin_is_emulated(tmp_path, role):
    """No pin is left `unwired`: each resolves, in every role, to a GPIO or
    an ADC3 channel (or USART10), as the role's backplane uses it."""
    assert "unwired" not in _board()
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    if role == "udv":       # no udv firmware in the catalogue yet: lend it one
        (catalog / "firmware" / "udv.yaml").write_text(
            (catalog / "firmware" / "ecu.yaml").read_text().replace("id: ecu", "id: udv"))
    board = System(_system(tmp_path, "", role), catalog).boards[role]
    for pin, want in WIRED_PINS.items():
        kind, target = board.endpoint(pin)
        assert (kind, target) == ROLE_KINDS.get(role, {}).get(pin, want), (role, pin)


def _unwire(tmp_path, pin="PB6"):
    """A catalogue whose board lists `pin` as `unwired` again (the mechanism a
    board with pins the emulator can't wire yet still uses)."""
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    f = catalog / "boards" / "mainlite.yaml"
    line = f"  {pin}: {{port: sysbus.gpioPortB, pin: {pin[2:]}}}\n"
    assert line in f.read_text()
    f.write_text(f.read_text().replace(line, "", 1) + f"unwired:\n  {pin}: gpio\n")
    return catalog


@pytest.mark.parametrize("body", [
    "port:\n  step_ms: 10\n  signals:\n    air_n: {gpio_out: ams.PB6}\n",
    "devices:\n  isospi: {model: ltc6820, spi: ams.SPI1, cs: ams.PB6}\n"
    "  cells: {model: ltc6811, attach: isospi}\n",
])
def test_a_pin_the_emulator_does_not_wire_is_refused_as_not_emulated(tmp_path, body):
    with pytest.raises(SystemError, match="leaves the module but is not emulated yet"):
        System(_system(tmp_path, body), _unwire(tmp_path))


def test_the_not_emulated_error_names_the_header_pin(tmp_path):
    p = _system(tmp_path, "port:\n  step_ms: 10\n  signals:\n    d: {gpio_out: ams.PB6}\n")
    with pytest.raises(SystemError, match=r"PB6 \(J3\.8\) leaves the module"):
        System(p, _unwire(tmp_path))


PD5 = "  PD5: {port: sysbus.gpioPortD, pin: 5}"


@pytest.mark.parametrize("old, new, message", [
    # A role routes only what leaves the module.
    ("      PB9: SPARE_J3        # D6", "      PE2: SPARE_J3        # D6",
     "role ecu routes PE2, which does not leave the board \\(pin model v1.0: class nc"),
    ("      PB9: SPARE_J3        # D6", "      SDMMC1: SPARE_J3        # D6",
     "role ecu routes SDMMC1, which does not leave the board \\(pin model v1.0: class onboard"),
    ("      PB9: SPARE_J3        # D6", "      PZ9: SPARE_J3        # D6",
     "role ecu labels 'PZ9', which its pin model v1.0 lacks"),
    # The board has every backplane pin of its model, and nothing else.
    (PD5, "  # PD5",
     "routes PD5 \\(J3.3\\) to the backplane, but the board lacks them"),
    (PD5, PD5 + "\n  PE2: {port: sysbus.gpioPortE, pin: 2}",
     "board mainlite: PE2 is not routed off the board \\(pin model v1.0: class nc"),
    (PD5, PD5 + "\n  PE3: {port: sysbus.gpioPortE, pin: 3}",
     "PE3 is not routed off the board \\(pin model v1.0: class onboard, MICROSD_DET"),
    ("  USART10: sysbus.usart10", "  USART10: sysbus.usart10\n  USART3: sysbus.usart3",
     "USART3 is not a pin or peripheral of its pin model v1.0"),
    ("  I2C2: BMI088", "  I2C2: BMI088\n  SPI1: radio",
     "onboard SPI1 is not on the board only \\(pin model v1.0: class backplane"),
    ("pin_model: mainlite.pins.yaml", "pin_model: other.pins.yaml", "no pin model"),
])
def test_the_board_and_roles_are_checked_against_the_model(tmp_path, old, new, message):
    with pytest.raises(SystemError, match=message):
        System(REPO / "systems" / "ecu.yaml", _catalog_with(tmp_path, old, new))


def test_a_model_that_is_not_its_release_is_refused(tmp_path):
    catalog = _catalog_with(tmp_path, 'net: "GPIO1"', 'net: "GPIOX"', "pin-models/mainlite.pins.yaml")
    with pytest.raises(SystemError, match="is not the pin-model-v1.0 release asset"):
        System(REPO / "systems" / "ecu.yaml", catalog)


def test_an_unsupported_schema_version_is_refused(tmp_path):
    fields, body = pm.split(MODEL.read_text())
    body = body.replace("schema_version: 1\n", "schema_version: 2\n", 1)
    head = MODEL.read_text().split(pm.HEADER_END)[0].replace(
        fields["sha256"], hashlib.sha256(body.encode()).hexdigest())
    catalog = _catalog_with(tmp_path, "x", "x", "pin-models/mainlite.pins.yaml")
    (catalog / "pin-models" / "mainlite.pins.yaml").write_text(head + pm.HEADER_END + body)
    with pytest.raises(SystemError, match="version 2 is not one vhil reads"):
        System(REPO / "systems" / "ecu.yaml", catalog)
