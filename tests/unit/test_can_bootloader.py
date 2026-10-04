"""The CAN bootloader client's framing, against the bootloader's wire format
(stm32-can-bootloader v1.7.0 bl_proto.h / bl_isotp.c) and can-flasher v3.1.1's
own unit tests (protocol/isotp.rs, protocol/commands.rs)."""
import struct
import zlib

import pytest

from vhil import can_bootloader as cb
from vhil.sim import Frame


def test_ids_follow_the_direction_and_node_layout():
    assert cb.tx_id(0x2) == 0x002 and cb.tx_id(cb.NODE_BROADCAST) == 0x00F
    assert cb.rx_id(0x1) == 0x011 and cb.rx_id(0xE) == 0x01E
    for bad in (0x0, 0xF):                     # host and broadcast never reply
        with pytest.raises(ValueError):
            cb.rx_id(bad)


def test_a_short_message_is_one_padded_single_frame():
    # CONNECT 0.2 under CMD: [type, opcode, major, minor] (commands.rs connect_payload_shape)
    assert cb.segment(bytes([0x00, 0x01, 0, 2])) == [bytes.fromhex("0400010002000000")]
    assert cb.segment(b"\x07" * 7) == [b"\x07" + b"\x07" * 7]


def test_a_long_message_is_a_first_frame_and_numbered_consecutive_frames():
    msg = bytes(range(20))
    frames = cb.segment(msg)
    assert frames[0] == bytes([0x10, 20]) + msg[:6]
    assert frames[1] == bytes([0x21]) + msg[6:13]
    assert frames[2] == bytes([0x22]) + msg[13:20]
    assert all(len(f) == 8 for f in frames)


def test_a_flash_write_chunk_segments_as_can_flasher_sends_it():
    """A 256 B write is 262 B with type, opcode and address: one FF and 37 CFs,
    the CF sequence wrapping 15 -> 0 (isotp.rs strict_bl_accepts_..._262_bytes)."""
    msg = bytes([cb.MSG_CMD, cb.FLASH_WRITE]) + struct.pack("<I", 0x08020000) + bytes(256)
    frames = cb.segment(msg)
    assert len(frames) == 1 + 37
    assert frames[0][:2] == bytes([0x11, 0x06])            # 0x106 = 262
    assert [f[0] for f in frames[1:18]] == [0x20 | (s & 0xF) for s in range(1, 18)]


def test_segment_refuses_what_the_bootloader_cannot_reassemble():
    with pytest.raises(ValueError):
        cb.segment(b"")
    with pytest.raises(ValueError):
        cb.segment(bytes(1025))                            # BL_ISOTP_MAX_MSG = 1024


@pytest.mark.parametrize("n", [1, 7, 8, 13, 14, 65, 262, 1024])
def test_reassembly_inverts_segmentation(n):
    msg = bytes((i * 7) & 0xFF for i in range(n))
    rx = cb.Reassembler()
    out = [rx.feed(f) for f in cb.segment(msg)]
    assert out[-1] == msg and all(o is None for o in out[:-1])


def test_reassembly_skips_flow_control_and_rejects_bad_sequences():
    rx = cb.Reassembler()
    frames = cb.segment(bytes(20))
    assert rx.feed(frames[0]) is None
    assert rx.feed(bytes([0x30, 0, 0])) is None            # the node's FC(CTS)
    with pytest.raises(cb.IsoTpError, match="seq"):
        rx.feed(frames[2])
    with pytest.raises(cb.IsoTpError, match="without FF"):
        cb.Reassembler().feed(frames[1])


def test_replies_parse_ack_nack_and_discover():
    ack = cb.parse_reply(5, bytes([cb.MSG_ACK, cb.FLASH_READ_CRC, 0x78, 0x56, 0x34, 0x12]))
    assert (ack.msg_type, ack.opcode, ack.data) == (cb.MSG_ACK, cb.FLASH_READ_CRC,
                                                    bytes.fromhex("78563412"))
    nack = cb.parse_reply(5, bytes([cb.MSG_NACK, cb.FLASH_ERASE, cb.NACK_BAD_SESSION]))
    assert (nack.opcode, nack.code) == (cb.FLASH_ERASE, cb.NACK_BAD_SESSION)
    disc = cb.parse_reply(5, bytes([cb.MSG_DISCOVER_REPLY, cb.DISCOVER, 2, 0, 2]))
    assert disc.data == bytes([2, 0, 2])


def test_crc32_is_the_bootloaders_reflected_ieee():
    assert cb.crc32(b"123456789") == 0xCBF43926            # CRC-32/ISO-HDLC check value
    assert cb.crc32(b"\xFF" * 0x20000) == zlib.crc32(b"\xFF" * 0x20000)


def test_firmware_info_parses_and_packs_the_version():
    record = struct.pack("<6I8sQ16s2I", cb.FWINFO_MAGIC, 0x00010000, 1, 6, 300, 0x483,
                         b"\xAA" * 8, 1_700_000_000, b"AMS\0", 2, 0)
    image = b"\xFF" * cb.FWINFO_OFFSET + record
    info = cb.FwInfo.from_image(image)
    assert (info.major, info.minor, info.patch, info.product, info.reserved) == \
        (1, 6, 300, "AMS", (2, 0))
    assert info.packed_version == (1 << 16) | (6 << 8) | 255     # patch clamps to a byte
    assert cb.FwInfo.from_image(b"\xFF" * 2048) is None


def test_sectors_cover_an_image_from_the_app_base():
    assert cb.sectors_of(cb.APP_BASE, 1) == [0x08020000]
    assert cb.sectors_of(cb.APP_BASE, 0x20000) == [0x08020000]
    assert cb.sectors_of(cb.APP_BASE, 0x20001) == [0x08020000, 0x08040000]
    assert cb.sectors_of(cb.APP_BASE, 0xC0000)[-1] == 0x080C0000         # sector 6


@pytest.mark.parametrize("start, length", [
    (0x08000000, 0x20000),            # sector 0: the bootloader
    (0x0801FFE0, 0x40),               # straddles into sector 0
    (0x080E0000, 0x20000),            # sector 7: NVM, seed, metadata
    (0x080DFFE0, 0x40),               # straddles into sector 7
    (cb.APP_BASE, 0),
])
def test_the_client_never_touches_the_bootloader_or_its_nvm(start, length):
    class NoSim:                      # any frame sent would be an AttributeError
        def can(self, bus):
            return None
    bl = cb.CanBootloader(NoSim(), "can_acu", 2)
    with pytest.raises(ValueError, match="refusing"):
        bl.erase(start, length)
    with pytest.raises(ValueError, match="refusing"):
        bl.write(start, bytes(length))
    with pytest.raises(ValueError, match="refusing"):
        bl.flash(bytes(0xC0001))      # one byte past sector 6


def test_flash_writes_flashword_padded_chunks():
    assert cb.pad_flashword(b"\x01" * 33) == b"\x01" * 33 + b"\xFF" * 31
    assert cb.pad_flashword(b"\x01" * 64) == b"\x01" * 64


class FakeBus:
    """A bus whose node answers every command with an ACK at once."""

    def __init__(self, sim, node):
        self.sim, self.node, self.sent, self.out = sim, node, [], []
        self.reply = b"\xAA"                           # the ACK's data

    def send_sequence(self, frames, gap_us, burst):
        for i, (can_id, data) in enumerate(frames):
            self.sent.append((self.sim.t + (i // burst) * burst * gap_us, can_id, bytes(data)))
            if data[0] >> 4 in (0x0, 0x1):                 # SF / FF: the request's start
                opcode = data[2] if data[0] >> 4 == 0 else data[3]
                for f in cb.segment(bytes([cb.MSG_ACK, opcode]) + self.reply):
                    self.out.append(Frame(self.sim.t + 100, cb.rx_id(self.node), False, f))

    def frames(self, ids, since_us=0):
        return [f for f in self.out if f.id in ids and f.t_us >= since_us]


class FakeSim:
    def __init__(self, node):
        self.t, self.bus = 0, FakeBus(self, node)

    def can(self, bus):
        return self.bus

    def now_us(self):
        return self.t

    def run_for(self, ms=0, us=0):
        self.t += int(ms * 1000) + us
        return self.t


def test_frames_go_out_at_the_bus_rate_and_the_ack_is_awaited():
    sim = FakeSim(2)
    bl = cb.CanBootloader(sim, "can_acu", 2)
    assert bl.command(cb.FLASH_WRITE, struct.pack("<I", cb.APP_BASE) + bytes(256)) == b"\xAA"
    times = [t for t, _, _ in sim.bus.sent]
    assert len(times) == 38 and all(i == 0x002 for _, i, _ in sim.bus.sent)
    bursts = sorted(set(times))
    assert [times.count(t) for t in bursts] == [cb.BURST] * 9 + [2]
    assert [b - a for a, b in zip(bursts, bursts[1:])] == [cb.BURST * cb.FRAME_US] * 9
    assert sim.t >= 38 * cb.FRAME_US               # ran until the last frame was out


def test_a_silent_node_times_out_in_virtual_time():
    sim = FakeSim(2)
    sim.bus.out = []
    sim.bus.send_sequence = lambda frames, gap, burst: None
    bl = cb.CanBootloader(sim, "can_acu", 2, timeout_ms=20)
    with pytest.raises(TimeoutError):
        bl.connect()
    assert 20_000 <= sim.t <= 22_000


# -- health and option bytes ----------------------------------------------------

def _sent_message(sim):
    """The one message the host sent, reassembled from its frames."""
    rx = cb.Reassembler()
    for _, _, data in sim.bus.sent:
        message = rx.feed(data)
    return message


def test_apply_wrp_sends_the_token_and_the_sector_0_mask():
    """[CMD, OB_APPLY_WRP, token_le32, mask_le32] (bl_proto.c:1102-1121)."""
    sim = FakeSim(1)
    cb.CanBootloader(sim, "can_acu", 1).apply_wrp()
    assert _sent_message(sim) == bytes([cb.MSG_CMD, cb.OB_APPLY_WRP]) + \
        b"WRP\0" + struct.pack("<I", 0x01)


def test_ob_read_parses_the_status_record():
    """bl_ob_status_t: wrp mask, user config, rdp byte, bor byte, 2 + 4 reserved."""
    sim = FakeSim(1)
    sim.bus.reply = struct.pack("<IIBB2xI", 0x01, 0x179E00F0, 0xAA, 0x04, 0)
    status = cb.CanBootloader(sim, "can_acu", 1).ob_read()
    assert status == cb.ObStatus(0x01, 0x179E00F0, 0xAA, 0x04)
    assert _sent_message(sim) == bytes([cb.MSG_CMD, cb.OB_READ])


def test_health_parses_the_record_and_its_wrp_flag():
    sim = FakeSim(1)
    sim.bus.reply = struct.pack("<8I", 12, 1, cb.HEALTH_FLAG_WRP_PROTECTED | 0x2, 3, 1,
                                cb.DTC_FLASH_HW, 0, 5)
    health = cb.CanBootloader(sim, "can_acu", 1).health()
    assert health.wrp_protected and health.flags == 0x12
    assert (health.dtc_count, health.last_dtc_code, health.max_flash_op_ms) == (1, cb.DTC_FLASH_HW, 5)
    with pytest.raises(ValueError):
        cb.Health.parse(bytes(31))
    with pytest.raises(ValueError):
        cb.ObStatus.parse(bytes(15))

