"""The bench operator's steps for IFS_HIL's operator-gated cases (#152).

Some IFS_HIL cases run on bench-01 only when an operator does something by
hand and sets an env var: pulls the AMS's microSD card (AMS_SD_NOCARD), reads
it on a PC (AMS_SD_MOUNT), or puts a second CAN adapter on the bus to drive
the AMS bus-off (AMS_BUSOFF_IFACE). The virtual bench sets the env var and
does the step itself, where the test expects the operator to, so IFS_HIL's
tests run unmodified:

  card out   S-142, S-143 (the card OUT): card detect goes to the slot's
             empty level and the card stops answering; it goes back in after
             the test.
  wipe card  U-160 (the block's first step): the card comes out, gets a fresh
             FAT32 and goes back in, into the running AMS, as an operator
             wiping it on a PC would.
  read card  U-161, U-162: the card comes out and its files are copied to
             AMS_SD_MOUNT, what a PC mounting it sees; then it goes back in.
  bus-off    J-130, J-131: the test module's _inject_busoff (a bench bring-up
             stub that raises) forces the AMS's FDCAN1 bus-off through the
             FDCAN fault hook (models/renode/Stm32H7Fdcan.cs, ForceBusOff).

A card the virtual bench can pull is backed by an image file. A system whose
card has none gets a blank one, which the firmware can't mount (it does not
format cards): its logger stays idle, as with the in-memory card the system
had before, until U-160's wipe gives it a FAT32. A card that logs from the
first test would be the car's, but each boot's index scan and orphan sealing
grow with the files the suite's ~100 boots leave on it, and that host time
puts the wall-clock suite behind real time (#154): with 800 files a boot
costs ~1.5 s of extra host time on a fast machine. The logger's own
behaviour is covered natively (tests/sim/test_ams_sd.py).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from vhil import renode as rn
from vhil.broker import PROBE_SOURCE, probe_name
from vhil.system import System

log = logging.getLogger("vhil.bench_operator")

TESTS = "tests/hil/ams/"
# IFS_HIL test -> (step before it, step after it). Keys as configs/gaps.yaml's:
# the path in the IFS_HIL checkout, then the test function's name.
STEPS = {
    TESTS + "test_block_s_sdcard.py::test_s142_bl_recovery_no_card": ("card_out", "card_in"),
    TESTS + "test_block_s_sdcard.py::test_s143_no_card_boots": ("card_out", "card_in"),
    TESTS + "test_block_u_microsd_multirun.py::test_u160_mask_stable_across_reboots":
        ("wipe_card", None),
    TESTS + "test_block_u_microsd_multirun.py::test_u161_file_per_run_no_fragmentation":
        ("read_card", None),
    TESTS + "test_block_u_microsd_multirun.py::test_u162_no_truncation_and_sealed":
        ("read_card", None),
}
# The modules whose bus-off injection stub the bench replaces.
BUSOFF_MODULES = (TESTS + "test_block_j_busoff.py",)
# The AMS's controller IFS_HIL's Block J drives bus-off (its docstring).
BUSOFF_CONTROLLER = "FDCAN1"


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} not installed (dosfstools / mtools): the virtual "
                           f"bench needs it for the AMS's card image")
    return path


def format_card(image: Path) -> None:
    """A fresh FAT32 on the card image, as the native tests format theirs
    (tests/sim/test_ams_sd.py)."""
    subprocess.run([_tool("mkfs.fat"), "-F", "32", "-s", "1", "-n", "AMS", str(image)],
                   check=True, capture_output=True)


def _ams_boards(system: System) -> list[str]:
    return [name for name, b in system.boards.items() if b.role == "ams"]


def _cards(system: System) -> list[str]:
    """The sd-card devices on an AMS board."""
    ams = set(_ams_boards(system))
    return [name for name, dev in system.devices.items()
            if dev["model"] == "sd-card" and dev["sdmmc"].split(".", 1)[0] in ams]


def prepare(system_path: Path) -> tuple[dict[str, dict], list[Path]]:
    """Before the bench starts: a blank image for each AMS card that has
    none. Returns the bench's params and card directories."""
    system = System(system_path)
    params: dict[str, dict] = {}
    dirs: list[Path] = []
    for name in _cards(system):
        dev = system.devices[name]
        values = {**dev["model_doc"].get("params", {}), **dev.get("params", {})}
        if values.get("image"):
            continue
        directory = Path(tempfile.mkdtemp(prefix=f"vhil-{rn.ident(name)}-"))
        image = directory / "card.img"
        with open(image, "wb") as f:
            f.truncate(int(values["capacity"]))
        params[name] = {"image": str(image)}
        dirs.append(directory)
    return params, dirs


def card_detect(system: System, board: str, sdmmc: str) -> tuple[str, int, bool] | None:
    """(GPIO port, pin, level with the slot empty) of an SD interface's card
    detect, from the board's pin model (interfaces.sdmmc: the MainLite's
    MICROSD_DET on PE3, 47k pull-up R21); None if it has none."""
    model = system.boards[board].pin_model
    if model is None:
        return None
    for iface in (model.doc.get("interfaces") or {}).get("sdmmc") or []:
        cd = iface.get("card_detect")
        if iface.get("instance") == sdmmc and cd:
            port = cd["port"]                               # e.g. "PE3"
            up = any(p.get("direction") == "up" for p in cd.get("pulls") or [])
            return f"sysbus.gpioPort{port[1]}", int(port[2:]), up
    return None


class Card:
    """An AMS's microSD card, as the bench operator handles it."""

    def __init__(self, bench, name: str):
        self.bench = bench
        system = bench.system
        dev = system.devices[name]
        board, _, sdmmc = system.resolve(dev["sdmmc"])
        self.machine = board.name
        self.path = f"{sdmmc}.{rn.ident(name)}"
        self.image = system.card_image(dev["params"]["image"])
        self.detect = card_detect(system, board.name, dev["sdmmc"].split(".", 1)[1])
        self.out = False
        if self.detect is not None:
            self._ensure_probe()

    def _ensure_probe(self) -> None:
        """The board's GPIO probe drives card detect; the broker made one
        only for a board with routed GPIOs (vhil/broker.py, probe_commands)."""
        config = self.bench.system.bench_config()
        routed = {r["machine"] for r in config.get("gpio_routes", []) + config.get("adc_routes", [])}
        if self.machine in routed:
            return
        if not routed:
            self.bench.on(self.machine, f"include {rn.file_arg(PROBE_SOURCE)}")
        self.bench.on(self.machine, f"emulation CreateVhilGpioProbe "
                                    f"{rn.quote(probe_name(self.machine))} {rn.quote(self.machine)}")

    def _detect(self, present: bool) -> None:
        if self.detect is None:
            return
        port, pin, empty = self.detect
        level = (not empty) if present else empty
        self.bench.on(self.machine, f"{probe_name(self.machine)} Drive {rn.quote(port)} "
                                    f"{int(pin)} {str(level).lower()}")

    def _flush(self) -> None:
        reply = self.bench.on(self.machine, f"{self.path} Flush").strip()
        try:
            flushed = int(reply.split()[-1], 0)
        except (IndexError, ValueError):
            raise RuntimeError(f"{self.path} Flush: {reply!r}") from None
        if flushed < 1:
            raise RuntimeError(f"{self.path} has no image file to flush")

    def pull(self) -> None:
        """Out of the slot: card detect at its empty level, the card silent
        (so the firmware writes no more), the image file up to date."""
        if self.out:
            return
        self._detect(False)
        self.bench.on(self.machine, f"{self.path} Respond false")
        self._flush()
        self.out = True
        log.info("%s: card pulled", self.machine)

    def insert(self) -> None:
        if not self.out:
            return
        self._flush()               # the host may have rewritten the image
        self.bench.on(self.machine, f"{self.path} Respond true")
        self._detect(True)
        self.out = False
        log.info("%s: card inserted", self.machine)

    def wipe(self) -> None:
        self.pull()
        format_card(self.image)
        self.insert()

    def read(self, mount: Path) -> list[str]:
        """Copy every file on the card to `mount`, as a PC mounting it sees
        them. The card must be out."""
        env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
        listing = subprocess.run([_tool("mdir"), "-i", str(self.image), "-b", "::/"],
                                 check=True, capture_output=True, text=True, env=env).stdout
        names = [line.strip().split("/")[-1] for line in listing.splitlines() if line.strip()]
        if mount.exists():
            shutil.rmtree(mount)
        mount.mkdir(parents=True)
        for name in names:
            subprocess.run([_tool("mcopy"), "-n", "-i", str(self.image), f"::/{name}",
                            str(mount / name)], check=True, capture_output=True, env=env)
        log.info("%s: card read to %s: %s", self.machine, mount, " ".join(sorted(names)))
        return names


class Operator:
    """Sets the env vars IFS_HIL's gated cases read, and does their steps."""

    def __init__(self, bench):
        self.bench = bench
        # The one AMS card, if prepare() gave it an image to read.
        cards = [c for c in _cards(bench.system)
                 if bench.system.devices[c].get("params", {}).get("image")]
        self.card = Card(bench, cards[0]) if len(cards) == 1 else None
        self.mount = Path(tempfile.mkdtemp(prefix="vhil-sd-mount-")) / "card"
        self.busoff: list[tuple[str, str]] = []           # (machine, controller)
        netdev = None
        for board in _ams_boards(bench.system):
            controller = bench.system.boards[board].board.get("can", {}).get(BUSOFF_CONTROLLER)
            node = f"{board}.{BUSOFF_CONTROLLER}"
            for bus_name, bus in bench.system.buses.items():
                if controller and node in bus.get("nodes", []):
                    self.busoff.append((board, controller))
                    netdev = netdev or bus.get("host_netdev") or bus_name

        self.env: dict[str, str] = {}
        if self.card is not None:
            self.env["AMS_SD_NOCARD"] = "1"
            self.env["AMS_SD_MOUNT"] = str(self.mount)
        if self.busoff:
            self.env["AMS_BUSOFF_IFACE"] = netdev
        os.environ.update(self.env)

    def step(self, name: str | None) -> None:
        """Do one of STEPS' steps; every one of them handles the card."""
        if name is None or self.card is None:
            return
        {"card_out": self.card.pull, "card_in": self.card.insert,
         "wipe_card": self.card.wipe, "read_card": self._read_card}[name]()

    def _read_card(self) -> None:
        self.card.pull()
        self.card.read(self.mount)
        self.card.insert()

    def inject_busoff(self, iface=None, ams_profile=None) -> None:
        """Stands in for IFS_HIL Block J's _inject_busoff(iface, ams_profile):
        the AMS's FDCAN1 enters bus-off as a corrupted bus drives it there."""
        for machine, controller in self.busoff:
            self.bench.on(machine, f"{controller} ForceBusOff")
            log.info("%s: %s forced bus-off", machine, controller)
