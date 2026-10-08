"""ams-logfs: log extraction over CAN (LOGFS, IFS08-CE-AMS #406/#439), and the
vehicle-state gate that keeps it off a live car.

AMS facts (IFS08-CE-AMS, Core/ unless shown):
  acu_can_task.cpp:431-496: ISO-TP on 0x002 (host -> node 2) and 0x012
    (node -> host), the bootloader's addressing; a complete request goes to
    SdLoggerTask (sd_diag_submit), BUSY when one is already being served.
  diag_proto.hpp: a message is [msg_type][opcode][args]; LOGFS rides
    APP_CTRL (0x06); replies are ACK (0x01) [opcode][data] or NACK (0x02)
    [opcode][code]. CONNECT 0x01 (ACK [major 1, minor 0]), DISCONNECT 0x02,
    LIST 0x21, OPEN 0x22, READ 0x23, CRC 0x24, CLOSE 0x25, FINALIZE 0x27;
    0x26 (DELETE) is deliberately unimplemented: read-only. NACK codes
    FILE_NOT_FOUND 0x04, BAD_SESSION 0x06, BAD_HANDLE 0x11,
    VEHICLE_STATE 0x15, UNSUPPORTED 0xFE.
  diag_dispatch.hpp:59-61, 71-127: anything but APP_CTRL gets no reply (the
    app never answers in the bootloader's namespace); everything but
    CONNECT/DISCONNECT needs a session; a LOGFS opcode is refused with
    VEHICLE_STATE unless the FSM (g_state_telemetry, sd_logger_task.cpp:565)
    is in Start or Error, the states with the contactors open. The reason
    (diag_dispatch.hpp:29-43): 0x012 out-prioritises the VCU heartbeat 0x100,
    so a pull in Run could starve it into VcuStale and drop the AIRs.
  logfs_server.hpp:34-48, 158-305: LIST [cursor:u16] -> [next:u16][count]
    [22-byte entries {index:u16, size:u32, mtime:u32, name[12]}]; OPEN
    [index:u16] -> [handle:u16][size:u32][crc32:u32] (0 = ask CRC); READ
    [handle][offset:u32][len:u16] -> up to 512 bytes, short = EOF; FINALIZE
    seals the active log (LOGnnnn.CSV + .CRC) and returns its index.
"""
import shutil
import struct
import subprocess
import zlib

import pytest

from ams_car import ERROR, PRECHARGE, RUN, START, Car
from vhil.can_bootloader import CanBootloader
from vhil.sim import Sim
from vhil.system import REPO

APP_CTRL, MSG_CMD, ACK, NACK = 0x06, 0x00, 0x01, 0x02
CONNECT, DISCONNECT = 0x01, 0x02
LIST, OPEN, READ, CRC, CLOSE, DELETE, FINALIZE = 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27
FILE_NOT_FOUND, BAD_SESSION, BAD_HANDLE, VEHICLE_STATE, UNSUPPORTED = 0x04, 0x06, 0x11, 0x15, 0xFE
CURSOR_END, ENTRY, MAX_READ = 0xFFFF, 22, 512
CARD_BYTES = 0x8000000          # catalog/models/sd-card.yaml capacity: 128 MiB


def _tool(name):
    path = shutil.which(name)
    if path is None:
        pytest.fail(f"{name} not installed (dosfstools / mtools)")
    return path


def _card(tmp_path):
    img = tmp_path / "card.img"
    with open(img, "wb") as f:
        f.truncate(CARD_BYTES)
    subprocess.run([_tool("mkfs.fat"), "-F", "32", "-s", "1", "-n", "AMS", str(img)],
                   check=True, capture_output=True)
    return img


def _read(img, name):
    return subprocess.run([_tool("mtype"), "-i", str(img), f"::/{name}"],
                          check=True, capture_output=True).stdout


class Logfs:
    """A pit tool's LOGFS client: APP_CTRL requests to node 2 on the ACU bus,
    over the bootloader client's ISO-TP (the same addressing)."""

    def __init__(self, sim):
        self.bl = CanBootloader(sim, "can_acu", node=2, timeout_ms=500)

    def __call__(self, opcode, args=b"", msg_type=APP_CTRL):
        r = self.bl.request(opcode, args, msg_type=msg_type)
        return r.msg_type, (r.code if r.msg_type == NACK else r.data)

    def ok(self, opcode, args=b""):
        kind, body = self(opcode, args)
        assert kind == ACK, f"opcode {opcode:#04x} NACKed {body:#04x}"
        return body

    def list_all(self):
        entries, cursor = [], 0
        while cursor != CURSOR_END:
            body = self.ok(LIST, struct.pack("<H", cursor))
            cursor, count = struct.unpack_from("<HB", body)
            for k in range(count):
                index, size, _mtime = struct.unpack_from("<HII", body, 3 + k * ENTRY)
                name = body[3 + k * ENTRY + 10:3 + (k + 1) * ENTRY].rstrip(b"\0").decode()
                entries.append((index, size, name))
        return entries

    def pull(self, index):
        handle, size, crc = struct.unpack("<HII", self.ok(OPEN, struct.pack("<H", index)))
        data = b""
        while True:
            chunk = self.ok(READ, struct.pack("<HIH", handle, len(data), MAX_READ))
            data += chunk
            if len(chunk) < MAX_READ:
                break
        whole = struct.unpack("<I", self.ok(CRC, struct.pack("<H", handle)))[0]
        self.ok(CLOSE, struct.pack("<H", handle))
        return handle, size, crc, whole, data


@pytest.fixture
def ams(images, tmp_path):
    img = _card(tmp_path)
    with Sim(REPO / "systems" / "ams.yaml", images("ams"), params={"sd": {"image": str(img)}},
             card_dirs=[img.parent]) as sim:
        sim.wait_for_app()
        sim.run_for(ms=3000)                    # past the boot grace, logging
        sim.img = img
        yield sim


def test_nothing_is_served_without_a_session(ams):
    """A stray LIST is refused BAD_SESSION; after DISCONNECT, again."""
    fs = Logfs(ams)
    assert fs(LIST, struct.pack("<H", 0)) == (NACK, BAD_SESSION)
    assert fs.ok(CONNECT) == bytes([1, 0]), "CONNECT carries the APP_CTRL protocol 1.0"
    fs.ok(LIST, struct.pack("<H", 0))
    fs.ok(DISCONNECT)
    assert fs(LIST, struct.pack("<H", 0)) == (NACK, BAD_SESSION)


def test_the_bootloader_namespace_gets_no_answer(ams):
    """A CMD-typed CONNECT (the bootloader's namespace) is not the app's to
    answer, even to refuse it: silence (diag_dispatch.hpp:9-11, 80)."""
    fs = Logfs(ams)
    with pytest.raises(TimeoutError):
        fs(CONNECT, msg_type=MSG_CMD)
    assert Car(ams).state() == START, "the CMD frame disturbed the AMS"


def test_extraction_is_read_only(ams):
    """DELETE (0x26) is not implemented: UNSUPPORTED, in a session and in Start."""
    fs = Logfs(ams)
    fs.ok(CONNECT)
    assert fs(DELETE, struct.pack("<H", 0)) == (NACK, UNSUPPORTED)


def test_a_finalized_log_is_listed_and_pulled_whole(ams):
    """FINALIZE seals the active log; LIST shows it; OPEN + READ to the short
    read + CRC give the file the card holds, byte for byte, and the CRC the
    sidecar carries (OPEN's crc, 0 meaning 'ask CRC')."""
    fs = Logfs(ams)
    fs.ok(CONNECT)
    sealed = struct.unpack("<H", fs.ok(FINALIZE))[0]
    entries = fs.list_all()
    match = [e for e in entries if e[0] == sealed]
    assert match, f"sealed index {sealed} not in {entries}"
    _, size, name = match[0]
    handle, open_size, open_crc, whole, data = fs.pull(sealed)
    assert handle != 0 and open_size == size == len(data), (handle, open_size, size, len(data))
    assert whole == zlib.crc32(data) and open_crc in (0, whole)
    assert fs(READ, struct.pack("<HIH", handle, 0, 8)) == (NACK, BAD_HANDLE), "a closed handle read"
    assert fs(OPEN, struct.pack("<H", 0xFFFE)) == (NACK, FILE_NOT_FOUND)
    ams.stop()
    assert _read(ams.img, name) == data, f"{name} on the card differs from the pull"


def test_extraction_is_refused_with_the_tractive_system_live(ams):
    """In Precharge and Run every LOGFS opcode is refused VEHICLE_STATE (the
    0x012 replies would out-prioritise the VCU heartbeat); CONNECT still
    answers, so the tool learns why."""
    car = Car(ams)
    fs = Logfs(ams)
    car.arm()
    assert car.state() == PRECHARGE
    assert fs.ok(CONNECT) == bytes([1, 0])
    assert fs(LIST, struct.pack("<H", 0)) == (NACK, VEHICLE_STATE)
    car.vcu(round(95 * 3.7))
    assert car.wait_for(RUN, 200) is not None
    for opcode, args in [(LIST, struct.pack("<H", 0)), (OPEN, struct.pack("<H", 0)),
                         (FINALIZE, b"")]:
        assert fs(opcode, args) == (NACK, VEHICLE_STATE), f"opcode {opcode:#04x} served in Run"
    assert car.state() == RUN, "the refused pull disturbed Run"


def test_extraction_is_served_in_error(ams):
    """Error is the state the feature exists for (diag_dispatch.hpp:49-58):
    a cell under-voltage latches it, and the log is listed and sealed."""
    fs = Logfs(ams)
    ams.monitor("sysbus.spi1.isospi.cells3 SetCell 2 2500", board="ams")
    car = Car(ams)
    assert car.wait_for(ERROR, 1000) is not None, f"no Error (state {car.state()})"
    fs.ok(CONNECT)
    sealed = struct.unpack("<H", fs.ok(FINALIZE))[0]
    assert any(e[0] == sealed for e in fs.list_all())
