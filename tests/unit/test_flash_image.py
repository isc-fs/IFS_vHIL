"""The provisioned flash image, against the bootloader's documented layout."""
import struct
import zlib

import pytest

from vhil import flash_image as fi


def test_layout_matches_the_bootloader_memory_map():
    bl, app = b"\x11" * 1000, bytes(range(256)) * 40
    image = fi.build(bl, app, node_id=2, version=7)
    assert len(image) == 0x100000
    assert image[:1000] == bl and image[1000:0x20000] == b"\xFF" * (0x20000 - 1000)
    assert image[0x20000:0x20000 + len(app)] == app
    assert set(image[0xE0000:0xFFFC0]) == {0xFF}                  # NVM left erased
    magic, size, crc, base, version, *rest = struct.unpack_from("<8I", image, 0xFFFE0)
    assert (magic, size, crc, base, version, rest) == \
        (0xB007C0DE, len(app), zlib.crc32(app), 0x08020000, 7, [0, 0, 0])


def test_seed_is_a_valid_bl_provision_seed():
    s = fi.seed(0x2)
    magic, node, check, reserved, crc = struct.unpack_from("<IBBHI", s)
    assert (magic, node, node ^ check, reserved) == (0xB0070D1D, 2, 0xFF, 0xFFFF)
    assert crc == zlib.crc32(s[:8]) and s[12:] == b"\xFF" * 20 and len(s) == 32


@pytest.mark.parametrize("node_id", [0, 0xF])
def test_node_id_out_of_range_is_refused(node_id):
    with pytest.raises(ValueError):
        fi.seed(node_id)


def test_oversized_images_are_refused():
    with pytest.raises(ValueError, match="sector 0"):
        fi.build(b"\0" * 0x20001, b"", 1)
    with pytest.raises(ValueError, match="768 KB"):
        fi.build(b"", b"\0" * (0xC0000 + 1), 1)
