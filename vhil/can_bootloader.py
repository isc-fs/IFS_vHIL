"""Host side of the CAN bootloader's protocol, driven in virtual time.

A minimal client of isc-fs/stm32-can-bootloader v1.7.0, speaking through a
Sim bus (`sim.can(bus)`) instead of SocketCAN, so a test flashes a board with
every frame and reply stamped in virtual time. It does what the suites need:
discover, connect, firmware info, health, erase / write / CRC / verify, jump,
reset, and the option bytes (read, the one-way sector-0 WRP latch).

Wire format (bootloader Core/Inc/bl_proto.h:1-60, cross-checked against
can-flasher v3.1.1 src/protocol/ids.rs and src/session/mod.rs:905-935):
  - 11-bit IDs <= 0x01F: bit 4 = direction (0 host->node, 1 node->host),
    bits 3..0 = the other end's node (host->node: dst, 0xF broadcast).
  - every logical message is ISO-TP framed (SF / FF+CFs, 8-byte frames,
    12-bit length, max 1024 B: Core/Inc/bl_isotp.h, bl_isotp.c); its first
    byte is the message type (CMD 0x00, ACK 0x01, NACK 0x02, NOTIFY 0x03,
    DISCOVER_REQUEST 0x04, DISCOVER_REPLY 0x05), then the opcode, then
    little-endian arguments (bl_proto.c:1316-1340).
  - the bootloader answers each FF with FC(CTS, BS=0, STmin=0) and never
    throttles (bl_proto.c:378-389); the host streams CFs without waiting,
    as can-flasher does (session/mod.rs:905-935). Replies come back as
    SF/FF+CFs without waiting for a host FC (bl_proto.c:288-315).
  - ACK = [opcode, data...]; NACK = [rejected opcode, code]
    (bl_proto.c:323-334).

Flash rules (Core/Src/bl_flash.c, Core/Inc/bl_memmap.h): only sectors 1..6
(0x08020000..0x080DFFFF) are writable; erase is per 128 KB sector, writes
FLASHWORD-aligned (32 B), tail padded with 0xFF; FLASH_VERIFY CRC-32s the
app from 0x08020000 and stamps the metadata record. This client refuses any
erase or write outside sectors 1..6 before a frame goes out, so it can never
touch the bootloader (sector 0) or its NVM/metadata (sector 7).

Pacing: the bus is 500 kbit/s (bootloader main.c:253-262), about 250 us per
8-byte classic frame with stuffing. The Sim's CAN hub has no bit timing, so a
message's frames are streamed at one per `frame_us` on average
(CanBus.send_sequence), as can-flasher's adapter puts them on the wire. They
arrive `burst` at a time (default 4, every 1 ms): each burst is a synced
action that costs the emulation a pause, and fits four times in the bootloader's
16-deep RX FIFO0 (main.c:308, 361, 414), which it drains between bursts.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Optional

# -- protocol constants (bl_proto.h) ------------------------------------------

PROTO_MAJOR, PROTO_MINOR = 0, 2                 # bl_proto.h BL_PROTO_VERSION_*
NODE_HOST, NODE_BROADCAST = 0x0, 0xF
DIR_NODE_TO_HOST = 0x10

MSG_CMD, MSG_ACK, MSG_NACK, MSG_NOTIFY = 0x00, 0x01, 0x02, 0x03
MSG_DISCOVER_REQUEST, MSG_DISCOVER_REPLY = 0x04, 0x05

CONNECT, DISCONNECT, DISCOVER, GET_FW_INFO, GET_HEALTH = 0x01, 0x02, 0x03, 0x04, 0x05
FLASH_ERASE, FLASH_WRITE, FLASH_READ_CRC, FLASH_VERIFY = 0x10, 0x11, 0x12, 0x13
OB_READ, OB_APPLY_WRP = 0x50, 0x51
RESET, JUMP = 0x60, 0x61

NACK_PROTECTED_ADDR, NACK_OUT_OF_BOUNDS, NACK_CRC_MISMATCH = 0x01, 0x02, 0x03
NACK_BAD_SESSION, NACK_FLASH_HW, NACK_NO_VALID_APP = 0x06, 0x07, 0x0C
NACK_OB_WRONG_TOKEN, NACK_UNSUPPORTED = 0x0F, 0xFE

# OB_APPLY_WRP's confirmation token, ASCII "WRP\0" LE (bl_obyte.h:33).
OB_APPLY_TOKEN = 0x00505257
HEALTH_FLAG_SESSION_ACTIVE = 1 << 0             # bl_health.h:42-44
HEALTH_FLAG_VALID_APP_PRESENT = 1 << 1
HEALTH_FLAG_WRP_PROTECTED = 1 << 4              # sector 0 WRP'd in the option bytes
DTC_FLASH_HW = 0x0010                           # bl_dtc.h:46

RESET_HARD, RESET_SOFT, RESET_STAY_IN_BL, RESET_TO_APP = 0, 1, 2, 3   # bl_proto.c:745-752

# -- memory map (bl_memmap.h) -------------------------------------------------

FLASH_BASE = 0x08000000
SECTOR_SIZE = 0x20000
APP_BASE, APP_END = 0x08020000, 0x080DFFFF      # sectors 1..6
FLASHWORD = 32
WRITE_CHUNK = 256      # can-flasher flash/mod.rs:65-76 (REQUIREMENTS "Write chunk size")
FWINFO_OFFSET, FWINFO_MAGIC = 0x400, 0xF14F1B00  # bl_fwinfo.h

# -- timing -------------------------------------------------------------------

FRAME_US = 250         # one 8-byte classic frame at 500 kbit/s, with stuffing
BURST = 4              # frames per synced burst (1 ms of wire): a quarter of RX FIFO0

MAX_MSG = 1024         # bl_isotp.h BL_ISOTP_MAX_MSG


def tx_id(node: int) -> int:
    """Host -> node frame ID (dst = node, 0xF broadcast)."""
    if not 0 <= node <= 0xF:
        raise ValueError(f"node {node:#x} outside 0x0..0xF")
    return node


def rx_id(node: int) -> int:
    """Node -> host frame ID for a node's replies."""
    if not 0x1 <= node <= 0xE:
        raise ValueError(f"node {node:#x} outside 0x1..0xE")
    return DIR_NODE_TO_HOST | node


def crc32(data: bytes) -> int:
    """CRC-32/ISO-HDLC, the bootloader's bl_flash_crc32 (bl_flash.c:244-260)."""
    return zlib.crc32(data) & 0xFFFFFFFF


# -- ISO-TP -------------------------------------------------------------------

def segment(message: bytes) -> list[bytes]:
    """ISO-TP frames of one message, each padded to 8 bytes with zeros (as
    can-flasher's IsoTpSegmenter, protocol/isotp.rs:140-230)."""
    n = len(message)
    if not 1 <= n <= MAX_MSG:
        raise ValueError(f"message of {n} B, ISO-TP here carries 1..{MAX_MSG}")
    if n <= 7:
        return [bytes([n]) + message + bytes(7 - n)]
    frames = [bytes([0x10 | (n >> 8), n & 0xFF]) + message[:6]]
    seq, offset = 1, 6
    while offset < n:
        chunk = message[offset:offset + 7]
        frames.append(bytes([0x20 | seq]) + chunk + bytes(7 - len(chunk)))
        seq, offset = (seq + 1) & 0xF, offset + 7
    return frames


class IsoTpError(Exception):
    pass


class Reassembler:
    """Rebuilds one node's messages from its frames. FC frames (the node's
    flow control for our FFs) are skipped."""

    def __init__(self):
        self._buf, self._total, self._seq = None, 0, 0

    def feed(self, data: bytes) -> Optional[bytes]:
        if not data:
            raise IsoTpError("empty frame")
        pci = data[0] >> 4
        if pci == 0x3:                                    # FC
            return None
        if pci == 0x0:                                    # SF
            n = data[0] & 0xF
            if not 1 <= n <= 7 or n + 1 > len(data):
                raise IsoTpError(f"bad SF {data.hex()}")
            self._buf = None
            return bytes(data[1:1 + n])
        if pci == 0x1:                                    # FF
            total = ((data[0] & 0xF) << 8) | data[1]
            if len(data) < 8 or not 7 < total <= MAX_MSG:
                raise IsoTpError(f"bad FF {data.hex()}")
            self._buf, self._total, self._seq = bytearray(data[2:8]), total, 1
            return None
        if pci == 0x2:                                    # CF
            if self._buf is None:
                raise IsoTpError("CF without FF")
            if data[0] & 0xF != self._seq:
                raise IsoTpError(f"CF seq {data[0] & 0xF}, expected {self._seq}")
            self._buf += data[1:1 + self._total - len(self._buf)]
            self._seq = (self._seq + 1) & 0xF
            if len(self._buf) >= self._total:
                out, self._buf = bytes(self._buf), None
                return out
            return None
        raise IsoTpError(f"bad PCI {data.hex()}")


# -- messages -----------------------------------------------------------------

@dataclass(frozen=True)
class Reply:
    """One message from a node. ACK: opcode + data; NACK: opcode (the
    rejected one) + code; DISCOVER_REPLY: data = node, major, minor;
    NOTIFY: opcode = the notification code."""
    t_us: int
    msg_type: int
    opcode: int
    data: bytes = b""
    code: Optional[int] = None


def parse_reply(t_us: int, message: bytes) -> Reply:
    if len(message) < 2:
        raise IsoTpError(f"message too short: {message.hex()}")
    kind, opcode, rest = message[0], message[1], bytes(message[2:])
    if kind == MSG_NACK:
        return Reply(t_us, kind, opcode, rest, rest[0] if rest else None)
    return Reply(t_us, kind, opcode, rest)


@dataclass(frozen=True)
class Discovered:
    node: int
    major: int
    minor: int
    t_us: int


@dataclass(frozen=True)
class FwInfo:
    """The app's firmware-info record at 0x08020400 (bl_fwinfo.h)."""
    magic: int
    record_version: int
    major: int
    minor: int
    patch: int
    mcu_id: int
    git_hash: bytes
    build_timestamp: int
    product: str
    reserved: tuple[int, int]

    @classmethod
    def parse(cls, record: bytes) -> "FwInfo":
        if len(record) < 64:
            raise ValueError(f"firmware-info record of {len(record)} B, expected 64")
        magic, rec, major, minor, patch, mcu, git, ts, name, r0, r1 = \
            struct.unpack_from("<6I8sQ16s2I", record)
        return cls(magic, rec, major, minor, patch, mcu, git, ts,
                   name.split(b"\0", 1)[0].decode("ascii", "replace"), (r0, r1))

    @classmethod
    def from_image(cls, image: bytes) -> Optional["FwInfo"]:
        """The record inside an app image based at 0x08020000, if valid."""
        if len(image) < FWINFO_OFFSET + 64:
            return None
        info = cls.parse(image[FWINFO_OFFSET:FWINFO_OFFSET + 64])
        return info if info.magic == FWINFO_MAGIC else None

    @property
    def packed_version(self) -> int:
        """(major << 16) | (minor << 8) | patch, each clamped to a byte: the
        version word FLASH_VERIFY stamps (can-flasher firmware/mod.rs:128-140)."""
        return (min(self.major, 255) << 16) | (min(self.minor, 255) << 8) | min(self.patch, 255)


@dataclass(frozen=True)
class Health:
    """GET_HEALTH's 32-byte record (bl_health.h:50-66)."""
    uptime_s: int
    reset_cause: int
    flags: int
    flash_write_count: int
    dtc_count: int
    last_dtc_code: int
    fdcan_recovery_count: int
    max_flash_op_ms: int

    @classmethod
    def parse(cls, record: bytes) -> "Health":
        if len(record) < 32:
            raise ValueError(f"health record of {len(record)} B, expected 32")
        return cls(*struct.unpack_from("<8I", record))

    @property
    def wrp_protected(self) -> bool:
        return bool(self.flags & HEALTH_FLAG_WRP_PROTECTED)


@dataclass(frozen=True)
class ObStatus:
    """OB_READ's 16-byte bl_ob_status_t (bl_obyte.h:38-45), filled from
    HAL_FLASHEx_OBGetConfig (bl_obyte.c:19-34): wrp_sector_mask has bit N set
    when sector N is write-protected (WPSN_CUR1 inverted), user_config is
    OPTSR_CUR without its BOR and RDP fields, rdp_level and bor_level the low
    bytes of the HAL's RDPLevel and BORLevel."""
    wrp_sector_mask: int
    user_config: int
    rdp_level: int
    bor_level: int

    @classmethod
    def parse(cls, record: bytes) -> "ObStatus":
        if len(record) < 16:
            raise ValueError(f"option-byte record of {len(record)} B, expected 16")
        mask, user, rdp, bor = struct.unpack_from("<IIBB", record)
        return cls(mask, user, rdp, bor)


class Nack(Exception):
    def __init__(self, opcode: int, code: Optional[int]):
        super().__init__(f"NACK to opcode {opcode:#04x}: code {code if code is None else hex(code)}")
        self.opcode, self.code = opcode, code


def check_app_range(start: int, length: int) -> None:
    """Refuse anything outside sectors 1..6, the only range the bootloader
    lets a host change (bl_flash.c bl_flash_range_is_writable)."""
    if length <= 0 or start < APP_BASE or start + length > APP_END + 1:
        raise ValueError(f"[{start:#010x}, +{length:#x}) is outside the app sectors "
                         f"[{APP_BASE:#010x}, {APP_END + 1:#010x}): refusing to touch it")


def sectors_of(start: int, length: int) -> list[int]:
    """Base addresses of the 128 KB sectors covering [start, start + length)."""
    check_app_range(start, length)
    first = (start - FLASH_BASE) // SECTOR_SIZE
    last = (start + length - 1 - FLASH_BASE) // SECTOR_SIZE
    return [FLASH_BASE + s * SECTOR_SIZE for s in range(first, last + 1)]


def pad_flashword(data: bytes) -> bytes:
    return data + b"\xFF" * (-len(data) % FLASHWORD)


@dataclass(frozen=True)
class FlashReport:
    sectors: list[int]
    size: int
    crc: int
    version: int
    t_start_us: int
    t_verified_us: int


class CanBootloader:
    """One node's bootloader, reached over one Sim bus.

        bl = CanBootloader(sim, "can_acu", node=2)
        assert [d.node for d in bl.discover()] == [2]
        bl.flash(app_bytes, jump=True)
    """

    def __init__(self, sim, bus: str, node: int, *, frame_us: int = FRAME_US,
                 burst: int = BURST, timeout_ms: float = 1000):
        self.sim, self.bus, self.node = sim, bus, node
        self.can = sim.can(bus)
        self.frame_us, self.burst, self.timeout_ms = frame_us, burst, timeout_ms
        rx_id(node)                                   # validates the node

    # -- transport -------------------------------------------------------------

    def send(self, message: bytes, dst: Optional[int] = None) -> int:
        """Send one message (type byte first), one frame every frame_us, and
        run until its last frame is on the wire; returns the virtual time (us)
        of its first frame."""
        frames = segment(message)
        dst = self.node if dst is None else dst
        t0 = self.sim.now_us()
        self.can.send_sequence([(tx_id(dst), f) for f in frames], self.frame_us, self.burst)
        self.sim.run_for(us=self.frame_us * len(frames))
        return t0

    def replies(self, since_us: int, node: Optional[int] = None) -> list[Reply]:
        """Every complete message a node sent at or after since_us."""
        node = self.node if node is None else node
        out, rx = [], Reassembler()
        for f in self.can.frames([rx_id(node)], since_us):
            try:
                message = rx.feed(f.data)
            except IsoTpError:
                # The tail of a message that started before since_us (a
                # heartbeat's CF): drop it and resynchronise on the next SF/FF.
                rx = Reassembler()
                continue
            if message is not None:
                out.append(parse_reply(f.t_us, message))
        return out

    def request(self, opcode: int, args: bytes = b"", *, msg_type: int = MSG_CMD,
                timeout_ms: Optional[float] = None) -> Reply:
        """Send a command and wait (in virtual time) for its ACK or NACK;
        notifications in between are skipped. Raises TimeoutError."""
        t0 = self.send(bytes([msg_type, opcode]) + args)
        timeout_ms = self.timeout_ms if timeout_ms is None else timeout_ms
        deadline = t0 + int(timeout_ms * 1000)
        while True:
            for r in self.replies(t0):
                if r.msg_type in (MSG_ACK, MSG_NACK) and r.opcode in (opcode, 0x00, 0xFF):
                    return r
            if self.sim.now_us() >= deadline:
                raise TimeoutError(f"no reply to opcode {opcode:#04x} from node {self.node:#x} "
                                   f"within {timeout_ms} ms")
            self.sim.run_for(ms=1)

    def command(self, opcode: int, args: bytes = b"", **kwargs) -> bytes:
        """request(), but a NACK raises Nack; returns the ACK's data."""
        r = self.request(opcode, args, **kwargs)
        if r.msg_type == MSG_NACK:
            raise Nack(r.opcode, r.code)
        return r.data

    # -- operations --------------------------------------------------------------

    def discover(self, window_ms: float = 50) -> list[Discovered]:
        """Broadcast DISCOVER and collect every reply in the window
        (bl_proto.c:441-452; can-flasher session/mod.rs:780-805)."""
        t0 = self.send(bytes([MSG_DISCOVER_REQUEST, DISCOVER]), dst=NODE_BROADCAST)
        self.sim.run_for(ms=window_ms)
        found = []
        for node in range(0x1, 0xF):
            for r in self.replies(t0, node):
                if r.msg_type == MSG_DISCOVER_REPLY and r.opcode == DISCOVER and len(r.data) >= 3:
                    found.append(Discovered(r.data[0], r.data[1], r.data[2], r.t_us))
        return found

    def connect(self) -> tuple[int, int]:
        data = self.command(CONNECT, bytes([PROTO_MAJOR, PROTO_MINOR]))
        return data[0], data[1]

    def disconnect(self) -> None:
        self.command(DISCONNECT)

    def fw_info(self) -> FwInfo:
        return FwInfo.parse(self.command(GET_FW_INFO))

    def health(self) -> Health:
        return Health.parse(self.command(GET_HEALTH))

    def ob_read(self) -> ObStatus:
        """OB_READ: not session-gated (bl_proto.c:1065-1082)."""
        return ObStatus.parse(self.command(OB_READ))

    def apply_wrp(self, sector_mask: int = 0x01, token: int = OB_APPLY_TOKEN) -> None:
        """OB_APPLY_WRP: write-protect sector 0 in the option bytes, one way
        over CAN (bl_proto.c:1084-1172). Session-gated; the bootloader ACKs
        before it programs the option bytes, and refuses any mask but sector 0
        (NACK_UNSUPPORTED) or a wrong token (NACK_OB_WRONG_TOKEN)."""
        self.command(OB_APPLY_WRP, struct.pack("<II", token, sector_mask))

    def erase(self, start: int, length: int) -> None:
        check_app_range(start, length)
        self.command(FLASH_ERASE, struct.pack("<II", start, length), timeout_ms=10_000)

    def write(self, addr: int, data: bytes) -> None:
        check_app_range(addr, len(data))
        self.command(FLASH_WRITE, struct.pack("<I", addr) + data)

    def read_crc(self, addr: int, length: int) -> int:
        data = self.command(FLASH_READ_CRC, struct.pack("<II", addr, length))
        return struct.unpack_from("<I", data)[0]

    def verify(self, crc: int, size: int, version: int = 0) -> None:
        self.command(FLASH_VERIFY, struct.pack("<III", crc, size, version))

    def jump(self) -> None:
        self.command(JUMP, struct.pack("<I", APP_BASE))

    def reset(self, mode: int) -> None:
        self.command(RESET, bytes([mode]))

    def flash(self, image: bytes, *, version: Optional[int] = None, jump: bool = True,
              chunk: int = WRITE_CHUNK) -> FlashReport:
        """can-flasher's flash flow without diff mode (flash/mod.rs:286-380):
        CONNECT; per sector erase, 256 B writes, CRC read-back; FLASH_VERIFY
        over the whole image; then JUMP, or DISCONNECT to leave the board in
        the bootloader. version defaults to the image's firmware-info record."""
        sectors = sectors_of(APP_BASE, len(image))
        if version is None:
            info = FwInfo.from_image(image)
            version = info.packed_version if info else 0
        t_start = self.sim.now_us()
        self.connect()
        for base in sectors:
            offset = base - APP_BASE
            body = image[offset:offset + SECTOR_SIZE]
            self.erase(base, SECTOR_SIZE)
            for i in range(0, len(body), chunk):
                self.write(base + i, pad_flashword(body[i:i + chunk]))
            expected = crc32(body + b"\xFF" * (SECTOR_SIZE - len(body)))
            got = self.read_crc(base, SECTOR_SIZE)
            if got != expected:
                raise AssertionError(f"sector {base:#010x}: CRC {got:#010x}, expected {expected:#010x}")
        self.verify(crc32(image), len(image), version)
        t_verified = self.sim.now_us()
        if jump:
            self.jump()
        else:
            self.disconnect()
        return FlashReport(sectors, len(image), crc32(image), version, t_start, t_verified)
