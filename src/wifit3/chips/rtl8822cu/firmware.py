"""RTL8822CU firmware container validation.

The device-specific NIC image is extracted verbatim from Realtek's GPL
``rtl88x2cu`` driver. Downloading it is intentionally a later step: this
module first makes the firmware shape and section boundaries explicit.
"""
from __future__ import annotations

import logging
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
    REG_C2HEVT,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_DDMA_CH0CTRL,
    REG_DDMA_CH0DA,
    REG_DDMA_CH0SA,
    REG_FIFOPAGE_CTRL_2,
    REG_FIFOPAGE_INFO_1,
    REG_FWHW_TXQ_CTRL,
    REG_FW_DBG7,
    REG_H2CQ_CSR,
    REG_MCUFW_CTRL,
    REG_RQPN_CTRL_2,
    REG_RSV_CTRL,
    REG_SYS_FUNC_EN,
    REG_TXDMA_PQ_MAP,
    REG_TXDMA_STATUS,
    RTW_DMA_MAPPING_HIGH,
)

from .constants import (
    BB_PATH_A,
    C2H_DBG,
    C2H_MAC_HIDDEN_RPT,
    HALMAC_RF_1T1R,
    HALMAC_RF_1T2R,
    HALMAC_RF_2T2R,
    REG_LTECOEX_CTRL,
    REG_LTECOEX_RDATA,
    REG_LTECOEX_WDATA,
    REG_TXPKT_EMPTY,
)
from .transport import RTL8822CUTransport

logger = logging.getLogger(__name__)

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


def _poll8(transport: RTL8822CUTransport, addr: int, mask: int, value: int,
           attempts: int = 1000, delay: float = 0.0005) -> bool:
    for _ in range(attempts):
        if transport.read8(addr) & mask == value & mask:
            return True
        time.sleep(delay)
    return False


def _txfifo_wait_empty(transport: RTL8822CUTransport, chk_num: int = 10) -> None:
    """``txfifo_is_empty_88xx``: poll REG_TXPKT_EMPTY chk_num times before the download.
    [SRC halmac_common_88xx.c:3337-3357]"""
    for _ in range(chk_num):
        if transport.read8(REG_TXPKT_EMPTY) != 0xFF:
            raise IOError("RTL8822CU TX FIFO not empty before FW download")
        if (transport.read8(REG_TXPKT_EMPTY + 1) & 0x06) != 0x06:
            raise IOError("RTL8822CU TX FIFO not empty before FW download")


def _ltecoex_ready(transport: RTL8822CUTransport) -> None:
    for _ in range(1000):
        if transport.read8(REG_LTECOEX_CTRL + 3) & (1 << 5):
            return
        time.sleep(0.0005)
    raise IOError("RTL8822CU LTE-coex indirect access not ready")


def _ltecoex_read(transport: RTL8822CUTransport, offset: int) -> int:
    """``ltecoex_reg_read_88xx``. [SRC halmac_common_88xx.c:3404-3421]"""
    _ltecoex_ready(transport)
    transport.write32(REG_LTECOEX_CTRL, 0x800F0000 | (offset & 0xFFFF))
    return transport.read32(REG_LTECOEX_RDATA)


def _ltecoex_write(transport: RTL8822CUTransport, offset: int, value: int) -> None:
    """``ltecoex_reg_write_88xx``. [SRC halmac_common_88xx.c:3435-3451]"""
    _ltecoex_ready(transport)
    transport.write32(REG_LTECOEX_WDATA, value)
    transport.write32(REG_LTECOEX_CTRL, 0xC00F0000 | (offset & 0xFFFF))


def _fw_tx_desc(size: int, *, qsel: int = 16, offset: int = _TX_DESC_SIZE,
                beacon: bool = False, bmc: bool = False) -> bytes:
    """48 byte RTL8822C TX descriptor + checksum. The checksum spans ``(PKT_OFFSET + 6) * 2``
    u16 words [SRC fill_txdesc_check_sum_8822c halmac_common_8822c.c:184]: 24 words (the whole
    descriptor) for offset=48, so the beacon desc's word8 is covered. Reserved page FW packet:
    qsel=16 (BEACON), offset=48. FW offload H2C packet: qsel=0x13, offset=0. The 2nd download
    (``beacon=True``) uses the full dump_mgntframe BEACON descriptor."""
    desc = bytearray(_TX_DESC_SIZE)
    if beacon:
        w0 = (size & 0xFFFF) | (offset << 16) | (0x84 << 24)
        if bmc:
            w0 |= 1 << 24
        struct.pack_into("<I", desc, 0x00, w0)
        struct.pack_into("<I", desc, 0x04, 0x00081001)   # MACID=1, QSEL=BEACON(0x10), RATE_ID=8
        if bmc:
            struct.pack_into("<I", desc, 0x08, 0x3F000000)   # broadcast MACID
        struct.pack_into("<I", desc, 0x0C, 0x00000100)   # HWSEQ_EN
        struct.pack_into("<I", desc, 0x10, 0x001A0000)   # data-rate default
        struct.pack_into("<I", desc, 0x18, 0x00000001)
        struct.pack_into("<I", desc, 0x20, 0x00008000)
    else:
        struct.pack_into("<I", desc, 0, (size & 0xFFFF) | (offset << 16))
        struct.pack_into("<I", desc, 4, qsel << 8)
    n_words = ((offset >> 3) + (_TX_DESC_SIZE >> 3)) << 1
    checksum = 0
    for i in range(n_words):
        checksum ^= struct.unpack_from("<H", desc, 2 * i)[0]
    struct.pack_into("<H", desc, 28, checksum)
    return bytes(desc)


def _write_fw_packet(dev: usb.core.Device, transport: RTL8822CUTransport,
                     bulk_out: int, payload: bytes, *, beacon: bool = False,
                     rsvd_boundary: int = 0) -> None:
    """``dl_rsvd_page_88xx``: stage one FW chunk into the reserved page (pg_addr 0), wait for the
    BCN_VALID latch, then restore FIFOPAGE_CTRL_2 to rsvd_boundary. [SRC halmac_common_88xx.c:317-380]"""
    transport.write16(REG_FIFOPAGE_CTRL_2, BIT_BCN_VALID_V1)          # pg_addr 0 | BIT15
    cr1 = transport.read8(REG_CR + 1)
    transport.write8(REG_CR + 1, cr1 | ((BIT_ENSWBCN >> 8) & 0xFF))   # ENSWBCN
    txq2 = transport.read8(REG_FWHW_TXQ_CTRL + 2)
    transport.write8(REG_FWHW_TXQ_CTRL + 2, txq2 & ~(1 << 6))
    try:
        # BMC = multicast bit of the chunk's "addr1" (byte 4, past FC[2]+duration[2]).
        bmc = bool(payload[4] & 1) if beacon else False
        packet = _fw_tx_desc(len(payload), beacon=beacon, bmc=bmc) + payload
        sent = dev.write(bulk_out, packet, 1000)
        if sent != len(packet):
            raise IOError(f"RTL8822CU firmware bulk write short: {sent}/{len(packet)}")
        if not _poll8(transport, REG_FIFOPAGE_CTRL_2 + 1, 1 << 7, 1 << 7):
            raise IOError("RTL8822CU firmware page did not latch")
    finally:
        transport.write16(REG_FIFOPAGE_CTRL_2, (rsvd_boundary & 0xFFF) | BIT_BCN_VALID_V1)
        transport.write8(REG_FWHW_TXQ_CTRL + 2, txq2)
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
                    bulk_out: int, destination: int, data: bytes, *, beacon: bool = False,
                    rsvd_boundary: int = 0) -> None:
    transport.write32_set(REG_DDMA_CH0CTRL, BIT_DDMACH0_RESET_CHKSUM_STS)
    for offset in range(0, len(data), _MAX_CHUNK):
        chunk = data[offset:offset + _MAX_CHUNK]
        wire_chunk = chunk + b"\0" if (len(chunk) + _TX_DESC_SIZE) % 512 == 0 else chunk
        _write_fw_packet(dev, transport, bulk_out, wire_chunk, beacon=beacon, rsvd_boundary=rsvd_boundary)
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
    """``pltfm_reset_88xx`` 8822C branch: only the CPU_DMEM_CON reset. The SYS_CLK_CTRL
    BIT6 toggle is 8821C/8822B-only and must not run here. [SRC halmac_fw_88xx.c:390-401]"""
    transport.write8_clr(REG_CPU_DMEM_CON + 2, 1)
    transport.write8_set(REG_CPU_DMEM_CON + 2, 1)


def download_firmware(dev: usb.core.Device, transport: RTL8822CUTransport,
                      bulk_out: int, image: FirmwareImage, *, beacon: bool = False,
                      rsvd_boundary: int = 0) -> None:
    """``download_firmware_88xx``: wait for the TX FIFO to drain, then the HALMAC iDDMA
    NIC-firmware download bracketed by an LTE-coex 0x38 backup/restore. The cycle-1 C2H
    request marker (``C2HEVT=0xFD``) is written by the caller, not here. [SRC halmac_fw_88xx.c:114-206]"""
    _txfifo_wait_empty(transport)
    lte_backup = _ltecoex_read(transport, 0x38)
    _wlan_cpu_enable(transport, False)
    # Backup + config the 6 DMA/queue registers, reads interleaved with writes exactly as
    # the vendor emits them. [SRC halmac_fw_88xx.c:147-187]
    pq1 = transport.read8(REG_TXDMA_PQ_MAP + 1)
    transport.write8(REG_TXDMA_PQ_MAP + 1, RTW_DMA_MAPPING_HIGH << 6)
    cr = transport.read8(REG_CR)
    transport.write8(REG_CR, BIT_HCI_TXDMA_EN | BIT_TXDMA_EN)
    transport.write32(REG_H2CQ_CSR, 1 << 31)
    info1 = transport.read16(REG_FIFOPAGE_INFO_1)
    rqpn2 = transport.read32(REG_RQPN_CTRL_2) | (1 << 31)
    transport.write16(REG_FIFOPAGE_INFO_1, 0x200)
    transport.write32(REG_RQPN_CTRL_2, rqpn2)
    bcn = transport.read8(REG_BCN_CTRL)
    transport.write8(REG_BCN_CTRL, (bcn & ~BIT_EN_BCN_FUNCTION) | BIT_DIS_TSF_UDT)
    try:
        _reset_platform(transport)
        transport.write16(REG_MCUFW_CTRL, (transport.read16(REG_MCUFW_CTRL) & 0x3800) | BIT_MCUFWDL_EN)
        _upload_section(dev, transport, bulk_out, image.dmem_addr & 0x7FFFFFFF, image.dmem,
                        beacon=beacon, rsvd_boundary=rsvd_boundary)
        _upload_section(dev, transport, bulk_out, image.imem_addr & 0x7FFFFFFF, image.imem,
                        beacon=beacon, rsvd_boundary=rsvd_boundary)
        if image.emem:
            _upload_section(dev, transport, bulk_out, image.emem_addr & 0x7FFFFFFF, image.emem,
                            beacon=beacon, rsvd_boundary=rsvd_boundary)
    finally:  # restore_mac_reg_88xx, in backup order [SRC halmac_fw_88xx.c:620]
        transport.write8(REG_TXDMA_PQ_MAP + 1, pq1)
        transport.write8(REG_CR, cr)
        transport.write32(REG_H2CQ_CSR, 1 << 31)
        transport.write16(REG_FIFOPAGE_INFO_1, info1)
        transport.write32(REG_RQPN_CTRL_2, rqpn2)
        transport.write8(REG_BCN_CTRL, bcn)
    # dlfw_end_flow_88xx: TXDMA_STATUS first, then the checksum-status check. [SRC halmac_fw_88xx.c:647]
    transport.write32(REG_TXDMA_STATUS, BTI_PAGE_OVF)
    fw_ctrl = transport.read16(REG_MCUFW_CTRL)
    if (fw_ctrl & BIT_CHECK_SUM_OK) != BIT_CHECK_SUM_OK:
        raise IOError(f"RTL8822CU FW checksum status invalid: 0x{fw_ctrl:04x}")
    transport.write16(REG_MCUFW_CTRL, (fw_ctrl | BIT_FW_DW_RDY) & ~BIT_MCUFWDL_EN)
    _wlan_cpu_enable(transport, True)
    firmware_ready(transport)
    _ltecoex_write(transport, 0x38, lte_backup)


def firmware_ready(transport: RTL8822CUTransport) -> int:
    """Wait for the exact 8822C HALMAC ready value (REG_MCUFW_CTRL = C078)."""
    for _ in range(5000):
        value = transport.read16(REG_MCUFW_CTRL)
        if value == 0xC078:
            return value
        time.sleep(0.00005)
    debug = transport.read32(REG_FW_DBG7)
    raise IOError(f"RTL8822CU firmware did not boot (MCUFW=0x{value:04x}, DBG7=0x{debug:08x})")


# --- FW-offload H2C: general-info + PHYDM-info, then the MAC-hidden report handshake ---
_H2C_PKT_SIZE = 32
_H2C_CATEGORY = 0x01                 # FW_OFFLOAD_H2C_SET_CATEGORY
_H2C_CMD_ID = 0xFF                   # FW_OFFLOAD_H2C_SET_CMD_ID
_SUB_CMD_GENERAL_INFO = 0x0D
_SUB_CMD_PHYDM_INFO = 0x11
_GENINFO_REG_CMD_ID = 0x0C
_GENINFO_REG_CLASS = 0x02

# Register H2C message boxes [SRC hal_halmac.h:27-37, halmac_reg2.h:1156,1204]
RTW_HALMAC_H2C_MAX_SIZE = 8
MAX_H2C_BOX_NUMS = 4
MESSAGE_BOX_SIZE = 4
EX_MESSAGE_BOX_SIZE = 4
REG_HMETFR = 0x01CC
REG_HMEBOX0 = 0x01D0
REG_HMEBOX_E0 = 0x01F0


def _set_le32_bits(buf: bytearray, offset: int, lsb: int, width: int, value: int) -> None:
    v = int.from_bytes(buf[offset:offset + 4], "little")
    mask = ((1 << width) - 1) << lsb
    buf[offset:offset + 4] = (((v & ~mask) | ((value << lsb) & mask)) & 0xFFFFFFFF).to_bytes(4, "little")


def _h2c_pkt(sub_cmd: int, content_size: int, seq: int) -> bytearray:
    """set_h2c_pkt_hdr_88xx: the 8-byte FW-offload H2C header. [SRC halmac_common_88xx.c:614]"""
    pkt = bytearray(_H2C_PKT_SIZE)
    _set_le32_bits(pkt, 0x00, 0, 7, _H2C_CATEGORY)
    _set_le32_bits(pkt, 0x00, 8, 8, _H2C_CMD_ID)
    _set_le32_bits(pkt, 0x00, 16, 16, sub_cmd)
    _set_le32_bits(pkt, 0x04, 0, 16, 8 + content_size)
    _set_le32_bits(pkt, 0x04, 16, 16, seq)
    return pkt


def _dump_h2cq(transport: RTL8822CUTransport) -> None:
    """send_general_info_88xx H2C queue poll: read the reserved H2C queue's first word back
    through the packet buffer debug window until the FW general info header appears
    (byte0 & 0x7F == 0x01 and byte1 == 0xFF), else fail after 100 polls with 5us between them.
    Each poll inlines dump_fifo_88xx -> read_buf_88xx for HAL_FIFO_SEL_TX, bracketed by the
    rx_clk_gate_88xx BIT(3) toggle on REG_RCR+2. The queue address derives from the reserved
    page allocation (rsvd_h2cq_addr << TX_PAGE_SIZE_SHIFT_88XX), not a frozen literal.
    [SRC halmac_fw_88xx.c:1083-1104, halmac_common_88xx.c:2016-2120, halmac_cfg_wmac_88xx.c:1102-1114]"""
    from .mac import _compute_trx_alloc
    h2cq_addr = _compute_trx_alloc().h2cq_addr
    start_pg = (h2cq_addr >> 12) + 0x780            # read_buf_88xx HAL_FIFO_SEL_TX page base
    read_addr = 0x8000 + (h2cq_addr & 0xFFF)        # read_buf_88xx window offset for the residue
    for _ in range(100):
        rcr2 = transport.read8(0x060A)                 # REG_RCR+2 save (dump_fifo_88xx)
        transport.write8_set(0x060A, 1 << 3)           # rx_clk_gate_88xx(enable=0): REG_RCR+2 |= BIT(3)
        keep = transport.read16(0x0140) & 0xF000       # REG_PKTBUF_DBG_CTRL value32
        transport.write16(0x0140, start_pg | keep)
        ele = transport.read32(read_addr)
        transport.write16(0x0140, keep)
        transport.write8(0x060A, rcr2)                 # restore REG_RCR+2
        if (ele & 0x7F) == _H2C_CATEGORY and ((ele >> 8) & 0xFF) == _H2C_CMD_ID:
            return
        time.sleep(0.000005)                           # PLTFM_DELAY_US(5) between polls
    raise IOError("RTL8822CU H2C queue readback never matched the general info header (100 polls)")


@dataclass
class H2cState:
    """hal->LastHMEBoxNum: which HMEBOX the next register H2C goes to. Reset by every
    firmware download. [SRC hal_halmac.c:3465]"""
    box_num: int = 0


def fill_h2c_cmd(transport: RTL8822CUTransport, h2c_state: H2cState, element_id: int,
                 payload: bytes) -> None:
    """rtl8822c_fillh2ccmd -> rtw_halmac_send_h2c: element id in byte 0 and the payload after
    it, sent as bytes 4-7 into the extension box then bytes 0-3 into the message box, which is
    what tells the firmware the command is complete. [SRC rtl8822c_cmd.c:32, hal_halmac.c:4157]"""
    if len(payload) > RTW_HALMAC_H2C_MAX_SIZE - 1:
        raise ValueError(f"RTL8822CU H2C 0x{element_id:02x}: payload too long ({len(payload)})")
    h2c = bytearray(RTW_HALMAC_H2C_MAX_SIZE)
    h2c[0] = element_id
    h2c[1:1 + len(payload)] = payload
    box = h2c_state.box_num
    for _ in range(100):                # _is_fw_read_cmd_down: the FW has drained this box
        if not transport.read8(REG_HMETFR) & (1 << box):
            break
        time.sleep(0.001)
    transport.write32(REG_HMEBOX_E0 + box * EX_MESSAGE_BOX_SIZE,
                      int.from_bytes(h2c[4:8], "little"))
    transport.write32(REG_HMEBOX0 + box * MESSAGE_BOX_SIZE,
                      int.from_bytes(h2c[0:4], "little"))
    h2c_state.box_num = (box + 1) % MAX_H2C_BOX_NUMS


def _send_general_info_by_reg(transport: RTL8822CUTransport, h2c_state: H2cState, rfe_type: int,
                              chip_ver: int, rf_type_drv: int, tx_ant: int, rx_ant: int) -> None:
    """A companion register H2C (rfe / rf / cut / antenna) alongside the FW-offload packets.
    [SRC hal_halmac.c]"""
    element_id = _GENINFO_REG_CMD_ID | (_GENINFO_REG_CLASS << 5)
    fill_h2c_cmd(transport, h2c_state, element_id,
                 bytes([rfe_type & 0xFF, rf_type_drv & 0xFF, chip_ver & 0xFF,
                        (rx_ant & 0xF) | ((tx_ant & 0xF) << 4)]))


_RF_TYPE_HALMAC2DRV = {          # halmac RF enum -> driver enum rf_type [SRC hal_halmac.c:3035-3073]
    HALMAC_RF_1T1R: 0x00,        # RF_1T1R [SRC include/cmn_info/rtw_sta_info.h:80]
    HALMAC_RF_1T2R: 0x01,        # RF_1T2R [SRC include/cmn_info/rtw_sta_info.h:81]
    HALMAC_RF_2T2R: 0x02,        # RF_2T2R [SRC include/cmn_info/rtw_sta_info.h:82]
}
RF_TYPE_MAX = 0x10               # the C's default arm [SRC hal_halmac.c:3066-3069, rtw_sta_info.h:96]


def send_general_info(dev: usb.core.Device, transport: RTL8822CUTransport, bulk_out: int,
                      h2c_state: H2cState, rfe_type: int, chip_ver: int, *,
                      rf_type: int = HALMAC_RF_1T1R, tx_ant: int = BB_PATH_A,
                      rx_ant: int = BB_PATH_A, package: int = 0x00) -> None:
    """_send_general_info: give the FW its general info (fw_tx_boundary) and phydm info
    (rfe/rf/cut/antenna/package) via two FW offload H2C bulk packets, confirm via the H2C queue
    readback, then the reg H2C companion.

    The C reads rf_type / tx_ant / rx_ant at call time from rtw_hal_get_trx_path (hal->rf_type /
    tx_path / rx_path, rf via _rf_type_drv2halmac) and package from hal->PackageType, so they change
    between the two cold cycles; the caller passes what it has at each. The reg companion's driver
    rf_type is derived here via _rf_type_halmac2drv [SRC hal_halmac.c:3104].
    [SRC _send_general_info hal_halmac.c:3129-3180 ; proc_send_general_info_88xx /
    proc_send_phydm_info_88xx halmac_fw_88xx.c:1114-1173]"""
    from .mac import _compute_trx_alloc
    alloc = _compute_trx_alloc()
    fw_tx_boundary = alloc.rsvd_fw_txbuf_addr - alloc.rsvd_boundary  # GENERAL_INFO_SET_FW_TX_BOUNDARY
    gen = _h2c_pkt(_SUB_CMD_GENERAL_INFO, content_size=4, seq=0)
    _set_le32_bits(gen, 0x08, 16, 8, fw_tx_boundary)
    dev.write(bulk_out, _fw_tx_desc(_H2C_PKT_SIZE, qsel=0x13, offset=0) + bytes(gen), 1000)

    phy = _h2c_pkt(_SUB_CMD_PHYDM_INFO, content_size=8, seq=1)
    _set_le32_bits(phy, 0x08, 0, 8, rfe_type)
    _set_le32_bits(phy, 0x08, 8, 8, rf_type)
    _set_le32_bits(phy, 0x08, 16, 8, chip_ver)
    _set_le32_bits(phy, 0x08, 24, 4, rx_ant)
    _set_le32_bits(phy, 0x08, 28, 4, tx_ant)
    _set_le32_bits(phy, 0x0C, 8, 8, package)
    dev.write(bulk_out, _fw_tx_desc(_H2C_PKT_SIZE, qsel=0x13, offset=0) + bytes(phy), 1000)

    _dump_h2cq(transport)
    rf_type_drv = _RF_TYPE_HALMAC2DRV.get(rf_type)   # _rf_type_halmac2drv [SRC hal_halmac.c:3104]
    if rf_type_drv is None:
        logger.error("RTL8822CU halmac rf_type 0x%02x maps to no driver rf_type; sending the "
                     "vendor's invalid marker 0x%02x", rf_type, RF_TYPE_MAX)
        rf_type_drv = RF_TYPE_MAX
    _send_general_info_by_reg(transport, h2c_state, rfe_type, chip_ver, rf_type_drv, tx_ant, rx_ant)


@dataclass(frozen=True)
class MacHiddenRpt:
    """The c2h_mac_hidden_rpt_hdl fields this port consumes: PackageType for send_general_info, and
    the two that downgrade the RF path spec. The other MAC caps are read to match the wire only.
    [SRC hal/hal_com.c:1424-1480]"""
    package_type: int = 0
    hw_stype: int = 0
    ant_num: int = 0


def read_mac_hidden_rpt(transport: RTL8822CUTransport) -> MacHiddenRpt:
    """hal_read_mac_hidden_rpt tail: poll REG_C2HEVT_MSG_NORMAL until the FW posts the MAC-hidden
    report, read its 13 bytes, then acknowledge.
    [SRC hal_read_mac_hidden_rpt hal_com.c:1618-1633]"""
    rpt = MacHiddenRpt()
    for _ in range(5000):
        if transport.read8(REG_C2HEVT) == C2H_MAC_HIDDEN_RPT:
            data = [transport.read8(REG_C2HEVT + 2 + i) for i in range(13)]
            # GET_C2H_MAC_HIDDEN_RPT_{PACKAGE_TYPE,HW_STYPE,ANT_NUM} [SRC hal_com.c:1378,1381,1383]
            rpt = MacHiddenRpt(package_type=(data[4] >> 4) & 0x7,
                               hw_stype=(data[5] >> 4) & 0xF,
                               ant_num=(data[6] >> 5) & 0x7)
            break
        time.sleep(0.0001)
    transport.write8(REG_C2HEVT, C2H_DBG)            # report has been read
    return rpt
