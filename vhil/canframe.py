"""A classic CAN frame's bits on the wire, and bus load from them (#174).

The same computation as VhilCanFrameBits in models/renode/Stm32H7Fdcan.cs,
which the arbitrated bus (models/renode/VhilCanBus.cs) times frames with:
tests check one against the other, and the worker estimates the load of a bus
Renode's hub carries (no timing of its own) from the frames it saw.

Frame layout (ISO 11898-1; Bosch CAN 2.0B part B section 3): SOF, arbitration
field (11-bit ID, RTR, IDE / 11-bit base ID, SRR, IDE, 18-bit extension, RTR),
control (r0 / r1 r0, DLC), data, CRC-15; then CRC delimiter, ACK slot, ACK
delimiter and 7 bits of EOF; 3 bits of intermission before the bus is idle.
Stuffing (a complementary bit after five equal bits) runs from SOF to the end
of the CRC sequence.
"""
from __future__ import annotations

from typing import Iterable

INTERMISSION = 3
TAIL = 3 + 7          # CRC delimiter, ACK slot, ACK delimiter, EOF
CRC_POLY = 0x4599     # x^15 + x^14 + x^10 + x^8 + x^7 + x^4 + x^3 + 1


def _push(bits: list[int], value: int, width: int) -> None:
    bits.extend((value >> i) & 1 for i in range(width - 1, -1, -1))


def crc15(bits: Iterable[int]) -> int:
    crc = 0
    for bit in bits:
        nxt = bit ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= CRC_POLY
    return crc


def stream(can_id: int, data: bytes = b"", extended: bool = False, remote: bool = False) -> list[int]:
    """Unstuffed bits from SOF through the CRC sequence."""
    data = bytes(data)[:8]
    bits = [0]                                   # SOF
    if extended:
        _push(bits, can_id >> 18, 11)
        bits += [1, 1]                           # SRR, IDE
        _push(bits, can_id & 0x3FFFF, 18)
        bits += [int(remote), 0, 0]              # RTR, r1, r0
    else:
        _push(bits, can_id & 0x7FF, 11)
        bits += [int(remote), 0, 0]              # RTR, IDE, r0
    _push(bits, len(data), 4)
    if not remote:
        for b in data:
            _push(bits, b, 8)
    _push(bits, crc15(bits), 15)
    return bits


def stuff_bits(bits: list[int]) -> int:
    """Stuff bits a transmitter inserts in `bits`."""
    count, run, last = 0, 0, None
    for b in bits:
        if b == last:
            run += 1
        else:
            run, last = 1, b
        if run == 5:
            count += 1
            run, last = 1, 1 - last          # the stuff bit starts the next run
    return count


def frame_bits(can_id: int, data: bytes = b"", extended: bool = False, remote: bool = False) -> int:
    """SOF through the last EOF bit, stuff bits included (no intermission)."""
    bits = stream(can_id, data, extended, remote)
    return len(bits) + stuff_bits(bits) + TAIL


def busy_bits(can_id: int, data: bytes = b"", extended: bool = False, remote: bool = False) -> int:
    """What a frame occupies the bus for: the frame and its intermission."""
    return frame_bits(can_id, data, extended, remote) + INTERMISSION


def arbitration(can_id: int, extended: bool = False, remote: bool = False) -> list[int]:
    """The arbitration field as sent: lower (more dominant) wins."""
    if extended:
        bits: list[int] = []
        _push(bits, can_id >> 18, 11)
        bits += [1, 1]
        _push(bits, can_id & 0x3FFFF, 18)
        return bits + [int(remote)]
    bits = []
    _push(bits, can_id & 0x7FF, 11)
    return bits + [int(remote), 0]


def wins(a: tuple, b: tuple) -> bool:
    """Whether frame a = (id, extended, remote) wins arbitration against b."""
    return arbitration(*a) < arbitration(*b)


def load(frames: Iterable, window_us: float, bitrate: int = 500_000) -> float:
    """Bus load of `frames` (objects with .id, .data, .extended) seen in a
    window: their bits and intermissions over the window's bits. An estimate
    for a bus without timing (Renode's hub): it assumes every frame was at
    `bitrate` and none was retried."""
    if window_us <= 0:
        return 0.0
    bits = sum(busy_bits(f.id, f.data, f.extended) for f in frames)
    return bits / (window_us * 1e-6 * bitrate)
