"""Host-only checks that nothing a system file, a device param override or a
run scenario holds can change the structure of the Renode text built from it:
every hostile value is either refused or lands as one inert token.

The property: for each field and each hostile string, System() refuses the
file, or the rendered script has exactly the structure of the clean one (same
lines, same commands, same platform-description entries, string literals and
file arguments aside)."""
import copy
import functools
import math
import os
import re
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from vhil import renode as rn
from vhil.renode import RenodeMonitor, UnsafeText
from vhil.server.runs import RunRequest
from vhil import system as vsystem
from vhil.system import REPO, System, SystemError

HOSTILE = [
    'a"b', 'a"""b', '"""\nmach create "x"\n"""', "a\nb", "a\rb", "a;b", "x; quit",
    "a\\b", "a'b", "`id`", "$elf_ams", "#c", "a b", "a: b", "a, b", "@/etc/passwd",
    "../../etc/passwd", "/etc/passwd", "", "a\x00b", "\u00fcn\u00efc\u00f6de", "\u202ex",
    'x"\n    imageFile: "/etc/shadow', "a -> b@0", "sysbus.cpu", "0x10",
    "plain\n", "can0\n",   # a JSON-schema pattern ending in $ lets a trailing newline through
]

AMS, ECU_AMS, ECU = (REPO / "systems" / f"{s}.yaml" for s in ("ams", "ecu-ams", "ecu"))


def structure(text: str) -> list[str]:
    """The script with its string literals, file arguments, comments and
    single platform-description values blanked: what a value must not be
    able to change."""
    out = []
    for line in text.splitlines():
        if line.startswith("#") or line.startswith(":name:"):
            out.append("#")
            continue
        line = re.sub(r'"(?!"")[^"\n]*"', '"S"', line)
        line = re.sub(r"@\S+", "@P", line)
        out.append(re.sub(r'^(    \w+): (?:"S"|true|false|-?[0-9][0-9A-Za-z.+-]*)$', r"\1: V", line))
    return out


@pytest.fixture(autouse=True)
def _cached_catalogue(monkeypatch):
    """Catalogue entries parsed once: the matrix below builds ~2500 systems,
    and parsing the catalogue's YAML is most of each one. A mutation only
    ever touches the system file, never the catalogue."""
    monkeypatch.setattr(vsystem, "_entry", _cached_entry)


@functools.cache
def _entry_once(kind: str, ident: str, catalog: Path) -> dict:
    return _ORIGINAL_ENTRY(kind, ident, catalog)


def _cached_entry(kind, ident, catalog):
    if not isinstance(ident, str):
        return _ORIGINAL_ENTRY(kind, ident, catalog)
    return copy.deepcopy(_entry_once(kind, ident, catalog))


_ORIGINAL_ENTRY = vsystem._entry


def _render(path: Path) -> str:
    system = System(path)
    return system.render_renode({b: Path(f"/fw/{b}.elf") for b in system.boards}, socketcan=True)


def _write(tmp_path: Path, doc: dict, name: str = "s.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return p


def _rename(mapping: dict, old: str, new: str) -> None:
    items = list(mapping.items())
    mapping.clear()
    mapping.update((new if k == old else k, v) for k, v in items)


# Each mutation puts a value into one field of a system document.
def _set(*keys):
    def mutate(doc, v):
        d = doc
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = v
    return mutate


MUTATIONS = {
    AMS: {
        "id": _set("id"),
        "board key": lambda d, v: d["boards"].update({v: dict(d["boards"]["ams"])}),
        "board.board": _set("boards", "ams", "board"),
        "board.firmware": _set("boards", "ams", "firmware"),
        "board.role": _set("boards", "ams", "role"),
        "firmware_ref": _set("boards", "ams", "firmware_ref"),
        "bus key": lambda d, v: _rename(d["buses"], "can_acu", v),
        "bus node": lambda d, v: d["buses"]["can_acu"].update(nodes=[v]),
        "host_netdev": _set("buses", "can_acu", "host_netdev"),
        "device key": lambda d, v: _rename(d["devices"], "sd", v),
        "device model": _set("devices", "sd", "model"),
        "sdmmc": _set("devices", "sd", "sdmmc"),
        "cs": _set("devices", "isospi", "cs"),
        "i2c": _set("devices", "imu", "i2c"),
        "attach": _set("devices", "cells", "attach"),
        "param key": lambda d, v: d["devices"]["sd"].setdefault("params", {}).update({v: 1}),
        "param image": lambda d, v: d["devices"]["sd"].setdefault("params", {}).update(image=v),
        "param capacity": lambda d, v: d["devices"]["sd"].setdefault("params", {}).update(capacity=v),
        "param dead": lambda d, v: d["devices"]["sd"].setdefault("params", {}).update(dead=v),
        "param write_farthest_first": lambda d, v: d["devices"]["cells"].setdefault(
            "params", {}).update(write_farthest_first=v),
        "param current_A": lambda d, v: d["devices"]["pack_current"].setdefault(
            "params", {}).update(current_A=v),
        "output key": lambda d, v: _rename(d["devices"]["dcdc_current"]["outputs"], "out", v),
        "output endpoint": _set("devices", "dcdc_current", "outputs", "out"),
        "bench power": lambda d, v: d["bench"]["power"][0].update(board=v),
        "gpio route": lambda d, v: d["bench"]["gpio_routes"][0].update(to=v),
        "gpio route tca": lambda d, v: d["bench"]["gpio_routes"][0].update(tca=v),
        "adc route": lambda d, v: d["bench"]["adc_routes"][0].update(**{"from": v}),
        "adc route channel": lambda d, v: d["bench"]["adc_routes"][0].update(channel=v),
        "quantum_s": _set("time", "quantum_s"),
    },
    ECU_AMS: {
        "arbitration": lambda d, v: d["buses"]["can_acu"].update(arbitration=v),
        "dac route": lambda d, v: d["bench"]["dac_routes"][0].update(to=v),
        "bootloader_ref": lambda d, v: d["boards"]["ecu"].update(bootloader_ref=v),
    },
    ECU: {
        "signal name": lambda d, v: _rename(d["port"]["signals"], "start", v),
        "signal endpoint": lambda d, v: d["port"]["signals"].update(start={"gpio_in": v}),
        "signal bus": lambda d, v: d["port"]["signals"].update(x={"can_tx": {"bus": v, "id": 1}}),
    },
}
# A value each field takes, to render the clean script against (else the
# file as it is).
BENIGN = {"param image": "plain.img"}
CASES = [(src, field, value) for src, fields in MUTATIONS.items() for field in fields
         for value in HOSTILE]


@pytest.mark.parametrize("src, field, value", CASES,
                         ids=[f"{s.stem}-{f}-{i % len(HOSTILE)}" for i, (s, f, _) in
                              enumerate(CASES)])
def test_a_hostile_system_value_is_refused_or_inert(tmp_path, src, field, value):
    try:
        text = _render_with(tmp_path, src, field, value)
    except (SystemError, UnsafeText):
        return
    assert structure(text) in _clean(src, field), f"{field}={value!r} changed the script"


def _render_with(folder: Path, src: Path, field: str, v: str) -> str:
    doc = yaml.safe_load(src.read_text())
    MUTATIONS[src][field](doc, v)
    (folder / v.encode().hex()[:32]).mkdir(parents=True, exist_ok=True)
    return _render(_write(folder / v.encode().hex()[:32], doc, src.name))


@functools.cache
def _clean(src: Path, field: str) -> tuple:
    """The clean scripts' structures: the file as it is, and with a plain value
    in the field (an unset param, "", renders as the first). Once per field."""
    import tempfile
    clean = [structure(_render(src))]
    with tempfile.TemporaryDirectory() as d:
        try:
            clean.append(structure(_render_with(Path(d), src, field,
                                                BENIGN.get(field, "plain_1"))))
        except SystemError:
            pass
    return tuple(clean)


@pytest.mark.parametrize("value", HOSTILE)
def test_the_encoder_holds_when_validation_is_bypassed(monkeypatch, value):
    """A regression in the checks must still not reach the script: with
    _check_params and _check_names off, a hostile param value is refused by
    the encoder or stays one quoted token."""
    system = System(AMS)
    monkeypatch.setattr(System, "_check_params", lambda *a: None)
    monkeypatch.setattr(System, "_check_names", lambda *a: None)
    baseline = structure(system.render_renode())
    for dev, key in (("cells", "write_farthest_first"), ("sd", "capacity"), ("sd", "dead")):
        d = copy.deepcopy(system.devices[dev])
        d["params"] = {key: value}
        system.devices[dev], saved = d, system.devices[dev]
        try:
            text = system.render_renode()
        except (UnsafeText, SystemError):
            continue
        finally:
            system.devices[dev] = saved
        if value:
            assert structure(text) == baseline, f"{dev}.{key}={value!r}"


@pytest.mark.parametrize("value", [v for v in HOSTILE if not rn.IDENT.fullmatch(v)])
@pytest.mark.parametrize("what", ["buses", "devices"])
def test_hostile_names_never_reach_the_script_unchecked(monkeypatch, value, what):
    """Bus and device names that slipped past the checks are refused by the
    encoders when the script is written."""
    system = System(AMS)
    monkeypatch.setattr(System, "_check_names", lambda *a: None)
    _rename(getattr(system, what), {"buses": "can_acu", "devices": "sd"}[what], value)
    with pytest.raises(UnsafeText):
        system.render_renode(socketcan=True)


# -- the encoders ------------------------------------------------------------------

@pytest.mark.parametrize("value", [v for v in HOSTILE if v and not re.fullmatch(r"[\w.]+", v)])
def test_identifiers_and_paths_refuse_anything_but_words(value):
    with pytest.raises(UnsafeText):
        rn.ident(value)
    with pytest.raises(UnsafeText):
        rn.path(value)


@pytest.mark.parametrize("value", ['a"b', "a\nb", "a\rb", "a;b", "a\\b", "a'b", "`id`", "$x",
                                   "#c", "a\x00b", "\u00fc", "\u202e", "a\tb"])
def test_strings_refuse_what_could_end_them(value):
    with pytest.raises(UnsafeText):
        rn.quote(value)


def test_strings_pass_plain_text_through_quoted():
    assert rn.quote("sysbus.gpioPortB") == '"sysbus.gpioPortB"'
    assert rn.quote("/tmp/pytest-of-x/card 1.img") == '"/tmp/pytest-of-x/card 1.img"'


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "1", None, b"1"])
def test_numbers_are_finite_numbers(value):
    with pytest.raises(UnsafeText):
        rn.number(value)


def test_file_arguments_are_one_absolute_word(tmp_path):
    assert rn.file_arg(tmp_path / "x.elf") == "@" + (tmp_path / "x.elf").resolve().as_posix()
    for bad in ("a b.elf", "a;b.elf", 'a"b', "a\nb"):
        with pytest.raises(UnsafeText):
            rn.file_arg(tmp_path / bad)


def test_comments_stay_on_one_line():
    assert rn.comment("a\nmach create\r\"x\"\u00fc") == 'a?mach create?"x"?'


def test_a_monitor_command_is_one_line():
    monitor = object.__new__(RenodeMonitor)   # never connected: it must refuse before sending
    for bad in ("version\nquit", "version\rquit", "a\x00b"):
        with pytest.raises(UnsafeText):
            monitor.execute(bad)


# -- the monitor binding -----------------------------------------------------------

def test_renode_starts_with_its_own_monitor_port_off():
    """Renode's -P <port> listens on every interface; ours (VhilMonitor.cs)
    on 127.0.0.1. -P is always -1 and the loopback monitor gets the port."""
    cmd = rn.monitor_command("renode", 40123)
    assert cmd[cmd.index("-P") + 1] == "-1"
    assert cmd.count("-P") == 1 and "--port" not in cmd
    execute = cmd[cmd.index("-e") + 1]
    assert execute == (f"include @{rn.MONITOR_SOURCE.as_posix()}; "
                       f"emulation StartVhilMonitor 40123")
    source = rn.MONITOR_SOURCE.read_text()
    assert "new TcpListener(IPAddress.Loopback, port)" in source


@pytest.mark.parametrize("module", ["vhil/sim.py", "vhil/bench.py"])
def test_no_module_starts_renode_with_a_monitor_port(module):
    text = (REPO / module).read_text()
    assert '"-P"' not in text and "--port" not in text
    assert "rn.launch(" in text


@pytest.mark.parametrize("script", ["scripts/explore.sh", "scripts/probe.sh"])
def test_console_scripts_turn_the_monitor_port_off(script):
    text = (REPO / script).read_text()
    assert "--console -P -1" in text


# -- sd-card images ----------------------------------------------------------------

def _ams_with(tmp_path, image, **kw) -> System:
    doc = yaml.safe_load(AMS.read_text())
    doc["devices"]["sd"]["params"] = {"image": image}
    return System(_write(tmp_path, doc, "ams.yaml"), **kw)


def test_a_card_image_inside_a_card_dir_is_used_by_its_real_path(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "c.img").write_bytes(b"")
    s = _ams_with(tmp_path, str(cards / "c.img"), extra_card_dirs=[cards])
    assert f'imageFile: "{(cards / "c.img").resolve().as_posix()}"' in s.render_renode()


def test_a_relative_card_image_is_in_the_configured_dir(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    cards.mkdir()
    monkeypatch.setenv("VHIL_CARD_DIR", str(cards))
    s = _ams_with(tmp_path, "c.img")
    assert f'imageFile: "{(cards / "c.img").resolve().as_posix()}"' in s.render_renode()
    assert "persistent: true" in s.render_renode()


@pytest.mark.parametrize("image", ["/etc/passwd", "../x.img", "cards/../../x.img", "a/../b.img",
                                   "a\\b.img", "a\x00b"])
def test_a_card_image_outside_the_card_dirs_is_refused(tmp_path, monkeypatch, image):
    cards = tmp_path / "cards"
    cards.mkdir()
    monkeypatch.setenv("VHIL_CARD_DIR", str(cards))
    with pytest.raises(SystemError):
        _ams_with(tmp_path, image)


def test_a_symlink_out_of_the_card_dir_is_refused(tmp_path, monkeypatch):
    cards, outside = tmp_path / "cards", tmp_path / "outside"
    cards.mkdir()
    outside.mkdir()
    (outside / "x.img").write_bytes(b"")
    (cards / "link.img").symlink_to(outside / "x.img")
    (cards / "dir").symlink_to(outside)
    monkeypatch.setenv("VHIL_CARD_DIR", str(cards))
    for image in ("link.img", "dir/x.img", str(cards / "link.img")):
        with pytest.raises(SystemError, match="not inside a card-image directory"):
            _ams_with(tmp_path, image)


def test_a_card_image_is_refused_without_its_dir_even_in_tmp(tmp_path, monkeypatch):
    """The default card dir is build/cards: a test's temp image needs its
    directory passed (Sim(card_dirs=...)), as tests/sim/test_ams_sd.py does."""
    monkeypatch.delenv("VHIL_CARD_DIR", raising=False)
    (tmp_path / "c.img").write_bytes(b"")
    with pytest.raises(SystemError, match="not inside a card-image directory"):
        _ams_with(tmp_path, str(tmp_path / "c.img"))
    _ams_with(tmp_path, str(tmp_path / "c.img"), extra_card_dirs=[tmp_path])


def test_sim_params_are_checked_as_the_system_files_are(tmp_path):
    from vhil.sim import Sim
    (tmp_path / "c.img").write_bytes(b"")
    with pytest.raises(SystemError, match="not inside"):
        Sim(AMS, {"ams": "/fw/ams.elf", "ams.bootloader": "/fw/bl.elf"}, params={"sd": {"image": "/etc/passwd"}})
    with pytest.raises(SystemError, match="must be true or false"):
        Sim(AMS, {"ams": "/fw/ams.elf", "ams.bootloader": "/fw/bl.elf"}, params={"sd": {"dead": "true\nquit"}})
    with pytest.raises(SystemError, match="unknown params"):
        Sim(AMS, {"ams": "/fw/ams.elf", "ams.bootloader": "/fw/bl.elf"}, params={"sd": {"imageFile": "x"}})
    sim = Sim(AMS, {"ams": "/fw/ams.elf", "ams.bootloader": "/fw/bl.elf"}, params={"sd": {"image": str(tmp_path / "c.img")}},
              card_dirs=[tmp_path])
    assert sim.system.devices["sd"]["params"]["image"] == str(tmp_path / "c.img")


# -- param types -------------------------------------------------------------------

@pytest.mark.parametrize("param, value, message", [
    ("dead", "false", "true or false"),
    ("dead", 0, "true or false"),
    ("capacity", "0x10", "an integer"),
    ("capacity", True, "an integer"),
    ("capacity", 1.5, "an integer"),
    ("image", 5, "card image file name"),
])
def test_sd_card_params_take_their_declared_types(tmp_path, param, value, message):
    doc = yaml.safe_load(AMS.read_text())
    doc["devices"]["sd"]["params"] = {param: value}
    with pytest.raises(SystemError, match=message):
        System(_write(tmp_path, doc, "ams.yaml"))


@pytest.mark.parametrize("value", [math.nan, math.inf, "1"])
def test_an_analog_input_is_a_finite_number(tmp_path, value):
    doc = yaml.safe_load(AMS.read_text())
    doc["devices"]["pack_current"]["params"] = {"current_A": value}
    with pytest.raises(SystemError):
        System(_write(tmp_path, doc, "ams.yaml"))


def test_a_string_param_without_a_format_is_a_plain_word(tmp_path):
    catalog = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog)
    model = yaml.safe_load((catalog / "models" / "ltc6820.yaml").read_text())
    model["params"] = {"mode": "fast"}
    (catalog / "models" / "ltc6820.yaml").write_text(yaml.safe_dump(model))
    doc = yaml.safe_load(AMS.read_text())
    doc["devices"]["isospi"]["params"] = {"mode": "slow-2"}
    System(_write(tmp_path, doc, "ams.yaml"), catalog)
    for bad in ('slow"', "a b", "a;b", "a\nb", "x" * 129):
        doc["devices"]["isospi"]["params"] = {"mode": bad}
        with pytest.raises(SystemError, match="letters, digits"):
            System(_write(tmp_path, doc, "ams.yaml"), catalog)


# -- run scenarios ---------------------------------------------------------------

def _run(**scenario):
    return {"system": "ecu", "scenario": {"kind": "run", "virtual_ms": 10, **scenario}}


@pytest.mark.parametrize("value", [v for v in HOSTILE if v and not re.fullmatch(r"\w+", v)])
@pytest.mark.parametrize("make", [
    lambda v: _run(stimuli=[{"kind": "can_send", "bus": v, "id": 1}]),
    lambda v: _run(stimuli=[{"kind": "can_periodic", "bus": v, "id": 1, "period_ms": 10}]),
    lambda v: _run(stimuli=[{"kind": "gpio", "board": v, "pin": "PB5", "level": True}]),
    lambda v: _run(stimuli=[{"kind": "gpio", "board": "ecu", "pin": v, "level": True}]),
    lambda v: _run(stimuli=[{"kind": "analog", "board": v, "pin": "PF7", "volts": 1}]),
    lambda v: _run(stimuli=[{"kind": "analog", "board": "ecu", "pin": v, "volts": 1}]),
    lambda v: _run(stimuli=[{"kind": "can_send", "bus": "can_acu", "id": 1, "data": v}]),
    lambda v: _run(watch=[{"kind": "pin", "board": v, "pin": "PB4"}]),
    lambda v: _run(watch=[{"kind": "pin", "board": "ecu", "pin": v}]),
    lambda v: _run(watch=[{"kind": "symbol", "board": v, "name": "x"}]),
    lambda v: _run(watch=[{"kind": "symbol", "board": "ecu", "name": v}]),
], ids=["can_send.bus", "can_periodic.bus", "gpio.board", "gpio.pin", "analog.board",
        "analog.pin", "can.data", "pin.board", "pin.pin", "symbol.board", "symbol.name"])
def test_a_hostile_scenario_name_is_refused(make, value):
    with pytest.raises(ValidationError):
        RunRequest.model_validate(make(value))


# -- what the Sim sends --------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.sent = []

    def execute(self, command):
        assert "\n" not in command and "\r" not in command
        self.sent.append(command)
        return ""


@pytest.fixture
def sim():
    from vhil.sim import Sim
    s = Sim(ECU, {"ecu": "/fw/ecu.elf", "ecu.bootloader": "/fw/bl.elf"})
    s._monitor = _Recorder()
    return s


@pytest.mark.parametrize("value", [v for v in HOSTILE if not re.fullmatch(r"[\w .,:/+=@-]*", v)])
def test_the_sim_refuses_to_send_a_hostile_string(sim, value):
    calls = [
        lambda: sim.can("can_acu").send_periodic(value, 1, b"", 10),
        lambda: sim.can("can_acu").update_periodic(value, b""),
        lambda: sim.can("can_acu").stop_periodic(value),
        lambda: sim.can("can_acu").node(value),
        lambda: sim.io("ecu").set_input(value, 5, True),
        lambda: sim.io("ecu").watch(value, 5),
        lambda: sim.io("ecu").edges(value),
        lambda: sim.io("ecu").level(value),
        lambda: sim.call(value, "Respond", True),
        lambda: sim.call("sysbus.gpioPortB", value),
        lambda: sim.call("sysbus.gpioPortB", "Respond", value),
    ]
    for call in calls:
        before = len(sim._monitor.sent)
        with pytest.raises(UnsafeText):
            call()
        # Only a `mach set` for the board may have gone out, never the command.
        assert all(c.startswith("mach set") for c in sim._monitor.sent[before:])


def test_the_sim_sends_plain_commands_as_before(sim):
    sim.can("can_acu").send(0x100, b"\x01\x02")
    sim.can("can_acu").send_periodic("hb", 0x100, b"\x01", 10, start_us=5)
    sim.io("ecu").set_input("sysbus.gpioPortB", 5, True)
    sim.call("sysbus.gpioPortB", "Respond", False, 3, "x")
    assert sim._monitor.sent == [
        'mach set "ecu"', 'vhil_probe_can_acu Send 256 "0102" false',
        'mach set "ecu"', 'vhil_probe_can_acu SendPeriodic "hb" 256 "01" 10000 5 false',
        'mach set "ecu"', 'vhil_gpio_ecu Drive "sysbus.gpioPortB" 5 true',
        'mach set "ecu"', 'sysbus.gpioPortB Respond false 3 "x"',
    ]


def test_the_editor_names_its_temp_file_only_by_a_valid_id(tmp_path, monkeypatch):
    from vhil import editor
    monkeypatch.setattr(editor.tempfile, "TemporaryDirectory",
                        lambda: _Dir(tmp_path / "t"))
    (tmp_path / "t").mkdir()
    errors = editor.validate({"kind": "system", "id": "../../escaped", "boards": {}})
    assert errors
    assert not (tmp_path / "escaped.yaml").exists()
    assert not any(p.name == "escaped.yaml" for p in tmp_path.rglob("*"))


class _Dir:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return str(self.path)

    def __exit__(self, *exc):
        pass


# -- roles: labels, GPIO re-kinds, firmware refs from a remote ----------------------

def _mainlite() -> dict:
    return yaml.safe_load((REPO / "catalog" / "boards" / "mainlite.yaml").read_text())


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("where", ["pin label", "onboard label", "role firmware",
                                   "backplane page"])
def test_a_hostile_roles_table_is_refused_without_the_schema(value, where):
    """The catalogue's labels, role firmware and backplane page are checked
    by System() too, so a schema that loosens lets none of them through."""
    board = _mainlite()
    if where == "pin label":
        board["roles"]["ecu"]["pins"]["PF8"] = value
    elif where == "onboard label":
        board["onboard"]["SDMMC1"] = value
    elif where == "role firmware":
        board["roles"]["udv"]["firmware"] = value
    else:
        board["roles"]["ams"]["backplane"]["doc"] = value
    plain = {"pin label": vsystem.LABEL, "onboard label": vsystem.LABEL,
             "role firmware": vsystem.ID, "backplane page": vsystem.BACKPLANE_DOC}[where]
    if plain.fullmatch(value):
        System.check_board_roles(board)       # a plain word is just a label
        return
    with pytest.raises(SystemError):
        System.check_board_roles(board)


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_label_never_reaches_a_system_file(value):
    """Labels are display only: whatever an interface reads, the system file
    the editor writes names the pin."""
    from vhil import editor
    spec = editor.specification()
    doc = yaml.safe_load(AMS.read_text())
    graph = editor.to_dataflow(doc, spec)
    for n in graph["graphs"][0]["nodes"]:
        for i in n["interfaces"]:
            if n["name"].startswith("mainlite") and " · " in i["name"]:
                i["name"] = f"{editor.pin_of(i['name'])} · {value}"
    assert editor.from_dataflow(graph, spec) == doc


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_gpio_rekind_port_is_refused_or_inert(tmp_path, value):
    """A role's GPIO re-kind lands in the script only through a device's
    chip select, as a board GPIO's does: through the path encoder."""
    def catalog_with(port: str, where: Path) -> Path:
        shutil.copytree(REPO / "catalog", where)
        board = _mainlite()
        board["roles"]["ams"]["gpio"]["PF9"]["port"] = port
        (where / "boards" / "mainlite.yaml").write_text(yaml.safe_dump(board, sort_keys=False))
        return where

    doc = yaml.safe_load(AMS.read_text())
    doc["devices"]["isospi"]["cs"] = "ams.PF9"
    path = _write(tmp_path, doc, "ams.yaml")
    images = {"ams": Path("/fw/ams.elf")}
    try:
        text = System(path, catalog_with(value, tmp_path / "hostile")).render_renode(
            images, socketcan=True)
    except (SystemError, UnsafeText):
        return
    clean = System(path, catalog_with("sysbus.gpioPortF", tmp_path / "clean"))
    # The port is a value: one peripheral path, alone on its line (a plain
    # one, like sysbus.cpu, names another peripheral but adds nothing).
    port = lambda s: [re.sub(r"^[A-Za-z_][\w.]*:$", "PORT:", line) for line in structure(s)]
    assert port(text) == port(clean.render_renode(images, socketcan=True)), value


@pytest.mark.parametrize("value", HOSTILE + ["--upload-pack=touch /tmp/x", "a..b", "x\x00y"])
def test_a_remote_ref_name_reaches_nothing_unless_a_system_could_hold_it(value):
    """git ls-remote output is the remote's: a branch or tag name that isn't
    a plain git ref (vhil.system.REF, what a firmware_ref may be) is dropped
    before the picker ever offers it."""
    from vhil.server.githost import parse_ls_remote
    sha = "a" * 40
    got = parse_ls_remote(f"{sha}\trefs/heads/{value}\n{sha}\trefs/tags/{value}\n")
    names = [r["name"] for kind in ("branches", "tags") for r in got[kind]]
    assert all(vsystem.REF.fullmatch(n) for n in names)
    if vsystem.REF.fullmatch(value):
        assert names == [value, value]
    else:
        assert value not in names


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_remote_sha_is_dropped(value):
    from vhil.server.githost import parse_ls_remote
    assert parse_ls_remote(f"{value}\trefs/heads/dev\n") == {"branches": [], "tags": []}


ROUTE_FIELDS = {
    "gpio to": lambda d, v: d["bench"]["gpio_routes"][0].update(to=v),
    "gpio tca": lambda d, v: d["bench"]["gpio_routes"][0].update(tca=v),
    "gpio pin": lambda d, v: d["bench"]["gpio_routes"][0].update(pin=v),
    "adc from": lambda d, v: d["bench"]["adc_routes"][0].update(**{"from": v}),
    "adc channel": lambda d, v: d["bench"]["adc_routes"][0].update(channel=v),
    "cell source device": lambda d, v: d["bench"]["cell_stimulus"].update(device=v),
    "cell source mV": lambda d, v: d["bench"]["cell_stimulus"].update(cell_mV=v),
}


@pytest.mark.parametrize("field", ROUTE_FIELDS)
@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_bench_route_never_reaches_the_monitor(tmp_path, field, value):
    """The broker turns gpio_routes and adc_routes into monitor commands
    (the probe's Drive, Watch and Level): a hostile value is refused by the
    system, never sent."""
    doc = yaml.safe_load(AMS.read_text())
    ROUTE_FIELDS[field](doc, value)
    with pytest.raises(SystemError):
        System(_write(tmp_path, doc))
