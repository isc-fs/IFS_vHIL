"""A classic CAN frame's bits on the wire (vhil/canframe.py), the reference
for the arbitrated bus's VhilCanFrameBits (models/renode/Stm32H7Fdcan.cs;
tests/sim/test_can_bus.py checks the two agree on real frames)."""
import pytest

from vhil import canframe


def _bits(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(7, -1, -1)]


def test_crc15_is_crc15_can():
    # CRC-15/CAN: poly 0x4599, init 0, no reflection; check value over
    # "123456789" is 0x059E (the CRC RevEng catalogue).
    assert canframe.crc15(_bits(b"123456789")) == 0x059E


@pytest.mark.parametrize("bits, stuffed", [
    ([0] * 4, 0),
    ([0] * 5, 1),                  # 00000 -> 000001
    ([0] * 10, 2),                 # the stuff bit starts no run of zeros
    ([0] * 5 + [1] * 4, 2),        # the stuff bit (1) and four 1s make five
    ([0, 1] * 20, 0),
    ([1] * 5 + [0] * 5 + [1] * 5, 3),
])
def test_stuff_bits_after_five_equal_bits(bits, stuffed):
    assert canframe.stuff_bits(bits) == stuffed


@pytest.mark.parametrize("ext", [False, True])
@pytest.mark.parametrize("n", range(9))
def test_frame_bits_lie_between_the_unstuffed_and_worst_case_lengths(ext, n):
    """44 + 8n bits for a standard frame (64 + 8n extended) without stuffing;
    at most floor((g + 8n - 1) / 4) stuff bits, g = 34 (54 extended) bits
    from SOF through the CRC that stuffing covers (Davis et al., Controller
    Area Network schedulability analysis, 2007, eq. 3)."""
    base, g = (64, 54) if ext else (44, 34)
    for can_id, fill in ((0, 0x00), (0x7FF, 0xFF), (0x555, 0xAA), (0x100, 0x0F)):
        if ext:
            can_id = (can_id << 18) | 0x2AAAA
        bits = canframe.frame_bits(can_id, bytes([fill]) * n, extended=ext)
        assert base + 8 * n <= bits <= base + 8 * n + (g + 8 * n - 1) // 4


def test_an_all_zero_frame_is_stuffed():
    # SOF, ID 0x000, RTR, IDE, r0, DLC 0: 19 dominant bits in a row get 4
    # stuff bits before the CRC even starts.
    assert canframe.frame_bits(0x000) > 44 + 3


def test_busy_bits_add_the_intermission():
    assert canframe.busy_bits(0x100, b"\x01") == canframe.frame_bits(0x100, b"\x01") + 3


@pytest.mark.parametrize("a, b", [
    ((0x100, False, False), (0x101, False, False)),       # lower ID
    ((0x100, False, False), (0x100, False, True)),        # data before remote
    ((0x100, False, False), (0x100 << 18, True, False)),  # standard before extended (RTR vs SRR)
    ((0x100, False, True), (0x100 << 18, True, False)),   # ... even remote (IDE decides)
    ((0x0FF << 18 | 5, True, False), (0x100, False, False)),  # base ID first
    (((0x100 << 18) | 1, True, False), ((0x100 << 18) | 2, True, False)),
])
def test_arbitration_follows_the_wire_order(a, b):
    assert canframe.wins(a, b) and not canframe.wins(b, a)


def test_load_is_bits_over_the_window():
    class F:
        id, data, extended = 0x100, b"\x01", False
    bits = canframe.busy_bits(0x100, b"\x01")
    assert canframe.load([F()] * 10, 10_000) == pytest.approx(10 * bits / 5000)
    assert canframe.load([], 10_000) == 0 and canframe.load([F()], 0) == 0
