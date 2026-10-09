"""Decoder for the AMS's split-rate binary logs: IMUnnnn.BIN, CELnnnn.BIN and
ELEnnnn.BIN, written beside LOGnnnn.CSV (IFS08-CE-AMS #596/#598/#601/#603).
Shared by the ams-sd and ams-imu suites.

Format (IFS08-CE-AMS dev, bin_log.hpp:7-37, :53-58, :82-101):
  a 512-byte header, then fixed-size little-endian records, no separators
  header: 0 magic "AMSBIN1\\0", 8 u16 format version (1), 10 u16 record
    size, 12 u32 rotation index (shared with LOGnnnn.CSV), 16 u32 tick_ms at
    open, 20 char[8] stream name, 28 u8[3] firmware version, 31 u8 0,
    32 u8[4] git hash, 36 reserved, 64 char[448] schema, NUL-terminated
  schema: one line per field, in record order, no padding:
    <name> <type> <count> <scale> <unit> [<flags>]
    type u8 i8 u16 i16 u32 i32; count 1, N (name0..) or AxB (nameA_B,
    row-major); scale a decimal or "a/b"; flag "z" = a raw 0 is "not measured"
The decoder reads the schema the file carries, as the firmware's own
tools/log_decode.py does, so it needs no per-stream layout; the tests pin
each stream's schema text against the firmware's (bin_log.hpp:108-112 IMU,
:153-163 CEL, :209-219 ELE).
"""
import struct
from dataclasses import dataclass, field
from fractions import Fraction

from vhil import elf

HEADER_BYTES = 512
MAGIC = b"AMSBIN1\0"
FORMAT_VERSION = 1
SCHEMA_OFFSET = 64
_TYPES = {"u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i"}

# The firmware's schema texts, verbatim (bin_log.hpp:109-112, :154-163, :210-219).
IMU_SCHEMA = ("tick_ms u32 1 1 ms\n"
              "a i16 3 6/32768 g\n"
              "g i16 3 8.726646259971648/32768 rad/s\n")
CEL_SCHEMA = ("t_adcv_ms u32 1 1 ms\n"
              "i_n u16 1 1 -\n"
              "i_span_us u16 1 1 us\n"
              "i i32 1 0.001 A\n"
              "seq u16 1 1 -\n"
              "ltc_ok u16 1 1 -\n"
              "attempt u8 1 1 -\n"
              "flags u8 1 1 -\n"
              "c u16 5x19 1 mV z\n")
ELE_SCHEMA = ("tick_ms u32 1 1 ms\n"
              "seq u16 1 1 -\n"
              "n u8 1 1 -\n"
              "flags u8 1 1 -\n"
              "i_mean i32 1 0.001 A\n"
              "i_min i32 1 0.001 A\n"
              "i_max i32 1 0.001 A\n"
              "dcbus_V u16 1 1 V\n"
              "dcbus_age_ms u16 1 1 ms\n")
# Record sizes the firmware static_asserts (bin_log.hpp:117, :176, :230).
RECORD_BYTES = {"IMU": 16, "CEL": 208, "ELE": 24}


def split_rate(sim) -> bool:
    """The AMS build logs IMU/CEL/ELE .BIN files beside LOG.CSV (AMS dev:
    sd_logger_task.cpp:179-185 g_bin_files). main logged the IMU to
    IMUnnnn.CSV and nothing else, which the tests no longer read."""
    return "_ZN12_GLOBAL__N_1L11g_bin_filesE" in elf.symbols(sim.firmware["ams"])


@dataclass
class Field:
    name: str
    type: str
    shape: tuple          # () scalar, (n,) vector, (a, b) matrix
    scale: Fraction
    unit: str
    flags: str = ""

    @property
    def count(self) -> int:
        n = 1
        for d in self.shape:
            n *= d
        return n


@dataclass
class BinLog:
    version: int
    record_size: int
    index: int
    open_tick_ms: int
    stream: str
    fw_version: tuple
    git_hash: bytes
    schema: str
    fields: list
    records: list = field(default_factory=list)   # dicts of raw (unscaled) values
    tail: int = 0                                  # bytes after the last whole record


def _scale(text: str) -> Fraction:
    if "/" in text:
        a, b = text.split("/")
        return Fraction(a) / Fraction(b)
    return Fraction(text)


def parse_schema(text: str) -> list:
    fields = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        name, typ, count, scale, unit = parts[:5]
        if typ not in _TYPES:
            raise ValueError(f"schema: unknown type {typ!r} in {line!r}")
        if "x" in count:
            a, b = count.split("x")
            shape = (int(a), int(b))
        else:
            shape = () if int(count) == 1 else (int(count),)
        fields.append(Field(name, typ, shape, _scale(scale), unit, parts[5] if len(parts) > 5 else ""))
    return fields


def decode(data: bytes) -> BinLog:
    """The header and every whole record of a .BIN file. Raises ValueError on
    a header that is not the firmware's."""
    if len(data) < HEADER_BYTES:
        raise ValueError(f"{len(data)} bytes: shorter than the {HEADER_BYTES}-byte header")
    if data[:8] != MAGIC:
        raise ValueError(f"magic {data[:8]!r}")
    version, record_size, index, open_tick = struct.unpack_from("<HHII", data, 8)
    stream = data[20:28].split(b"\0")[0].decode("ascii")
    schema = data[SCHEMA_OFFSET:HEADER_BYTES].split(b"\0")[0].decode("ascii")
    fields = parse_schema(schema)
    fmt = "<" + "".join(f"{f.count}{_TYPES[f.type]}" for f in fields)
    if struct.calcsize(fmt) != record_size:
        raise ValueError(f"schema is {struct.calcsize(fmt)} bytes, header says {record_size}")
    log = BinLog(version, record_size, index, open_tick, stream, tuple(data[28:31]),
                 bytes(data[32:36]), schema, fields)
    body = data[HEADER_BYTES:]
    whole = len(body) // record_size
    log.tail = len(body) - whole * record_size
    for values in struct.iter_unpack(fmt, body[:whole * record_size]):
        rec, i = {}, 0
        for f in fields:
            chunk = list(values[i:i + f.count])
            i += f.count
            if not f.shape:
                rec[f.name] = chunk[0]
            elif len(f.shape) == 1:
                rec[f.name] = chunk
            else:
                a, b = f.shape
                rec[f.name] = [chunk[r * b:(r + 1) * b] for r in range(a)]
        log.records.append(rec)
    return log


def scaled(log: BinLog, name: str, raw):
    """A raw value (or list of them) of field `name` in its unit, as a float."""
    s = next(f.scale for f in log.fields if f.name == name)
    if isinstance(raw, list):
        return [scaled(log, name, r) for r in raw]
    return float(raw * s)
