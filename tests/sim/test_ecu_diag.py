"""ecu-diag (#46): the ECU's diagnostic and telemetry outputs, in virtual time.

ECU facts (IFS08-CE-ECU; Core/Src/app/ unless shown otherwise):
  Contract: every frame the ECU owns or consumes is one CAN_MSG(name, id, dlc,
    sender, period_ms) in a .def, listed in Core/Inc/can/messages/
    all_messages.inc; the DBC is generated from it. FIELD_LE/_BE(_S)(name,
    type, byte, len, ...) start at 8*byte (Motorola: 8*byte+7), FIELD_*_BITS
    at a bit; get_le/get_be walk the bits as Core/Inc/can/can_dsl.hpp:31-58.
    The contract and constants (ecu_config.hpp) are read from the source the
    image was built from, so the suite follows the firmware version.
  Pit-diag: 0x7E0 on FDCAN2, DLC >= 4, BE magic 0xDEADBEEF sets the gate,
    anything else clears it; either answers 0x7E1 [enabled]
    (can_rx_task.cpp:79-90). Extended IDs are rejected at the filter
    (app_init_task.cpp:48-52). While the gate is set, ControlTask posts the
    13 frames 0x700-0x70D (not 0x704) in one tick every PitDiagStreamMs = 100
    (control_task.cpp:377-396, ecu_config.hpp:24); last_pit starts at 0, so
    an arm long after the last frame streams on the next tick.
  0x704 health is DiagTask's, every DiagPeriodMs = 1000, ungated
    (diag_task.cpp:38-68); its stub bits mirror ecu_config's toggles
    (pit_diag.cpp:288-303).
  0x703 = __firmware_info's version and git_hash[0..3] (pit_diag.cpp:258-271,
    firmware_info.cpp:69-94), the record at 0x08020400.
  Inverter mirrors (vehicle_service.cpp:137-233): 0x461 App_State bits 32-38,
    DEM_Code low byte = byte 2, DEM_Present bit 31, L2 bits 39-46, L1 bits
    47-55; 0x463 Current_D/Q LE16 bytes 0-3, Volt_Modulus 12 bits at 32,
    erpm 20-bit signed at bit 44; 0x464 four raw temperatures (degC + 50);
    0x465 cmd_src/ctrl_type/ctrl_mode; 0x466 DC bus 10 bits at 16; 0x467
    Torque_Max_Feas LE16 byte 0, Setpoint_Q LE16 byte 4; 0x468 Torque_Est LE16
    byte 2. Shaft rpm = erpm / MotorPolePairs (10), truncated toward zero, on
    0x702 and on 0x506 every ControlTask tick (udv_tx.cpp:42-52,
    control_task.cpp:330). 0xFF is the inverter's disconnected-sensor
    sentinel; 0x464 silent for InvTempsStaleMs = 500 makes both invalid
    (motor_thermal.cpp:22-41, control_task.cpp:184-185).
  Dash, FDCAN3: TelemetryTask posts 0x510-0x521 every 200 ms, hand-packed LE
    (telemetry_task.cpp:54-178, 298-327); 0x515/0x516/0x519/0x51A and
    0x51B[0:4] are declared placeholders (telemetry_task.cpp:104-137).
  GPS: MTK3339 on USART10. GpsTask sends PMTK314 (RMC+GGA only) and PMTK220,200
    at start, parses RMC (fix, lat, lon, knots -> km/h, course) and GGA (fix,
    sats), and posts 0x508/0x509 every GpsTxPeriodMs = 200 whether or not a
    sentence arrived (gps_task.cpp:102-143, gps_nmea.cpp:153-215,
    gps_tx.cpp:31-52).

Model: the GPS is bytes into USART10's RX (Renode's STM32F7_USART WriteLine),
which is what the module's TX line does; the PMTK commands are read back from
the same UART's TX history.

Drift and findings:
  - 0x511 VCU_r2d_confirm is declared acyclic (period 0, "emitted on the DV
    R2D transition", vcu_r2d_confirm.def) but goes out every 100 ms with the
    latch as value (control_task.cpp:335-337); the generated DBC's cycle time
    is stale.
  - IFS_HIL#128 (0x703 absent on bench-01) reproduces on the bus model: a
    stream tick on the uDV tick posts 18 ACU frames into FDCAN2's 16-deep TX
    FIFO (fdcan.c:115) faster than the bus drains it, and the HAL's refusal
    is ignored (can_tx_task.cpp:50-52), so 0x703 and 0x705 never go out
    (isc-fs/IFS08-CE-ECU#251; strict xfails below). Renode's hub, which
    delivered every frame at once, hid it.
  - 0x508/0x509 keep 200 ms only while GpsTask wakes on time: last_tx = now
    (gps_task.cpp:135) turns any late wake into a 220 ms interval at the next
    20 ms poll. Seen here every few posts, idle or loaded; how often on the
    car depends on CPU load Renode does not time cycle-accurately.
  - The first dash burst is off the 200 ms grid (tick 8: the task's tick base
    is taken before NRF24 bring-up, telemetry_task.cpp:301-308).
  - The NMEA checksum is stripped, not checked (gps_nmea.cpp:156-162).
  - 0x707 motor_rpm_mech is a plain int16 cast of the shaft rpm
    (pit_diag.cpp:67): it wraps above 32767 rpm, far beyond the motor.
"""
import re
from functools import lru_cache

import pytest

from vhil import candef
from vhil.sim import Frame, Sim, assert_cadence, assert_period, intervals_us
from vhil.system import REPO

PIT_CMD, PIT_ACK, HEALTH = 0x7E0, 0x7E1, 0x704
FWINFO, HEARTBEAT, MOTOR_RPM = 0x703, 0x100, 0x506
GPS_POSITION, GPS_STATUS = 0x508, 0x509
ENABLE, DISABLE = bytes.fromhex("DEADBEEF"), bytes(4)
INV_STATE, INV_RPM, INV_TEMPS, INV_MODE, INV_VDC, INV_TQLIM, INV_TQEST = (
    0x461, 0x463, 0x464, 0x465, 0x466, 0x467, 0x468)
DASH = {0x510: 8, 0x511: 6, 0x512: 6, 0x513: 6, 0x514: 4, 0x515: 4, 0x516: 4,
        0x517: 2, 0x518: 7, 0x519: 8, 0x51A: 8, 0x51B: 8, 0x51C: 6, 0x51D: 4,
        0x51E: 6, 0x51F: 4, 0x520: 6, 0x521: 6}       # telemetry_task.cpp:63-177
DASH_PERIOD_MS = 200                                  # telemetry_task.cpp:35
FWINFO_ADDR = 0x08020400                              # firmware_info.cpp:15-16
TICK_MS = 10
KERNEL_TICK_US = 1000      # configTICK_RATE_HZ 1000 (FreeRTOSConfig.h:67)
TX_FIFO_DEPTH = 16         # FDCAN2 TxFifoQueueElmtsNbr (fdcan.c:115)
BOOT_MS = 300


# -- the contract, from the built source ------------------------------------------

def _contract(src):
    """{id: Message} of every message all_messages.inc includes (vhil/candef.py)."""
    out = candef.load(src)
    assert out, f"no messages declared under {src / candef.MESSAGES}"
    return out


@lru_cache(maxsize=4)
def _config_text(src):
    return (src / "Core" / "Inc" / "app" / "ecu_config.hpp").read_text()


def _config(src, name):
    m = re.search(rf"\b{name}\s*=\s*([\w.]+)", _config_text(src))
    assert m, f"{name} not in ecu_config.hpp"
    v = m.group(1)
    return {"true": True, "false": False}.get(v) if v in ("true", "false") else int(v.rstrip("uU"), 0)


@pytest.fixture(scope="module")
def src(firmware):
    return firmware("ecu").resolve().parent.parent


@pytest.fixture(scope="module")
def contract(src):
    return _contract(src)


@pytest.fixture(scope="module")
def stream_ids(src, contract):
    """The pit-diag stream: the ECU's 0x7xx frames at PitDiagStreamMs."""
    period = _config(src, "PitDiagStreamMs")
    ids = sorted(i for i, m in contract.items()
                 if m.sender == "VCU" and 0x700 <= i < 0x7E0 and m.period_ms == period)
    assert ids, "no pit-diag stream frames in the contract"
    return ids


# -- stimulus ------------------------------------------------------------------

def _put_le(buf, start, length, value):
    for i in range(length):
        bit = start + i
        buf[bit >> 3] = (buf[bit >> 3] & ~(1 << (bit & 7))) | (((value >> i) & 1) << (bit & 7))


def _frame(dlc, **fields):
    """An inverter frame from {name: (start_bit, length, value)}, LE (vendor DBC @1)."""
    buf = bytearray(dlc)
    for start, length, value in fields.values():
        _put_le(buf, start, length, value & ((1 << length) - 1))
    return bytes(buf)


def _inv_state(state, dem_code=0, dem_present=False, l2=0, l1=0):
    return _frame(8, dem=(16, 15, dem_code), present=(31, 1, int(dem_present)),
                  state=(32, 7, state), l2=(39, 8, l2), l1=(47, 9, l1))


def _inv_rpm(erpm, current_d=0, current_q=0, modulus=0):
    return _frame(8, d=(0, 16, current_d), q=(16, 16, current_q), mod=(32, 12, modulus),
                  erpm=(44, 20, erpm))


def _be16(*values):
    return b"".join((v & 0xFFFF).to_bytes(2, "big") for v in values)


def _nmea(body):
    cs = 0
    for c in body.encode():
        cs ^= c
    return f"${body}*{cs:02X}"


def _gps(sim, *bodies):
    """The module's TX line: whole sentences into USART10, CR LF ended."""
    for body in bodies:
        sim.monitor(f'sysbus.usart10 WriteLine "{_nmea(body)}" CRLF', board="ecu")


def _offers(bus, ids, since_us):
    """id -> the frames with that id the bus model saw the ECU offer from
    since_us on, each stamped with its offer (TXBAR) time: when the firmware
    handed it to FDCAN, whatever the bus then made it wait."""
    out = {}
    for r in bus.timeline(since_us=since_us):
        if r.id in ids and r.node.startswith("ecu:"):
            out.setdefault(r.id, []).append(Frame(r.offer_ns // 1000, r.id, r.extended, r.data))
    return out


def _arm(sim, payload=ENABLE, bus="can_acu", extended=False):
    t = sim.now_us()
    sim.can(bus).send(PIT_CMD, payload, extended=extended)
    return t


# -- scenario: armed, every input live ---------------------------------------------

INV = dict(state=3, dem_code=5, dem_present=True, l2=0x21, l1=0x023,
           erpm=123456, current_d=-320, current_q=640, modulus=512,
           temps=(70, 80, 90, 95), cmd_src=2, ctrl_type=1, ctrl_mode=3,
           vdc=350, tq_max_feas=1200, setpoint_q=-96, tq_est=-45)
AMS = dict(v_cell_min=3712, accu_dA=-123, dcdc_dA=45, vmin=(3700, 3701, 3702, 3703, 3704),
           vmax=(4100, 4101, 4102, 4103, 4104), tmax=(30, 31, 32, 33, 34), tmax_dcdc=41,
           fsm=3)
RMC = "GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"
GGA = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
PEDALS_V = {"PF8": 0.8, "PF9": 1.1, "PF7": 0.4}       # APPS1, APPS2, brake (ecu.yaml)
LIVE_MS = 2000


def _feed_inverter(sim):
    inv, i = sim.can("can_inv"), INV
    inv.send_periodic("state", INV_STATE, _inv_state(i["state"], i["dem_code"], i["dem_present"],
                                                     i["l2"], i["l1"]), 10)
    inv.send_periodic("rpm", INV_RPM, _inv_rpm(i["erpm"], i["current_d"], i["current_q"],
                                               i["modulus"]), 10)
    inv.send_periodic("temps", INV_TEMPS, bytes(i["temps"]) + bytes(4), 100)
    inv.send_periodic("mode", INV_MODE, bytes([i["ctrl_type"] << 4 | i["cmd_src"],
                                               i["ctrl_mode"]]) + bytes(6), 10)
    inv.send_periodic("vdc", INV_VDC, _frame(8, vdc=(16, 10, i["vdc"])), 10)
    inv.send_periodic("tqlim", INV_TQLIM, _frame(8, feas=(0, 16, i["tq_max_feas"]),
                                                 sq=(32, 16, i["setpoint_q"])), 10)
    inv.send_periodic("tqest", INV_TQEST, _frame(8, est=(16, 16, i["tq_est"])), 10)


def _feed_ams(sim):
    acu, a = sim.can("can_acu"), AMS
    acu.send_periodic("okp", 0x020, b"\x01", 10)
    acu.send_periodic("vmin", 0x12C, _be16(a["v_cell_min"]), 100)
    acu.send_periodic("status", 0x4A0, bytes([a["fsm"], 1, 0x1F, 0]) + _be16(a["v_cell_min"], 0), 100)
    acu.send_periodic("cur", 0x135, _be16(a["accu_dA"], a["dcdc_dA"]), 50)
    acu.send_periodic("vmin_a", 0x131, _be16(*a["vmin"][:3]), 100)
    acu.send_periodic("vmin_b", 0x132, _be16(*a["vmin"][3:]), 100)
    acu.send_periodic("vmax_a", 0x133, _be16(*a["vmax"][:3]), 100)
    acu.send_periodic("vmax_b", 0x134, _be16(*a["vmax"][3:]), 100)
    acu.send_periodic("tmax_a", 0x136, _be16(*a["tmax"][:3]), 100)
    acu.send_periodic("tmax_b", 0x137, _be16(*a["tmax"][3:], a["tmax_dcdc"]), 100)


@pytest.fixture(scope="module")
def live(make_sim):
    """Booted quiet for BOOT_MS, then pit-diag armed with the inverter, the AMS
    and the GPS all talking, for LIVE_MS."""
    sim = make_sim("ecu")
    sim.run_for(ms=BOOT_MS)
    t_arm = _arm(sim)
    _feed_inverter(sim)
    _feed_ams(sim)
    _gps(sim, RMC, GGA)
    io = sim.io("ecu")
    for pin, volts in PEDALS_V.items():
        io.set_voltage(pin, volts)
    sim.run_for(ms=LIVE_MS)
    return sim, t_arm


@pytest.fixture
def ecu(images):
    with Sim(REPO / "systems" / "ecu.yaml", images("ecu")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=BOOT_MS)
        yield sim


def _latest(sim, msg, bus="can_acu"):
    f = sim.can(bus).last(msg.id)
    assert f is not None, f"no {msg.name} ({msg.id:#x})"
    return msg.decode(f.data)


# -- pit-diag gate -------------------------------------------------------------

def test_nothing_streams_before_the_arm(live, stream_ids):
    sim, t_arm = live
    early = [f for f in sim.can("can_acu").frames(stream_ids) if f.t_us < t_arm]
    assert not early, f"pit-diag frames before 0x7E0: {early[:3]}"


def test_arm_acks_once_and_streams_every_frame_every_100_ms(live, src, stream_ids):
    """H-001: 0x7E0 DEADBEEF -> one 0x7E1 [1] on the next CanRx wake; then
    all 13 stream frames posted in one tick (offered to the bus within one
    kernel tick: they then go out one after another), every 100 ms on one
    grid, the first on the tick after the ack (last_pit was 0)."""
    sim, t_arm = live
    acu = sim.can("can_acu")
    acks = acu.frames(PIT_ACK, since_us=t_arm)
    assert [a.data for a in acks] == [b"\x01"], f"acks {acks}"
    assert acks[0].t_us - t_arm < TICK_MS * 1000, f"ack {acks[0].t_us - t_arm} us after the arm"
    period_us = _config(src, "PitDiagStreamMs") * 1000
    # The frames that reach the bus; isc-fs/IFS08-CE-ECU#251 drops the
    # last of an 18-frame tick (the next test). FDCAN2's TX FIFO is 16 deep
    # (fdcan.c:115) and at most 5 non-stream frames precede the stream in
    # a tick (control_task.cpp:305-338), so at least 11 stream frames fit.
    offers = _offers(acu, stream_ids, t_arm)
    assert len(offers) >= TX_FIFO_DEPTH - 5, f"only {sorted(map(hex, offers))} reached the bus"
    first = {}
    for can_id, times in offers.items():
        assert_cadence(times, period_us=period_us, jitter_us=KERNEL_TICK_US,
                       min_count=LIVE_MS // 100 - 1)
        first[can_id] = times[0].t_us
    assert max(first.values()) - min(first.values()) < KERNEL_TICK_US, \
        f"stream split across ticks: {first}"
    assert 0 <= min(first.values()) - acks[0].t_us <= TICK_MS * 1000


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-ECU#251: a stream tick on the 100 ms uDV tick posts 18 ACU "
    "frames into FDCAN2's 16-deep TX FIFO (fdcan.c:115); CanTxTask ignores the "
    "refusal (can_tx_task.cpp:50-52), so 0x703 and 0x705 never reach the bus"))
def test_every_stream_frame_reaches_the_bus(live, stream_ids):
    """H-001: all 13 stream frames on the bus, every stream tick."""
    sim, t_arm = live
    offers = _offers(sim.can("can_acu"), stream_ids, t_arm)
    assert set(offers) == set(stream_ids), \
        f"never on the bus: {sorted(map(hex, set(stream_ids) - set(offers)))}"


def test_disarm_acks_and_stops_the_stream_but_not_health(ecu, src, stream_ids):
    """H-002: 0x7E0 zero -> 0x7E1 [0], no stream frame after it; 0x704 keeps
    its second. Re-arming resumes on the next tick."""
    acu = ecu.can("can_acu")
    _arm(ecu)
    ecu.run_for(ms=500)
    t_off = _arm(ecu, DISABLE)
    ecu.run_for(ms=2500)
    acks = acu.frames(PIT_ACK, since_us=t_off)
    assert [a.data for a in acks] == [b"\x00"], f"disarm acks {acks}"
    late = acu.frames(stream_ids, since_us=acks[0].t_us + 1)
    assert not late, f"stream after the disarm ack: {late[:3]}"
    health = acu.frames(HEALTH, since_us=t_off)
    assert_cadence(health, period_us=_config(src, "DiagPeriodMs") * 1000, jitter_us=KERNEL_TICK_US,
                   min_count=2)
    t_on = _arm(ecu)
    ecu.run_for(ms=3 * TICK_MS)
    resumed = {f.id for f in acu.frames(stream_ids, since_us=t_on)}
    # All that fit FDCAN2's TX FIFO: 0x703/0x705 are lost on a uDV tick
    # (isc-fs/IFS08-CE-ECU#251, test_every_stream_frame_reaches_the_bus).
    assert len(resumed) >= TX_FIFO_DEPTH - 5, f"re-arm resumed only {sorted(map(hex, resumed))}"


@pytest.mark.parametrize("payload, bus, extended, ack", [
    (bytes.fromhex("DEADBEEE"), "can_acu", False, b"\x00"),   # not the magic: disarms
    (bytes.fromhex("EFBEADDE"), "can_acu", False, b"\x00"),   # LE magic: not it either
    (bytes.fromhex("DEADBE"), "can_acu", False, None),        # DLC 3: ignored
    (ENABLE, "can_acu", True, None),                          # extended: filtered
    (ENABLE, "can_inv", False, None),                         # inverter bus
    (ENABLE, "can_dash", False, None),                        # dash bus
], ids=["off-by-one", "little-endian", "short", "extended", "fdcan1", "fdcan3"])
def test_only_the_magic_on_fdcan2_arms(ecu, stream_ids, payload, bus, extended, ack):
    acu = ecu.can("can_acu")
    t = _arm(ecu, payload, bus, extended)
    ecu.run_for(ms=300)
    acks = [a.data for a in acu.frames(PIT_ACK, since_us=t)]
    assert acks == ([ack] if ack else []), f"acks {acks}"
    assert acu.count(stream_ids, since_us=t) == 0, "the stream started"


# -- every frame against the contract ------------------------------------------------

def test_every_ecu_frame_matches_its_def(live, contract):
    """H-003: everything the ECU sends on FDCAN2 is a VCU message of the
    contract, at its DLC, with no bit set outside a declared field and every
    enumerated field on a declared value."""
    sim, _ = live
    problems = []
    for f in sim.can("can_acu").frames():
        msg = contract.get(f.id)
        if msg is None or msg.sender != "VCU":
            problems.append(f"{f.id:#x}: not a VCU message of the contract")
            continue
        if len(f.data) != msg.dlc:
            problems.append(f"{msg.name}: DLC {len(f.data)} != {msg.dlc}")
            continue
        if spare := msg.spare_bits(f.data):
            problems.append(f"{msg.name} {f.data.hex()}: undeclared bits {spare}")
        for field, allowed in msg.values.items():
            if (v := msg.raw(f.data, field)) not in allowed:
                problems.append(f"{msg.name}.{field} = {v}, not a declared value")
    assert not problems, "\n".join(sorted(set(problems))[:20])


def test_every_cyclic_frame_keeps_its_declared_period(live, contract):
    """Each ControlTask/DiagTask frame with a period in the .def is offered
    to the bus on one grid of it (osDelayUntil), within a kernel tick. When
    it then goes out depends on the bus: the probe's injected frames win
    arbitration over many of them. GpsTask's are the next test; the PitCal_*
    frames run only inside a calibration session (ecu-cal); 0x703 and 0x705
    never reach the bus (isc-fs/IFS08-CE-ECU#251, the stream test above)."""
    sim, t_arm = live
    acu = sim.can("can_acu")
    off = {}
    for can_id, msg in sorted(contract.items()):
        if (msg.sender != "VCU" or not msg.period_ms or msg.name.startswith("PitCal_")
                or can_id in (GPS_POSITION, GPS_STATUS, FWINFO, 0x705)):
            continue
        frames = _offers(acu, [can_id], t_arm).get(can_id, [])
        grid = [f.t_us - frames[0].t_us - k * msg.period_ms * 1000 for k, f in enumerate(frames)]
        if len(frames) < 2 or max(grid) - min(grid) > KERNEL_TICK_US:
            off[msg.name] = (msg.period_ms, len(frames), grid[:5])
    assert not off, f"(period ms, frames, offsets from the grid us): {off}"


def test_gps_frames_every_200_ms_or_the_next_poll(live, contract, src):
    """0x508/0x509 go out on the first 20 ms poll at least GpsTxPeriodMs after
    the last post (gps_task.cpp:133-139). last_tx takes the late wake's time,
    not += period, so a late wake slips the next post to the following poll:
    intervals are 200..220 ms, not the .def's flat 200 (seen: 217-219 ms
    every few posts, idle or loaded)."""
    sim, t_arm = live
    period = _config(src, "GpsTxPeriodMs") * 1000
    poll = _config(src, "GpsPollPeriodMs") * 1000
    for can_id in (GPS_POSITION, GPS_STATUS):
        d = intervals_us(sim.can("can_acu").frames(can_id, since_us=t_arm))
        assert len(d) >= LIVE_MS // 250, f"{can_id:#x}: {len(d)} intervals"
        bad = [x for x in d if not period - 1000 <= x <= period + poll + 1000]
        assert not bad, f"{can_id:#x} intervals {d}"


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-ECU#251: 0x703 is refused by FDCAN2's full TX FIFO on every "
    "stream tick that falls on the uDV tick, and never reaches the bus"))
def test_fwinfo_is_the_image_record(live, contract, firmware):
    """0x703 carries the version and git hash of the record the bootloader
    validates (A-005's fields), and the hash is not blank."""
    sim, _ = live
    fw = _latest(sim, contract[FWINFO])
    word = lambda off: int(sim.monitor(f"sysbus ReadDoubleWord {FWINFO_ADDR + off:#x}", board="ecu"), 16)
    assert word(0) == 0xF14F1B00, "no fwinfo record at 0x08020400"
    git = word(24).to_bytes(4, "little")
    assert (fw["fw_major"], fw["fw_minor"], fw["fw_patch"]) == (word(8), word(12), word(16))
    assert int(fw["git_hash"]) == int.from_bytes(git, "big") != 0


def test_health_announces_the_build_toggles(live, contract, src):
    """0x704's stub bits mirror ecu_config, so a bench image can't pass for a
    flight one; every task alive in every frame."""
    sim, _ = live
    msg = contract[HEALTH]
    want = {"stub_no_ams": _config(src, "StubNoAms"), "stub_no_inverter": _config(src, "StubNoInverter"),
            "stub_start": _config(src, "StubStart"), "stub_brake": _config(src, "StubBrakeRaw") != 0,
            "stub_torque_cap": _config(src, "TorqueCap") < 100}
    frames = sim.can("can_acu").frames(HEALTH)
    assert len(frames) >= 2
    for f in frames[1:]:                         # the first spans boot
        h = msg.decode(f.data)
        assert {k: bool(h[k]) for k in want} == want, f"stub announce {f.data.hex()}"
        tasks = [h[k] for k in ("task_control", "task_can_rx", "task_can_tx", "task_telemetry", "task_diag")]
        assert tasks == [1] * 5, f"task liveness {tasks} at {f.t_ms} ms"


# -- inverter mirrors ------------------------------------------------------------------

def test_inverter_state_and_faults_mirror(live, contract):
    """0x461 -> 0x700 inv_state, 0x702 inv_error/dem_present, 0x708 L1/L2 and
    its freshness: seq advances by the arrivals per 100 ms (10 at 10 ms)."""
    sim, t_arm = live
    i = INV
    st = _latest(sim, contract[0x700])
    assert st["inv_state"] == i["state"]
    inv = _latest(sim, contract[0x702])
    assert (inv["inv_error"], inv["dem_present"]) == (i["dem_code"] & 0xFF, 1)
    msg = contract[0x708]
    fl = _latest(sim, msg)
    l1 = ["pwrstg_alive", "pwrstg_enable", "pwrstg_uvlo", "pwrstg_desat", "pwrstg_dt_violation",
          "pwrstg_hvil_open", "pwrstg_ocp", "pwrstg_ovp_th1", "pwrstg_ovp_th2"]
    l2 = ["emctrl_init_ok", "emctrl_posfb", "emctrl_asc", "emctrl_curr_imbalance",
          "emctrl_pwrstg_fault", "emctrl_curr_derating", "emctrl_loop_delocked", "emctrl_phcurr_acq"]
    assert sum(int(fl[n]) << b for b, n in enumerate(l1)) == i["l1"]
    assert sum(int(fl[n]) << b for b, n in enumerate(l2)) == i["l2"]
    assert fl["inv_state_age_ms"] <= TICK_MS
    seq = [msg.decode(f.data)["inv_state_seq"] for f in sim.can("can_acu").frames(0x708, since_us=t_arm)]
    deltas = {(b - a) % 256 for a, b in zip(seq[1:], seq[2:])}
    assert deltas == {10}, f"0x461 arrivals per 0x708: {deltas}"


def test_inverter_feedback_mirrors(live, contract):
    """0x463/0x465/0x466/0x467/0x468 -> 0x702, 0x70B, 0x70C, 0x70D, 0x100."""
    sim, _ = live
    i = INV
    inv = _latest(sim, contract[0x702])
    assert (inv["dc_bus_voltage"], inv["inv_rpm"]) == (i["vdc"], int(i["erpm"] / 10))
    foc = _latest(sim, contract[0x70B])
    assert foc["current_d_A"] == i["current_d"] / 32 and foc["current_q_A"] == i["current_q"] / 32
    assert (foc["volt_modulus"], foc["cmd_src"], foc["ctrl_type"], foc["ctrl_mode"]) == (
        i["modulus"], i["cmd_src"], i["ctrl_type"], i["ctrl_mode"])
    assert [foc[f"s{n}_fresh"] for n in (4, 6, 8, 9)] == [1, 1, 1, 1]
    tq = _latest(sim, contract[0x70C])
    assert (tq["torque_max_feas"], tq["setpoint_q"], tq["torque_est"]) == (
        i["tq_max_feas"], i["setpoint_q"] / 32, i["tq_est"])
    pw = _latest(sim, contract[0x70D])
    assert (pw["dc_bus_V"], pw["accu_current"]) == (i["vdc"], pytest.approx(AMS["accu_dA"] / 10))
    hb = _latest(sim, contract[HEARTBEAT])
    assert (hb["dc_bus_voltage"], hb["dc_bus_valid"]) == (i["vdc"], 1)


@pytest.mark.parametrize("erpm", [0, 9, -9, 10, -10, 123456, -123456, 524287, -524288])
def test_rpm_mirrors_to_shaft_rpm(ecu, contract, erpm):
    """J-001 / L-003: 0x463 erpm (20-bit signed) / 10, truncated toward zero,
    -> 0x702 inv_rpm and 0x506 motor_rpm on every 10 ms tick."""
    _arm(ecu)
    ecu.can("can_inv").send_periodic("rpm", INV_RPM, _inv_rpm(erpm), 10)
    ecu.run_for(ms=50)
    t = ecu.now_us()
    ecu.run_for(ms=210)
    shaft = int(erpm / 10)
    rpm = ecu.can("can_acu").frames(MOTOR_RPM, since_us=t)
    assert_cadence(rpm, period_us=TICK_MS * 1000, jitter_us=KERNEL_TICK_US, min_count=20)
    assert {contract[MOTOR_RPM].decode(f.data)["motor_rpm"] for f in rpm} == {shaft}
    assert _latest(ecu, contract[0x702])["inv_rpm"] == shaft


def test_temperatures_mirror_with_sentinel_and_staleness(ecu, contract, src):
    """J-002: 0x464 raw bytes -> 0x706 (DBC offset -50); 0xFF invalidates
    that sensor; both invalid -> temp_unknown and the unknown cap; a silent
    0x464 is unknown after InvTempsStaleMs, not the held reading."""
    msg = contract[0x706]
    inv = ecu.can("can_inv")
    _arm(ecu)
    unknown_cap = _config(src, "MotorTempUnknownCapPct")
    stale_ms = _config(src, "InvTempsStaleMs")

    def after(temps, ms=250):
        inv.send_periodic("temps", INV_TEMPS, bytes(temps) + bytes(4), 100)
        ecu.run_for(ms=ms)
        return _latest(ecu, msg)

    t = after((70, 80, 90, 0xFF))
    assert [t[k] for k in ("temp_board_degC", "temp_pwrstg_degC", "temp_motor1_degC",
                           "temp_motor2_degC")] == [20, 30, 40, 205]
    assert (t["temp_s1_valid"], t["temp_s2_valid"], t["temp_unknown"]) == (1, 0, 0)
    t = after((70, 80, 0xFF, 0xFF))
    assert (t["temp_s1_valid"], t["temp_s2_valid"], t["temp_unknown"]) == (0, 0, 1)
    assert t["thermal_cap_pct"] == unknown_cap
    t = after((70, 80, 90, 95))
    assert (t["temp_s1_valid"], t["temp_s2_valid"], t["temp_unknown"]) == (1, 1, 0)
    inv.stop_periodic("temps")
    ecu.run_for(ms=stale_ms - 150)
    t = _latest(ecu, msg)
    assert t["temp_unknown"] == 0, "unknown before InvTempsStaleMs"
    ecu.run_for(ms=300)
    t = _latest(ecu, msg)
    assert (t["temp_s1_valid"], t["temp_s2_valid"], t["temp_unknown"]) == (0, 0, 1)
    assert t["temp_motor1_degC"] == 40, "the raw mirror is the last reading"


# -- dash, FDCAN3 ---------------------------------------------------------------------

def test_dash_frames_every_200_ms(live):
    """Every dash frame at its DLC, all 18 in one burst every 200 ms, the
    sequence +1 per burst; nothing else on FDCAN3."""
    sim, _ = live
    dash = sim.can("can_dash")
    seen = {f.id for f in dash.frames()}
    assert seen == set(DASH), f"FDCAN3 ids: extra {sorted(seen - set(DASH))}, missing {sorted(set(DASH) - seen)}"
    for can_id, dlc in DASH.items():
        frames = dash.frames(can_id, since_us=sim.app_started["ecu"] + BOOT_MS * 1000)
        assert {len(f.data) for f in frames} == {dlc}, f"{can_id:#x} DLC"
        assert_period(frames, period_us=DASH_PERIOD_MS * 1000, tolerance_us=1000, min_count=8)
    status = dash.frames(0x510, since_us=sim.app_started["ecu"] + BOOT_MS * 1000)
    seq = [int.from_bytes(f.data[6:8], "little") for f in status]
    assert seq == list(range(seq[0], seq[0] + len(seq))), f"seq {seq}"
    ticks = [int.from_bytes(f.data[4:8], "little") for f in dash.frames(0x51B, since_us=sim.app_started["ecu"] + BOOT_MS * 1000)]
    assert {b - a for a, b in zip(ticks, ticks[1:])} == {DASH_PERIOD_MS}, f"tick_ms {ticks}"


def test_dash_mirrors_the_vehicle(live, contract):
    """Each live field of 0x510-0x521 against its source: inverter, AMS and
    the ControlTask mirrors (0x701's pedal raws, 0x700's state)."""
    sim, _ = live
    dash = sim.can("can_dash")
    d = {i: dash.last(i).data for i in DASH}
    u16 = lambda b, o: int.from_bytes(b[o:o + 2], "little")
    s16 = lambda b, o: int.from_bytes(b[o:o + 2], "little", signed=True)
    i, a = INV, AMS
    st = _latest(sim, contract[0x700])
    ped = _latest(sim, contract[0x701])
    assert d[0x510][0] == i["state"] and d[0x510][3] == st["ok_precharge"] == 1
    assert d[0x510][1] == st["torque_pct"] and d[0x510][2] == (2 if st["t11_8_9"] else 0)
    assert (u16(d[0x511], 0), u16(d[0x511], 2), u16(d[0x511], 4)) == (
        ped["apps1_raw"], ped["apps2_raw"], ped["brake_raw"])
    assert (u16(d[0x512], 0), u16(d[0x512], 2), d[0x512][4], d[0x512][5]) == (
        i["vdc"], a["v_cell_min"], i["dem_code"], 1)
    assert (u16(d[0x513], 0), u16(d[0x513], 2), u16(d[0x513], 4)) == (
        i["temps"][2], i["temps"][1], i["temps"][0])
    assert int.from_bytes(d[0x514], "little", signed=True) == i["erpm"]
    assert (d[0x517][0], d[0x517][1]) == (st["fsm_state"], a["fsm"])
    assert (s16(d[0x518], 1), s16(d[0x518], 3), s16(d[0x518], 5)) == (
        a["accu_dA"], a["dcdc_dA"], a["tmax_dcdc"])
    assert [u16(d[0x51C], o) for o in (0, 2, 4)] + [u16(d[0x51D], o) for o in (0, 2)] == list(a["vmin"])
    assert [u16(d[0x51E], o) for o in (0, 2, 4)] + [u16(d[0x51F], o) for o in (0, 2)] == list(a["vmax"])
    assert [s16(d[0x520], o) for o in (0, 2, 4)] + [s16(d[0x521], o) for o in (0, 2)] == list(a["tmax"])
    assert s16(d[0x521], 4) == a["tmax_dcdc"]


# -- GPS ------------------------------------------------------------------------------

def test_gps_module_is_configured_at_start(live):
    """PMTK314 (RMC + GGA only) then PMTK220,200 (5 Hz), checksummed."""
    sim, _ = live
    tx = sim.monitor("sysbus.usart10 DumpHistoryBuffer", board="ecu")
    lines = [ln.strip() for ln in tx.splitlines() if ln.strip()]
    assert lines[:2] == [_nmea("PMTK314,0,1,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0"),
                         _nmea("PMTK220,200")], f"GPS TX: {tx!r}"


def test_gps_fix_is_published(live, contract):
    """RMC + GGA -> 0x508 lat/lon (deg * 1e7) and 0x509 speed (knots -> km/h),
    course, sats, fix and the sentence count."""
    sim, _ = live
    pos = _latest(sim, contract[GPS_POSITION])
    assert pos["latitude"] == pytest.approx(48 + 7.038 / 60, abs=1e-7)
    assert pos["longitude"] == pytest.approx(11 + 31.0 / 60, abs=1e-7)
    st = _latest(sim, contract[GPS_STATUS])
    assert st["speed_kmh"] == pytest.approx(22.4 * 1.852, abs=0.01)
    assert st["course_deg"] == pytest.approx(84.4)
    assert (st["sats"], st["has_fix"], st["nmea_count"]) == (8, 1, 2)


def test_gps_before_any_sentence_and_southwest(ecu, contract):
    """No sentence yet: 0x508/0x509 still go out, empty, no fix. A S/W fix
    is negative; an RMC that loses the fix clears has_fix and keeps the last
    good position rather than its empty fields."""
    pos, st = contract[GPS_POSITION], contract[GPS_STATUS]
    ecu.run_for(ms=250)
    assert _latest(ecu, pos) == {"latitude": 0, "longitude": 0}
    assert _latest(ecu, st)["has_fix"] == 0 and _latest(ecu, st)["nmea_count"] == 0
    _gps(ecu, "GPRMC,101010,A,3352.500,S,15112.250,W,000.0,000.0,010126,,",
         "GPGGA,101010,3352.500,S,15112.250,W,2,11,0.8,10.0,M,0.0,M,,")
    ecu.run_for(ms=250)
    p = _latest(ecu, pos)
    assert p["latitude"] == pytest.approx(-(33 + 52.5 / 60), abs=1e-7)
    assert p["longitude"] == pytest.approx(-(151 + 12.25 / 60), abs=1e-7)
    assert (_latest(ecu, st)["sats"], _latest(ecu, st)["has_fix"]) == (11, 1)
    _gps(ecu, "GPRMC,101011,V,,,,,,,010126,,")
    ecu.run_for(ms=250)
    assert _latest(ecu, st)["has_fix"] == 0
    assert _latest(ecu, pos) == p


@pytest.mark.xfail(strict=True, reason=(
    "isc-fs/IFS08-CE-ECU#249: has_fix is never aged. GpsService stamps last_tick_ms "
    "(gps_service.cpp:26) and nothing reads it; gps_tx.cpp:45 publishes the "
    "parser's last has_fix, so a GPS that falls silent after a fix keeps "
    "0x509 has_fix = 1 and 0x508 on the frozen position for ever, against "
    "gps_task.cpp:130-132 ('a stale/absent GPS still shows has_fix=0')"))
def test_a_silent_gps_loses_its_fix(ecu, contract):
    """The module stops talking (cable off) after a fix: within 2 s (ten
    5 Hz fixes) 0x509 must stop claiming one."""
    _gps(ecu, RMC, GGA)
    ecu.run_for(ms=250)
    assert _latest(ecu, contract[GPS_STATUS])["has_fix"] == 1
    ecu.run_for(ms=2000)
    st = _latest(ecu, contract[GPS_STATUS])
    assert st["nmea_count"] == 2, "sentences arrived from nowhere"
    assert st["has_fix"] == 0, "0x509 still claims a fix 2 s after the GPS fell silent"
