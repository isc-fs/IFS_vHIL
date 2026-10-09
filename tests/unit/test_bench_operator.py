"""Host-only checks of the bench operator's steps (vhil/bench_operator.py, #152)
against a bench whose monitor records commands."""
import os
import shutil
import subprocess

import pytest

from vhil import bench_operator as op
from vhil.system import REPO, System

AMS = REPO / "systems" / "ams.yaml"
ENV = ("AMS_SD_NOCARD", "AMS_SD_MOUNT", "AMS_BUSOFF_IFACE")


class _Bench:
    def __init__(self, system):
        self.system = system
        self.commands = []

    def on(self, machine, command):
        self.commands.append((machine, command))
        return "1" if command.endswith(" Flush") else ""


@pytest.fixture
def env(monkeypatch):
    for name in ENV:          # the operator sets them; restored after the test
        monkeypatch.setenv(name, "")


def _bench():
    params, dirs = op.prepare(AMS)
    system = System(AMS, extra_card_dirs=dirs)
    for name, values in params.items():
        system.set_params(name, values)
    return _Bench(system)


def test_the_card_detect_comes_from_the_pin_model():
    """MICROSD_DET on PE3 with its 47k pull-up: HIGH with the slot empty."""
    assert op.card_detect(System(AMS), "ams", "SDMMC1") == ("sysbus.gpioPortE", 3, True)


def test_every_step_names_an_ifs_hil_case():
    for key, (before, after) in op.STEPS.items():
        assert key.startswith(op.TESTS) and "::test_" in key
        assert {before, after} <= {None, "card_out", "card_in", "wipe_card", "read_card"}


def test_an_ecu_system_gets_no_steps(env):
    bench = _Bench(System(REPO / "systems" / "ecu.yaml"))
    o = op.Operator(bench)
    assert o.card is None and not o.busoff and o.env == {}
    assert all(os.environ[name] == "" for name in ENV)
    o.step("card_out")
    assert bench.commands == []


@pytest.mark.skipif(not (shutil.which("mkfs.fat") and shutil.which("mcopy")),
                    reason="needs dosfstools and mtools")
def test_the_ams_card_is_pulled_read_and_put_back(env):
    bench = _bench()
    o = op.Operator(bench)
    assert os.environ["AMS_SD_NOCARD"] == "1"
    assert os.environ["AMS_SD_MOUNT"] == str(o.mount)
    assert os.environ["AMS_BUSOFF_IFACE"] == "can2"          # systems/ams.yaml can_acu
    card = "sysbus.sdmmc1.sd"
    drive = 'vhil_gpio_ams Drive "sysbus.gpioPortE" 3'

    o.step("card_out")
    assert bench.commands == [("ams", f"{drive} true"), ("ams", f"{card} Respond false"),
                              ("ams", f"{card} Flush")]
    bench.commands.clear()
    o.step("card_in")
    assert bench.commands == [("ams", f"{card} Flush"), ("ams", f"{card} Respond true"),
                              ("ams", f"{drive} false")]

    o.step("wipe_card")                        # the blank card gets its FAT32
    (o.mount.parent / "LOG0000.CSV").write_text("tick_ms\n1\n")
    subprocess.run(["mcopy", "-i", str(o.card.image), str(o.mount.parent / "LOG0000.CSV"),
                    "::/LOG0000.CSV"], check=True, env=dict(os.environ, MTOOLS_SKIP_CHECK="1"))
    o.step("read_card")
    assert sorted(p.name for p in o.mount.iterdir()) == ["LOG0000.CSV"]
    assert (o.mount / "LOG0000.CSV").read_text() == "tick_ms\n1\n"
    assert not o.card.out

    o.step("wipe_card")
    o.step("read_card")
    assert list(o.mount.iterdir()) == []


def test_the_bus_off_is_the_ams_fdcan1_fault_hook(env):
    o = op.Operator(_Bench(System(AMS)))      # a card without an image: no card steps
    bench = o.bench
    bench.commands.clear()
    o.inject_busoff("can2", {})
    assert bench.commands == [("ams", "sysbus.fdcan1_h7 ForceBusOff")]
