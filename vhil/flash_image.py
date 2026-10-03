"""The flash image a provisioned MLC board holds: what an SWD bootloader burn
plus a `cf flash` leave behind, for the CAN bootloader (isc-fs/
stm32-can-bootloader, docs ARCHITECTURE.md "Memory map" and "Application
metadata record"; Core/Inc/bl_memmap.h, bl_provision.h).

  0x08000000  bootloader        sector 0
  0x08020000  application       sectors 1-6
  0x080E0000  NVM KV store      sector 7, erased: the bootloader formats it
  0x080FFFC0  provisioning seed one FLASHWORD: node id, consumed on first boot
  0x080FFFE0  app metadata      one FLASHWORD: magic, size, CRC-32, base

Everything not written reads 0xFF, as erased flash does.
"""
from __future__ import annotations

import struct
import zlib

FLASH_BASE = 0x08000000
FLASH_SIZE = 0x100000                 # STM32H733ZG: 1 MB, one bank
APP_BASE = 0x08020000
APP_MAX = 0x080E0000 - APP_BASE       # sectors 1-6, 768 KB
SEED_ADDR, SEED_MAGIC = 0x080FFFC0, 0xB0070D1D
META_ADDR, META_MAGIC = 0x080FFFE0, 0xB007C0DE


def seed(node_id: int) -> bytes:
    """bl_provision_seed_t: magic, node_id, ~node_id, 0xFFFF, CRC-32 over the
    first 8 bytes, 0xFF padding to one FLASHWORD."""
    if not 0x1 <= node_id <= 0xE:
        raise ValueError(f"node id {node_id:#x} outside 0x1..0xE")
    head = struct.pack("<IBBH", SEED_MAGIC, node_id, node_id ^ 0xFF, 0xFFFF)
    return head + struct.pack("<I", zlib.crc32(head)) + b"\xFF" * 20


def metadata(app: bytes, version: int = 0) -> bytes:
    """The app metadata record the bootloader stamps after a verified write:
    magic, size, IEEE CRC-32 over the image, base, version, 3 reserved."""
    return struct.pack("<8I", META_MAGIC, len(app), zlib.crc32(app), APP_BASE, version, 0, 0, 0)


def build(bootloader: bytes, app: bytes, node_id: int, version: int = 0) -> bytes:
    if len(bootloader) > APP_BASE - FLASH_BASE:
        raise ValueError(f"bootloader is {len(bootloader)} B, sector 0 holds 128 KB")
    if len(app) > APP_MAX:
        raise ValueError(f"application is {len(app)} B, sectors 1-6 hold 768 KB")
    image = bytearray(b"\xFF" * FLASH_SIZE)
    image[0:len(bootloader)] = bootloader
    offset = APP_BASE - FLASH_BASE
    image[offset:offset + len(app)] = app
    image[SEED_ADDR - FLASH_BASE:SEED_ADDR - FLASH_BASE + 32] = seed(node_id)
    image[META_ADDR - FLASH_BASE:META_ADDR - FLASH_BASE + 32] = metadata(app, version)
    return bytes(image)
