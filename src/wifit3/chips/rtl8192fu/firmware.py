from __future__ import annotations

import time
from dataclasses import dataclass
from importlib.resources import files

from . import constants as C
from .transport import RTL8192FUTransport


_ACCEPTED_SIGNATURES = {0x92E0, 0x92C0, 0x88E0, 0x88C0, 0x5300, 0x2300, 0x88F0, 0x10B0, 0x92F0}


@dataclass(frozen=True)
class FirmwareImage:
    signature: int
    version: int
    subversion: int
    payload: bytes


def load_firmware_blob() -> bytes:
    return files(__package__).joinpath("assets/rtl8192fufw.bin").read_bytes()


def parse_firmware(blob: bytes) -> FirmwareImage:
    if len(blob) < C.FW_HEADER_SIZE:
        raise ValueError("RTL8192FU firmware is shorter than its 32-byte header")
    signature = int.from_bytes(blob[0:2], "little")
    if signature & 0xFFF0 not in _ACCEPTED_SIGNATURES:
        raise ValueError(f"unsupported RTL8192FU firmware signature: {signature:#06x}")
    return FirmwareImage(
        signature=signature,
        version=int.from_bytes(blob[4:6], "little"),
        subversion=blob[6],
        payload=blob[C.FW_HEADER_SIZE:],
    )


def reset_8051(transport: RTL8192FUTransport) -> None:
    value = transport.read8(C.REG_RSV_CTRL)
    transport.write8(C.REG_RSV_CTRL, value & ~(1 << 1))
    value = transport.read8(C.REG_RSV_CTRL + 1)
    transport.write8(C.REG_RSV_CTRL + 1, value & ~1)
    value = transport.read16(C.REG_SYS_FUNC)
    transport.write16(C.REG_SYS_FUNC, value & ~C.SYS_FUNC_CPU_ENABLE)
    value = transport.read8(C.REG_RSV_CTRL)
    transport.write8(C.REG_RSV_CTRL, value & ~(1 << 1))
    value = transport.read8(C.REG_RSV_CTRL + 1)
    transport.write8(C.REG_RSV_CTRL + 1, value | 1)
    value = transport.read16(C.REG_SYS_FUNC)
    transport.write16(C.REG_SYS_FUNC, value | C.SYS_FUNC_CPU_ENABLE)


def _write_page(transport: RTL8192FUTransport, page: int, payload: bytes) -> None:
    value = transport.read8(C.REG_MCU_FW_DL + 2)
    transport.write8(C.REG_MCU_FW_DL + 2, (value & 0xF8) | page)
    for offset in range(0, len(payload), C.FW_BLOCK_SIZE):
        block = payload[offset:offset + C.FW_BLOCK_SIZE]
        transport.write_block(C.REG_FW_START_ADDRESS + offset, block)


def download_firmware(transport: RTL8192FUTransport, firmware: FirmwareImage) -> None:
    value = transport.read8(C.REG_SYS_FUNC + 1)
    transport.write8(C.REG_SYS_FUNC + 1, value | 4)
    value = transport.read16(C.REG_SYS_FUNC)
    transport.write16(C.REG_SYS_FUNC, value | C.SYS_FUNC_CPU_ENABLE)
    value = transport.read8(C.REG_MCU_FW_DL)
    if value & (1 << 7):
        transport.write8(C.REG_MCU_FW_DL, 0)
        reset_8051(transport)
    value = transport.read8(C.REG_MCU_FW_DL)
    transport.write8(C.REG_MCU_FW_DL, value | 1)
    value = transport.read32(C.REG_MCU_FW_DL)
    transport.write32(C.REG_MCU_FW_DL, value & ~(1 << 19))
    value = transport.read8(C.REG_MCU_FW_DL)
    transport.write8(C.REG_MCU_FW_DL, value | (1 << 2))
    try:
        for page, offset in enumerate(range(0, len(firmware.payload), C.FW_PAGE_SIZE)):
            _write_page(transport, page, firmware.payload[offset:offset + C.FW_PAGE_SIZE])
    finally:
        value = transport.read16(C.REG_MCU_FW_DL)
        transport.write16(C.REG_MCU_FW_DL, value & ~1)


def start_firmware(transport: RTL8192FUTransport) -> None:
    for _ in range(1000):
        if transport.read32(C.REG_MCU_FW_DL) & (1 << 2):
            break
    else:
        raise IOError("RTL8192FU firmware checksum timed out")
    value = transport.read32(C.REG_MCU_FW_DL)
    value = (value | (1 << 1)) & ~(1 << 6)
    transport.write32(C.REG_MCU_FW_DL, value)
    reset_8051(transport)
    for _ in range(1000):
        if transport.read32(C.REG_MCU_FW_DL) & (1 << 6):
            break
        time.sleep(0.0001)
    else:
        raise IOError("RTL8192FU firmware did not start")
    transport.write8(C.REG_HMTFR, 0x0F)
