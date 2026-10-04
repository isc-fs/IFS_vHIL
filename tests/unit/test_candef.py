"""vhil/candef.py: the firmware's .def contract, parsed and decoded.

Fixtures under fixtures/candef/ are unmodified .def files from IFS08-CE-ECU
(dev 544b651) and IFS08-CE-AMS (main df1c961); see their all_messages.inc.
The last tests run against the real firmware sources when the workspace has
a firmware build directory ($VHIL_FW_DIR or /vhil/fw, as in Docker)."""
import os
import random
from pathlib import Path

import pytest

from vhil import candef

FIX = Path(__file__).parent / "fixtures" / "candef"
ECU, AMS = FIX / "ecu", FIX / "ams"
FW_DIR = Path(os.environ.get("VHIL_FW_DIR", "/vhil/fw"))


def test_the_registry_decides_what_is_in_the_contract():
    c = candef.load(ECU)
    assert sorted(c) == [0x100, 0x4A0, 0x508, 0x700, 0x704]
    assert {m.source for m in c.values()} == {"vcu_heartbeat.def", "ams_status.def",
                                              "vcu_gps_position.def", "pit_diag_status.def",
                                              "pit_diag_health.def"}


def test_message_header_fields_and_units():
    m = candef.load(ECU)[0x704]
    assert (m.name, m.dlc, m.sender, m.period_ms, m.extended) == ("PitDiag_health", 8, "VCU", 1000, False)
    # FIELD_BE byte 0 -> MSB at bit 7; FIELD_LE_BITS start as written; FIELD_LE byte 6 -> bit 48
    assert m.fields["free_heap"] == candef.Field(True, False, 7, 16, 1.0, 0.0, "B")
    assert m.fields["reset_cause"] == candef.Field(False, False, 40, 3, 1.0, 0.0, "enum")
    assert m.fields["uptime_s"] == candef.Field(False, False, 48, 8, 1.0, 0.0, "s")
    assert len(m.fields) == 17
    assert m.values["reset_cause"][4] == "IWDG" and m.values["last_fault"][0xF5] == "StackOverflow"


def test_decode_matches_the_firmware_layout():
    status = candef.load(ECU)[0x700]
    # fsm 5, inv 4, byte2 bits 1+3 (t11_8_9, ok_precharge), torque 50 %,
    # v_cell_min BE 0x0E10 = 3600 mV, torque_cmd BE_S 0xFF33 = -205
    d = status.decode(bytes.fromhex("05040a320e10ff33"))
    assert d == {"fsm_state": 5, "inv_state": 4, "t11_8_9": 1, "rtds_active": 0, "ok_precharge": 1,
                 "start_button": 0, "dv_mode": 0, "tx_dropped": 0, "power_capped": 0,
                 "torque_pct": 50, "v_cell_min_mV": 3600, "torque_cmd": -205}
    gps = candef.load(ECU)[0x508]
    d = gps.decode(bytes.fromhex("401d171850d8cafd"))       # LE_S int32 * 1e-7
    assert d["latitude"] == pytest.approx(40.4168) and d["longitude"] == pytest.approx(-3.7038)
    currents = candef.load(AMS)[0x135]
    assert currents.decode(bytes.fromhex("ff9c0032")) == pytest.approx(
        {"current_accu_dA": -10.0, "current_dcdc_dA": 5.0})


def test_a_short_frame_decodes_only_the_fields_it_holds():
    hb = candef.load(ECU)[0x100]
    assert hb.decode(bytes.fromhex("6801")) == {"dc_bus_voltage": 0x168}
    assert hb.decode(bytes.fromhex("680102")) == {"dc_bus_voltage": 0x168, "discharge_engaged": 0,
                                                  "dc_bus_valid": 1}


def test_be_bits_walk_the_motorola_sawtooth():
    assert list(candef.be_bits(7, 16)) == [7, 6, 5, 4, 3, 2, 1, 0, 15, 14, 13, 12, 11, 10, 9, 8]
    assert list(candef.be_bits(3, 6)) == [3, 2, 1, 0, 15, 14]
    assert candef.get_be(bytes([0x0E, 0x10]), 7, 16) == 0x0E10
    assert candef.get_le(bytes([0x10, 0x0E]), 0, 16) == 0x0E10


def test_spare_bits_are_the_unclaimed_set_bits():
    status = candef.load(ECU)[0x700]
    assert status.spare_bits(bytes.fromhex("0000010000000000")) == [16]   # the RESERVED bit
    assert status.spare_bits(bytes.fromhex("05040a320e10ff33")) == []


def test_comments_and_value_passes_are_handled():
    text = """
    /* CAN_MSG(Old, 0x123, 8, "VCU", 10) -- commented out */
    // CAN_MSG(Older, 0x124, 8, "VCU", 10)
    CAN_MSG(Ext, 0x18FF0001, 8, "EMC", 0)
        FIELD_LE (a, uint8_t, 0x1, 8, 0.5f, -40, "degC")   /* hex byte, float suffix */
        // FIELD_LE (ghost, uint8_t, 2, 8, 1, 0, "")
        FIELD_BE_BITS (b, uint8_t, 23, 4, 1, 0, "")
    CAN_MSG_END(Ext)
    #ifdef ECU_DSL_VALUES_PASS
    CAN_VAL(Ext, b, 0x3, "Three")
    #endif
    """
    (msg,) = candef.parse_def(text, "x.def")
    assert (msg.name, msg.id, msg.extended, msg.period_ms) == ("Ext", 0x18FF0001, True, 0)
    assert list(msg.fields) == ["a", "b"]
    assert msg.fields["a"] == candef.Field(False, False, 8, 8, 0.5, -40.0, "degC")
    assert msg.decode(bytes([0, 100, 0x30, 0, 0, 0, 0, 0])) == {"a": 10.0, "b": 3}
    assert msg.values == {"b": {3: "Three"}}


def test_messages_dir_is_found_from_an_elf_path(tmp_path):
    assert candef.messages_dir(ECU / "build" / "ECU08.elf") == ECU / candef.MESSAGES
    assert candef.messages_dir(tmp_path / "build" / "x.elf") is None
    with pytest.raises(FileNotFoundError):
        candef.load(tmp_path)


def test_the_cache_rereads_a_changed_def(tmp_path):
    msgs = tmp_path / candef.MESSAGES
    msgs.mkdir(parents=True)
    (msgs / "all_messages.inc").write_text('#include "a.def"\n')
    (msgs / "a.def").write_text('CAN_MSG(A, 0x10, 1, "X", 0)\n FIELD_LE (v, uint8_t, 0, 8, 1, 0, "")\nCAN_MSG_END(A)\n')
    first = candef.load(tmp_path)
    assert candef.load(tmp_path) is first
    (msgs / "a.def").write_text('CAN_MSG(A, 0x10, 2, "X", 0)\n FIELD_LE (v, uint16_t, 0, 16, 1, 0, "")\nCAN_MSG_END(A)\n')
    assert candef.load(tmp_path)[0x10].dlc == 2


def test_to_json_carries_what_the_browser_decoder_needs():
    j = candef.load(ECU)[0x704].to_json()
    assert j["name"] == "PitDiag_health" and j["id"] == 0x704 and j["ext"] is False
    f = {x["name"]: x for x in j["fields"]}
    assert f["free_heap"] == {"name": "free_heap", "be": True, "signed": False, "start": 7, "length": 16,
                              "factor": 1.0, "offset": 0.0, "unit": "B"}
    assert f["reset_cause"]["values"]["4"] == "IWDG"


# -- against the real firmware sources, when built ------------------------------------

def _sources():
    if not FW_DIR.is_dir():
        return []
    return sorted(p.parents[4] for p in FW_DIR.glob(f"*/{candef.MESSAGES}/{candef.REGISTRY}"))


@pytest.mark.skipif(not _sources(), reason=f"no firmware sources under {FW_DIR}")
@pytest.mark.parametrize("src", _sources(), ids=lambda p: p.name)
def test_every_real_def_parses_and_decodes(src):
    contract = candef.load(src)
    assert contract, f"nothing parsed under {src}"
    rng = random.Random(0)
    for m in contract.values():
        assert m.fields, f"{m.name}: no fields parsed"
        for name in m.fields:          # every field lies inside its message's DLC
            assert max(m.bits(name)) < 8 * m.dlc, f"{m.name}.{name} past DLC {m.dlc}"
        claimed = [b for f in m.fields for b in m.bits(f)]
        assert len(claimed) == len(set(claimed)), f"{m.name}: overlapping fields"
        data = bytes(rng.randrange(256) for _ in range(m.dlc))
        assert set(m.decode(data)) == set(m.fields)
