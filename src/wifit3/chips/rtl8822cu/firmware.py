"""RTL8822CU firmware container validation.

The device-specific NIC image is extracted verbatim from Realtek's GPL
``rtl88x2cu`` driver. Downloading it is intentionally a later step: this
module first makes the firmware shape and section boundaries explicit.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from pathlib import Path

import usb.core

from wifit3.chips.rtw88_base.registers import (
    BIT_BCN_VALID_V1,
    BIT_CHECK_SUM_OK,
    BIT_DDMACH0_CHKSUM_CONT,
    BIT_DDMACH0_CHKSUM_EN,
    BIT_DDMACH0_CHKSUM_STS,
    BIT_DDMACH0_OWN,
    BIT_DDMACH0_RESET_CHKSUM_STS,
    BIT_DIS_TSF_UDT,
    BIT_DMEM_CHKSUM_OK,
    BIT_DMEM_DW_OK,
    BIT_EN_BCN_FUNCTION,
    BIT_ENSWBCN,
    BIT_FEN_CPUEN,
    BIT_FW_DW_RDY,
    BIT_HCI_TXDMA_EN,
    BIT_IMEM_CHKSUM_OK,
    BIT_IMEM_DW_OK,
    BIT_MASK_DDMACH0_DLEN,
    BIT_MCUFWDL_EN,
    BIT_TXDMA_EN,
    BIT_WLMCU_IOIF,
    BTI_PAGE_OVF,
    OCPBASE_DMEM_88XX,
    OCPBASE_TXBUF_88XX,
    REG_BCN_CTRL,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_DDMA_CH0CTRL,
    REG_DDMA_CH0DA,
    REG_DDMA_CH0SA,
    REG_FIFOPAGE_CTRL_2,
    REG_FIFOPAGE_INFO_1,
    REG_FW_DBG7,
    REG_H2CQ_CSR,
    REG_MCUFW_CTRL,
    REG_RQPN_CTRL_2,
    REG_RSV_CTRL,
    REG_SYS_CLK_CTRL,
    REG_SYS_FUNC_EN,
    REG_TXDMA_PQ_MAP,
    REG_TXDMA_STATUS,
    RTW_DMA_MAPPING_HIGH,
)

from .transport import RTL8822CUTransport


_HEADER_SIZE = 64
_CHECKSUM_SIZE = 8
_FW_PATH = Path(__file__).resolve().parent / "assets" / "rtl8822c_fw_nic.bin"
_TX_DESC_SIZE = 48
_MAX_CHUNK = 0x1000


@dataclass(frozen=True)
class FirmwareImage:
    version: int
    subversion: int
    h2c_format: int
    dmem_addr: int
    dmem: bytes
    imem_addr: int
    imem: bytes
    emem_addr: int | None
    emem: bytes


def parse_firmware(data: bytes) -> FirmwareImage:
    """Parse the HALMAC 88xx firmware header and validate every section bound."""
    if len(data) < _HEADER_SIZE:
        raise ValueError("RTL8822CU firmware is shorter than its 64-byte header")
    version = struct.unpack_from("<H", data, 4)[0]
    subversion = data[6]
    h2c_format = struct.unpack_from("<I", data, 28)[0]
    dmem_addr, dmem_size = struct.unpack_from("<II", data, 32)
    imem_size = struct.unpack_from("<I", data, 48)[0]
    emem_size = struct.unpack_from("<I", data, 52)[0]
    emem_addr = struct.unpack_from("<I", data, 56)[0]
    imem_addr = struct.unpack_from("<I", data, 60)[0]
    sections = (dmem_size + _CHECKSUM_SIZE, imem_size + _CHECKSUM_SIZE)
    if data[24] & (1 << 4):
        sections += (emem_size + _CHECKSUM_SIZE,)
    elif emem_size:
        raise ValueError("RTL8822CU firmware has EMEM bytes without the EMEM usage flag")
    expected = _HEADER_SIZE + sum(sections)
    if len(data) != expected:
        raise ValueError(f"RTL8822CU firmware size {len(data)} does not match header {expected}")
    pos = _HEADER_SIZE
    dmem = data[pos:pos + sections[0]]
    pos += sections[0]
    imem = data[pos:pos + sections[1]]
    pos += sections[1]
    emem = data[pos:pos + sections[2]] if len(sections) == 3 else b""
    return FirmwareImage(version, subversion, h2c_format, dmem_addr, dmem,
                         imem_addr, imem, emem_addr if emem else None, emem)


def load_firmware() -> FirmwareImage:
    return parse_firmware(_FW_PATH.read_bytes())


def _poll32(transport: RTL8822CUTransport, addr: int, mask: int, value: int,
            attempts: int = 1000, delay: float = 0.0005) -> bool:
    for _ in range(attempts):
        if transport.read32(addr) & mask == value & mask:
            return True
        time.sleep(delay)
    return False


def _poll16(transport: RTL8822CUTransport, addr: int, mask: int, value: int,
            attempts: int = 1000, delay: float = 0.0005) -> bool:
    for _ in range(attempts):
        if transport.read16(addr) & mask == value & mask:
            return True
        time.sleep(delay)
    return False


def _fw_tx_desc(size: int) -> bytes:
    """48-byte RTL8822C TX descriptor for a reserved-page FW packet."""
    desc = bytearray(_TX_DESC_SIZE)
    struct.pack_into("<I", desc, 0, (size & 0xFFFF) | (_TX_DESC_SIZE << 16) | (1 << 26))
    struct.pack_into("<I", desc, 4, 16 << 8)  # QSEL_BEACON
    checksum = 0
    for pos in range(0, 32, 2):
        checksum ^= struct.unpack_from("<H", desc, pos)[0]
    struct.pack_into("<H", desc, 28, checksum)
    return bytes(desc)


def _write_fw_packet(dev: usb.core.Device, transport: RTL8822CUTransport,
                     bulk_out: int, payload: bytes) -> None:
    """Upload one TX-buffer packet, then wait until the reserved page latches."""
    bcn_ctrl = transport.read8(REG_BCN_CTRL)
    cr1 = transport.read8(REG_CR + 1)
    transport.write16(REG_FIFOPAGE_CTRL_2, BIT_BCN_VALID_V1)
    transport.write8(REG_CR + 1, cr1 | ((BIT_ENSWBCN >> 8) & 0xFF))
    transport.write8(REG_BCN_CTRL, (bcn_ctrl & ~BIT_EN_BCN_FUNCTION) | BIT_DIS_TSF_UDT)
    try:
        packet = _fw_tx_desc(len(payload)) + payload
        sent = dev.write(bulk_out, packet, 1000)
        if sent != len(packet):
            raise IOError(f"RTL8822CU firmware bulk write short: {sent}/{len(packet)}")
        if not _poll16(transport, REG_FIFOPAGE_CTRL_2, BIT_BCN_VALID_V1, BIT_BCN_VALID_V1):
            raise IOError("RTL8822CU firmware page did not latch")
    finally:
        transport.write16(REG_FIFOPAGE_CTRL_2, BIT_BCN_VALID_V1)
        transport.write8(REG_BCN_CTRL, bcn_ctrl)
        transport.write8(REG_CR + 1, cr1)


def _iddma(transport: RTL8822CUTransport, src: int, dst: int, length: int, first: bool) -> None:
    if not _poll32(transport, REG_DDMA_CH0CTRL, BIT_DDMACH0_OWN, 0):
        raise IOError("RTL8822CU iDDMA channel remains owned")
    control = BIT_DDMACH0_CHKSUM_EN | BIT_DDMACH0_OWN | (length & BIT_MASK_DDMACH0_DLEN)
    if not first:
        control |= BIT_DDMACH0_CHKSUM_CONT
    transport.write32(REG_DDMA_CH0SA, src)
    transport.write32(REG_DDMA_CH0DA, dst)
    transport.write32(REG_DDMA_CH0CTRL, control)
    if not _poll32(transport, REG_DDMA_CH0CTRL, BIT_DDMACH0_OWN, 0):
        raise IOError("RTL8822CU iDDMA transfer timed out")


def _upload_section(dev: usb.core.Device, transport: RTL8822CUTransport,
                    bulk_out: int, destination: int, data: bytes) -> None:
    transport.write32_set(REG_DDMA_CH0CTRL, BIT_DDMACH0_RESET_CHKSUM_STS)
    for offset in range(0, len(data), _MAX_CHUNK):
        chunk = data[offset:offset + _MAX_CHUNK]
        wire_chunk = chunk + b"\0" if (len(chunk) + _TX_DESC_SIZE) % 512 == 0 else chunk
        _write_fw_packet(dev, transport, bulk_out, wire_chunk)
        _iddma(transport, OCPBASE_TXBUF_88XX + _TX_DESC_SIZE, destination + offset, len(chunk), offset == 0)
    fw_ctrl = transport.read8(REG_MCUFW_CTRL)
    if transport.read32(REG_DDMA_CH0CTRL) & BIT_DDMACH0_CHKSUM_STS:
        raise IOError(f"RTL8822CU firmware checksum failed at 0x{destination:08x}")
    if destination < OCPBASE_DMEM_88XX:
        fw_ctrl |= BIT_IMEM_DW_OK | BIT_IMEM_CHKSUM_OK
    else:
        fw_ctrl |= BIT_DMEM_DW_OK | BIT_DMEM_CHKSUM_OK
    transport.write8(REG_MCUFW_CTRL, fw_ctrl)


def _wlan_cpu_enable(transport: RTL8822CUTransport, enabled: bool) -> None:
    if enabled:
        transport.write8_set(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)
        transport.write8_set(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
    else:
        transport.write8_clr(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
        transport.write8_clr(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)


def _reset_platform(transport: RTL8822CUTransport) -> None:
    transport.write8_clr(REG_CPU_DMEM_CON + 2, 1)
    transport.write8_clr(REG_SYS_CLK_CTRL + 1, 1 << 6)
    transport.write8_set(REG_CPU_DMEM_CON + 2, 1)
    transport.write8_set(REG_SYS_CLK_CTRL + 1, 1 << 6)


def download_firmware(dev: usb.core.Device, transport: RTL8822CUTransport,
                      bulk_out: int, image: FirmwareImage) -> None:
    """HALMAC iDDMA download for the verified RTL8822C NIC firmware image."""
    _wlan_cpu_enable(transport, False)
    saved = [(REG_TXDMA_PQ_MAP + 1, 1, transport.read8(REG_TXDMA_PQ_MAP + 1)),
             (REG_CR, 1, transport.read8(REG_CR)),
             (REG_H2CQ_CSR, 4, 1 << 31),
             (REG_FIFOPAGE_INFO_1, 2, transport.read16(REG_FIFOPAGE_INFO_1)),
             (REG_RQPN_CTRL_2, 4, transport.read32(REG_RQPN_CTRL_2) | (1 << 31)),
             (REG_BCN_CTRL, 1, transport.read8(REG_BCN_CTRL))]
    try:
        transport.write8(REG_TXDMA_PQ_MAP + 1, RTW_DMA_MAPPING_HIGH << 6)
        transport.write8(REG_CR, BIT_HCI_TXDMA_EN | BIT_TXDMA_EN)
        transport.write32(REG_H2CQ_CSR, 1 << 31)
        transport.write16(REG_FIFOPAGE_INFO_1, 0x200)
        transport.write32(REG_RQPN_CTRL_2, saved[4][2])
        transport.write8(REG_BCN_CTRL, (saved[5][2] & ~BIT_EN_BCN_FUNCTION) | BIT_DIS_TSF_UDT)
        _reset_platform(transport)
        transport.write16(REG_MCUFW_CTRL, (transport.read16(REG_MCUFW_CTRL) & 0x3800) | BIT_MCUFWDL_EN)
        _upload_section(dev, transport, bulk_out, image.dmem_addr & 0x7FFFFFFF, image.dmem)
        _upload_section(dev, transport, bulk_out, image.imem_addr & 0x7FFFFFFF, image.imem)
        if image.emem:
            _upload_section(dev, transport, bulk_out, image.emem_addr & 0x7FFFFFFF, image.emem)
    finally:
        for addr, size, value in saved:
            (transport.write8 if size == 1 else transport.write16 if size == 2 else transport.write32)(addr, value)
    fw_ctrl = transport.read16(REG_MCUFW_CTRL)
    if (fw_ctrl & BIT_CHECK_SUM_OK) != BIT_CHECK_SUM_OK:
        raise IOError(f"RTL8822CU FW checksum status invalid: 0x{fw_ctrl:04x}")
    transport.write32(REG_TXDMA_STATUS, BTI_PAGE_OVF)
    transport.write16(REG_MCUFW_CTRL, (fw_ctrl | BIT_FW_DW_RDY) & ~BIT_MCUFWDL_EN)
    _wlan_cpu_enable(transport, True)


def firmware_ready(transport: RTL8822CUTransport) -> int:
    """Wait for the exact 8822C HALMAC ready value (REG_MCUFW_CTRL = C078)."""
    for _ in range(5000):
        value = transport.read16(REG_MCUFW_CTRL)
        if value == 0xC078:
            return value
        time.sleep(0.00005)
    debug = transport.read32(REG_FW_DBG7)
    raise IOError(f"RTL8822CU firmware did not boot (MCUFW=0x{value:04x}, DBG7=0x{debug:08x})")
