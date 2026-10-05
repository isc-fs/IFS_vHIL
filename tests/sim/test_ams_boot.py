"""ams-boot (#25): from reset to a healthy Start, in virtual time, outputs
watched from the first instruction.

AMS facts (IFS08-CE-AMS):
  PB4 AMS_OK, PB5 AIR+, PB6 AIR-, PB7 precharge (main.h:74-81), driven LOW
  at GPIO init; TSMS PF9 and DASH_CHG PF10 are inputs with pull-downs
  (main.c:739-742).
  AMS_OK stays LOW for SafetyBootGraceMs (2000) and goes HIGH in a healthy
  Start (safety_predicates.hpp:170, safety_task.cpp).
  0x4A0 / 0x4A1 / 0x4A2 every 500 ms (ams_status.def, ams_pack.def,
  ams_temps.def): 0x4A0 state, ams_ok, online mask, BE min / max cell;
  0x4A1 LE u32 pack mV; 0x4A2 i8 min / max / avg temp, [5] cockpit byte
  (bit 7 sentinel, 3:2 mode lock, 1 TSMS, 0 DASH_CHG; safety_task.cpp:400),
  [7] heartbeat. 0x4A4 every 100 ms: relay / AMS_OK read-backs in byte 0,
  bytes 1-7 reserved (relay_status.def).
  Pit-diag (armed by 0x7F0 DE AD BE EF): 0x6C4 jump reason, init progress,
  FDCAN1 start result (pit_boot_diag.def); 0x6C6 version, git hash, node id
  from __firmware_info (pit_fw_id.def, firmware_info.cpp; AmsNodeId = 2).
"""
import subprocess

import pytest

from vhil.sim import Sim
from vhil.system import REPO

STATUS, PACK, TEMPS, RELAYS = 0x4A0, 0x4A1, 0x4A2, 0x4A4
PIT_ARM, PIT_BOOT, PIT_FWID = 0x7F0, 0x6C4, 0x6C6
GPIOB, GPIOF = "sysbus.gpioPortB", "sysbus.gpioPortF"
AMS_OK, AIR_P, AIR_N, PRECHARGE = 4, 5, 6, 7
TSMS, DASH_CHG = 9, 10
GPIOF_PUPDR = 0x5802140C        # GPIOF base + 0x0C (stm32h733xx.h)
GRACE_MS = 2000
BOOT_MS = 4000
CELLS, CELL_MV = 95, 3700


@pytest.fixture(scope="module")
def booted(make_sim):
    sim = make_sim("ams", wait_for_app=False)     # watched from power-on
    io = sim.io("ams")
    pins = {n: io.watch(GPIOB, p) for n, p in
            {"ok": AMS_OK, "air_p": AIR_P, "air_n": AIR_N, "pre": PRECHARGE}.items()}
    sim.wait_for_app()
    sim.run_for(ms=BOOT_MS)
    sim.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))
    sim.run_for(ms=1100)
    return sim, pins


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=BOOT_MS)
        yield sim


def test_relays_stay_open_through_boot(booted):
    """A-001: no contactor coil is ever driven HIGH from reset to Start."""
    sim, pins = booted
    io = sim.io("ams")
    for name in ("air_p", "air_n", "pre"):
        assert [e for e in io.edges(pins[name]) if e.level] == [], f"{name} driven HIGH during boot"


def test_ams_ok_is_low_in_grace_then_high(booted):
    """C-046, C-047: AMS_OK LOW for the 2 s grace, HIGH once the healthy
    Start is confirmed, and it stays HIGH."""
    sim, pins = booted
    ok = sim.io("ams").edges(pins["ok"])
    assert [e.level for e in ok] == [True], f"AMS_OK edges: {ok}"
    # From the app's start, after the bootloader's window; app_started is
    # known to the ms (the app's HAL tick), so the grace is too.
    t_ms = (ok[0].t_us - sim.app_started["ams"]) / 1000
    assert GRACE_MS - 1 <= t_ms <= GRACE_MS + 50, f"AMS_OK HIGH {t_ms} ms into the app"


def test_the_first_status_frame_reports_start(booted):
    """A-004, S-144: the first 0x4A0 is in Start, well inside 4 s of the app's
    start (after the bootloader's 2 s window), with the
    SD card fitted (its init is off the boot path)."""
    sim, _ = booted
    first = sim.can("can_acu").frames(STATUS)[0]
    assert first.data[0] == 0, f"first 0x4A0 state {first.data[0]}"
    t_ms = (first.t_us - sim.app_started["ams"]) / 1000
    assert t_ms <= 1000, f"first 0x4A0 {t_ms} ms into the app"


def test_status_reports_every_module_and_cell(booted):
    """A-005, E-060: after discovery every module is online, the cells read
    their model value and AMS_OK is reported."""
    sim, _ = booted
    d = sim.can("can_acu").last(STATUS).data
    assert (d[0], d[1], d[2]) == (0, 1, 0x1F), f"state/ams_ok/mask {d[0]}/{d[1]}/0x{d[2]:02X}"
    assert int.from_bytes(d[4:6], "big") == CELL_MV and int.from_bytes(d[6:8], "big") == CELL_MV


def test_pack_voltage_is_the_sum_of_cells(booted):
    """A-006."""
    sim, _ = booted
    pack = int.from_bytes(sim.can("can_acu").last(PACK).data[0:4], "little")
    assert pack == CELLS * CELL_MV, f"0x4A1 pack {pack} mV"


def test_one_cell_moves_the_pack_by_its_own_change(ams):
    """A-006: +100 mV on one cell is +100 mV on the pack."""
    ams.monitor("sysbus.spi1.isospi.cells3 SetCell 2 3800", board="ams")
    ams.run_for(ms=1000)
    pack = int.from_bytes(ams.can("can_acu").last(PACK).data[0:4], "little")
    assert pack == CELLS * CELL_MV + 100, f"0x4A1 pack {pack} mV"


def test_temperatures_and_heartbeat(booted):
    """A-007: min / max / avg at the models' 25 C; the heartbeat advances by
    one per frame."""
    sim, _ = booted
    frames = sim.can("can_acu").frames(TEMPS)
    d = frames[-1].data
    temps = [int.from_bytes(d[i:i + 1], "little", signed=True) for i in range(3)]
    assert all(abs(t - 25) <= 1 for t in temps), f"0x4A2 min/max/avg {temps}"
    beats = [f.data[7] for f in frames]
    assert all((b - a) % 256 == 1 for a, b in zip(beats, beats[1:])), f"heartbeats {beats}"


@pytest.mark.parametrize("can_id", [STATUS, PACK, TEMPS])
def test_telemetry_every_500ms(booted, can_id):
    """A-008 (short form: the 60 s soak belongs to ams-can)."""
    sim, _ = booted
    t = [f.t_us for f in sim.can("can_acu").frames(can_id)]
    deltas = [b - a for a, b in zip(t[1:], t[2:])]
    assert len(deltas) >= 6 and all(abs(d - 500_000) <= 10_000 for d in deltas), deltas


def test_relay_status_every_100ms_reserved_zero(booted):
    """A-014: 0x4A4 at 10 Hz, every contactor read back open, reserved
    bytes zero."""
    sim, _ = booted
    frames = sim.can("can_acu").frames(RELAYS)
    t = [f.t_us for f in frames[1:]]
    assert all(abs((b - a) - 100_000) <= 5_000 for a, b in zip(t, t[1:])), "0x4A4 not at 100 ms"
    last = frames[-1].data
    assert last[0] & 0x07 == 0, f"contactor read-back 0x{last[0]:02X}"
    assert bytes(last[1:]) == bytes(7), f"reserved bytes {bytes(last[1:]).hex()}"


def test_cockpit_byte_sentinel_with_undriven_inputs(booted):
    """A-010, K-102, C-045: bit 7 set, mode Undecided, TSMS and DASH_CHG
    low with nothing driving them."""
    sim, _ = booted
    assert sim.can("can_acu").last(TEMPS).data[5] == 0x80


def test_cockpit_inputs_have_pull_downs(booted):
    """C-045: PF9 / PF10 PUPDR = 0b10 (pull-down)."""
    sim, _ = booted
    pupdr = int(sim.monitor(f"sysbus ReadDoubleWord {GPIOF_PUPDR:#x}", board="ams").strip(), 16)
    for pin in (TSMS, DASH_CHG):
        assert (pupdr >> (2 * pin)) & 3 == 0b10, f"PF{pin} PUPDR {(pupdr >> (2 * pin)) & 3:#04b}"


def test_cockpit_byte_follows_the_inputs(ams):
    """K-102: TSMS on bit 1, DASH_CHG on bit 0, the sentinel kept."""
    io = ams.io("ams")
    io.set_input(GPIOF, TSMS, True)
    ams.run_for(ms=600)
    assert ams.can("can_acu").last(TEMPS).data[5] & 0x83 == 0x82
    io.set_input(GPIOF, TSMS, False)
    io.set_input(GPIOF, DASH_CHG, True)
    ams.run_for(ms=600)
    assert ams.can("can_acu").last(TEMPS).data[5] & 0x83 == 0x81


def test_every_producer_is_alive(booted):
    """M-040, M-041: each task's frames in the last second."""
    sim, _ = booted
    since = sim.now_us() - 1_000_000
    missing = [hex(i) for i in (STATUS, PACK, TEMPS, RELAYS, 0x135)
               if sim.can("can_acu").count(i, since_us=since) == 0]
    assert not missing, f"silent producers: {missing}"


def test_boot_diag_reports_a_clean_start(booted):
    """G-097: cold boot -> jump reason 0, init progress 7, FDCAN1 start OK."""
    sim, _ = booted
    d = sim.can("can_acu").last(PIT_BOOT).data
    assert int.from_bytes(d[0:4], "little") == 0, f"jump reason {d[0:4].hex()}"
    assert d[4] == 7, f"init progress {d[4]}"
    assert int.from_bytes(d[5:8], "little") == 0, f"FDCAN1 start {d[5:8].hex()}"


def test_firmware_identity(booted, firmware):
    """A-009, A-013: version from the source's VERSION file, node id 2, and
    the git hash of the checkout the image was built from."""
    sim, _ = booted
    d = sim.can("can_acu").last(PIT_FWID).data
    src = firmware("ams").resolve().parent.parent
    version = tuple(int(x) for x in (src / "VERSION").read_text().strip().split("."))
    assert tuple(d[0:3]) == version, f"0x6C6 version {tuple(d[0:3])}, VERSION {version}"
    assert d[7] == 0x02, f"node id {d[7]}"
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode == 0:
        assert bytes(d[3:7]).hex() == head.stdout.strip()[:8], "git hash differs from the checkout"
