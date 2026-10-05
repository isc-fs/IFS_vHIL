"""ams-temps (#28): the NTC path end to end, in virtual time, with temperatures
set behind the ADG731 mux on the LTC6811 models.

AMS facts (IFS08-CE-AMS ams_config.hpp, bms_service.cpp, safety_predicates.hpp):
  Adg731ChannelMap: NTC_1..10 on S1..S10 (channels 0..9), NTC_11..20 on
    S17..S26 (16..25); S11..S16 and S27..S32 are not swept. The upper chip
    of a module stores slots 0..19, the lower 20..39 (bms_service.cpp:378).
  ntc_mV_to_tempC: 0 mV (short) or >= NtcOpenMv (2800) is no reading; so is
    anything outside NtcMinValidC..NtcMaxValidC (-40..150).
  TempSensorDisconnected = reason 13, detail = module mask, armed whatever
    TempFaultsTrusted says (safety_predicates.hpp:193); every slot is in
    RequiredTempSlots, so one open NTC on any module faults on the first
    sweep (TempDisconnectPolls = 1). FS rule: the SDC opens in < 500 ms.
  TempFaultsTrusted = false: CellOverTempC (60) / CellUnderTempC (-10) are
    not faults (safety_predicates.hpp:227).
  0x136: BE i16 degC, hottest NTC of modules 0..2 (250 ms). 0x6A0..0x6B8
    (pit-diag, armed by 0x7F0 DE AD BE EF): i8 degC, eight per frame,
    row-major over cell_tempC[5][40]. 0x6C0 byte 0 state, 6 reason, 7 detail.
"""
import pytest

from vhil.sim import Sim
from vhil.system import REPO

PIT_ARM, PIT_FSM, PIT_TEMPS, TMAX_A = 0x7F0, 0x6C0, 0x6A0, 0x136
START, ERROR = 0, 5
DISCONNECTED = 13
SWEPT = list(range(10)) + list(range(16, 26))       # Adg731ChannelMap


@pytest.fixture
def ams(images):
    with Sim(REPO / "systems" / "ams.yaml", images("ams")) as sim:
        sim.wait_for_app()
        sim.run_for(ms=1500)
        sim.can("can_acu").send(PIT_ARM, bytes.fromhex("DEADBEEF"))
        sim.run_for(ms=2500)                              # past boot grace
        yield sim


def _chip(ams, k, command):
    return ams.monitor(f"sysbus.spi1.isospi.cells{k} {command}", board="ams")


def _fsm(ams, ms=1500):
    ams.run_for(ms=ms)
    f = ams.can("can_acu").last(PIT_FSM)
    return f.data[0], f.data[6], f.data[7]


def _slot_temp(ams, module, slot):
    """cell_tempC[module][slot] as the pit-diag stream reports it."""
    flat = module * 40 + slot
    f = ams.can("can_acu").last(PIT_TEMPS + flat // 8)
    return int.from_bytes(f.data[flat % 8:flat % 8 + 1], "big", signed=True)


def _tmax(ams, module):
    f = ams.can("can_acu").last(TMAX_A)
    return int.from_bytes(f.data[2 * module:2 * module + 2], "big", signed=True)


def test_a_healthy_pack_reads_room_temperature(ams):
    """E-064: every swept channel converts, none fails, no fault."""
    assert _fsm(ams) == (START, 0, 0)
    assert ams.read_symbol("ams", "g_temp_sweep_sticky_mask", 4) == 0, "a temp sweep step failed"
    for m in range(5):
        for s in (0, 19, 20, 39):
            assert abs(_slot_temp(ams, m, s) - 25) <= 1, f"module {m} slot {s}"
    assert all(abs(_tmax(ams, m) - 25) <= 1 for m in range(3))


@pytest.mark.parametrize("chip, channel, slot", [
    (2, 3, 3),          # upper chip of module 1, S4 -> slot 3
    (2, 16, 10),        # upper, S17 -> slot 10 (the second bank)
    (3, 25, 39),        # lower chip of module 1, S26 -> slot 39
], ids=["upper-S4", "upper-S17", "lower-S26"])
def test_a_temperature_lands_in_its_slot(ams, chip, channel, slot):
    """Decode and the mux map: 47 C behind one channel reaches its slot and
    the module's maximum, and nothing else moves."""
    _chip(ams, chip, f"SetTemperature {channel} 470")
    ams.run_for(ms=1000)
    assert abs(_slot_temp(ams, 1, slot) - 47) <= 1, f"slot {slot}: {_slot_temp(ams, 1, slot)} C"
    assert abs(_tmax(ams, 1) - 47) <= 1
    assert abs(_tmax(ams, 0) - 25) <= 1 and abs(_tmax(ams, 2) - 25) <= 1
    others = [s for s in range(40) if s != slot]
    assert all(abs(_slot_temp(ams, 1, s) - 25) <= 1 for s in others)


@pytest.mark.parametrize("deci, label", [(700, "over"), (-120, "under")])
def test_out_of_range_temperatures_are_not_faults_while_untrusted(ams, deci, label):
    """B-026c, B-029-OT: TempFaultsTrusted = false, so 70 C (> 60) and -12 C
    (< -10) on a whole chip read through but never fault (IFS_HIL expects
    an Error: drift)."""
    _chip(ams, 0, f"SetAllTemperatures {deci}")
    assert _fsm(ams, ms=2000) == (START, 0, 0), f"{label}-temperature faulted"
    assert abs(_tmax(ams, 0) - (70 if deci > 0 else 25)) <= 1
    assert abs(_slot_temp(ams, 0, 0) - deci // 10) <= 1


@pytest.mark.parametrize("chip, channel, raw, module", [
    (6, 0, 30000, 3),     # open: rails to VREF2 (3.0 V), temp 1 of LTC_1
    (6, 0, 28500, 3),     # partly railed open, above NtcOpenMv
    (9, 25, 30000, 4),    # open on a lower chip's last channel
    (1, 9, 0, 0),         # shorted NTC: 0 mV is no reading either
], ids=["open", "half-railed", "lower-last", "short"])
def test_a_lost_sensor_opens_the_sdc_within_500ms(ams, chip, channel, raw, module):
    """FS rule: a disconnected temperature sensor faults reason 13 with its
    module, in < 500 ms, while the range faults stay gated."""
    assert ams.read_symbol("ams", "g_state_telemetry") == START
    _chip(ams, chip, f"SetAuxRaw {channel} {raw}")
    for elapsed in range(25, 525, 25):
        ams.run_for(ms=25)
        if ams.read_symbol("ams", "g_state_telemetry") == ERROR:
            break
    else:
        pytest.fail("no Error within 500 ms of the sensor loss")
    assert ams.read_symbol("ams", "g_fault_reason_telemetry") == DISCONNECTED
    state, reason, detail = _fsm(ams)
    assert (state, reason, detail) == (ERROR, DISCONNECTED, 1 << module), (state, reason, detail)


def test_a_reconnected_sensor_keeps_the_latch(ams):
    _chip(ams, 4, "SetAuxRaw 5 30000")
    assert _fsm(ams)[:2] == (ERROR, DISCONNECTED)
    _chip(ams, 4, "SetTemperature 5 250")
    assert _fsm(ams)[:2] == (ERROR, DISCONNECTED), "the latch cleared on reconnect"


@pytest.mark.parametrize("channel", [10, 15, 26, 31])
def test_an_unswept_mux_input_is_ignored(ams, channel):
    """S11..S16 and S27..S32 are unpopulated: an open there is never read."""
    assert channel not in SWEPT
    _chip(ams, 2, f"SetAuxRaw {channel} 30000")
    assert _fsm(ams, ms=2000) == (START, 0, 0)
