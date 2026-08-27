"""RTL8814AU firmware upload (modern WCPU_3081 / iDDMA path).

Port of `start_download_firmware` + `download_firmware_to_mem` (mac.c:646..744)
and `__rtw_download_firmware` (mac.c:776) for the 8814a on USB. The 8814a shares
this path with the 8822b; the differences are all data, not control flow:

  - bulk-OUT FW endpoint is **0x02** (8822b uses 0x05)
  - the TX descriptor is **40 bytes** (`tx_pkt_desc_sz`); 8822b is 48
  - the ZLP-avoidance %512 check uses the kernel's hardcoded 48 regardless
    (FW_DLFW_ZLP_TXDESC), see send_firmware_pkt / _section_upload
  - section sizes/addresses are read from the FW header (no EMEM for 8814a)

Layering:

  download_firmware
    │  parse FW header → (DMEM, IMEM[, EMEM]) addr+size
    ├── _section_upload  (per section)
    │    └── loop 4096-byte chunks:
    │         send_firmware_pkt        (40-B tx_pkt_desc + chunk, bulk-OUT 0x02)
    │         iddma_download_firmware  (3 reg writes + poll)
    │    check_fw_checksum (per section)
    └── reg restore + download_firmware_end_flow + wlan_cpu_enable(true)
"""

from __future__ import annotations

import logging
import struct
import time
from pathlib import Path
from typing import Callable

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
    BIT_MASK_BCN_HEAD_1_V1,
    BIT_MASK_DDMACH0_DLEN,
    BIT_MCUFWDL_EN,
    BIT_TXDMA_EN,
    BIT_WL_PLATFORM_RST,
    BIT_WLMCU_IOIF,
    BTI_PAGE_OVF,
    FW_KEY_MASK,
    FW_READY,
    FW_READY_MASK,
    ILLEGAL_KEY_GROUP,
    OCPBASE_TXBUF_88XX,
    REG_BCN_CTRL,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_DDMA_CH0CTRL,
    REG_DDMA_CH0DA,
    REG_DDMA_CH0SA,
    REG_FIFOPAGE_CTRL_2,
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

from .constants import (
    DLFW_MAX_CHUNK_SIZE,
    EP_FW_BULK_OUT,
    FW_DLFW_ZLP_TXDESC,
    FW_HDR_CHKSUM_SIZE,
    FW_HDR_SIZE,
    OCPBASE_DMEM_88XX,
    TX_DESC_QSEL_BEACON,
    TX_PKT_DESC_SZ,
)
from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)

# The full linux-firmware blob, WITH its 64-byte rtw_fw_hdr. We parse the
# header at upload time (mirroring start_download_firmware) and strip it before
# pushing section bodies on the wire — the 64-byte header never goes to the chip.
FW_BLOB_PATH = (
    Path(__file__).resolve().parent / "assets" / "rtw8814a_fw-linux_firmware.bin"
)
FW_SIGNATURE = 0x8814


def load_firmware_blob() -> bytes:
    """Return the full linux-firmware FW image (header + DMEM + IMEM)."""
    if not FW_BLOB_PATH.exists():
        raise FileNotFoundError(f"FW blob not found: {FW_BLOB_PATH}")
    data = FW_BLOB_PATH.read_bytes()
    if len(data) < FW_HDR_SIZE:
        raise ValueError(f"FW blob too small: {len(data)} bytes")
    sig = struct.unpack_from("<H", data, 0)[0]
    if sig != FW_SIGNATURE:
        raise ValueError(
            f"FW signature 0x{sig:04X} != expected 0x{FW_SIGNATURE:04X}"
        )
    return data


def parse_fw_header(blob: bytes) -> list[tuple[str, int, int, int]]:
    """Parse rtw_fw_hdr → ordered list of (name, file_offset, dst_addr, size).

    Mirrors start_download_firmware (mac.c:697..744): each section size has the
    8-byte checksum added; addresses are masked with ~BIT(31); EMEM is present
    only if mem_usage bit 4 is set.
    """
    mem_usage = blob[0x18]
    dmem_addr, dmem_size = struct.unpack_from("<II", blob, 0x20)
    imem_size, emem_size, emem_addr, imem_addr = struct.unpack_from("<IIII", blob, 0x30)

    dmem_size += FW_HDR_CHKSUM_SIZE
    imem_size += FW_HDR_CHKSUM_SIZE
    emem_size = (emem_size + FW_HDR_CHKSUM_SIZE) if (mem_usage & (1 << 4)) else 0

    mask = ~(1 << 31) & 0xFFFFFFFF
    sections = [
        ("DMEM", FW_HDR_SIZE, dmem_addr & mask, dmem_size),
        ("IMEM", FW_HDR_SIZE + dmem_size, imem_addr & mask, imem_size),
    ]
    if emem_size:
        sections.append(
            ("EMEM", FW_HDR_SIZE + dmem_size + imem_size, emem_addr & mask, emem_size)
        )

    expected = FW_HDR_SIZE + dmem_size + imem_size + emem_size
    if expected != len(blob):
        raise ValueError(
            f"FW size mismatch: header implies {expected}, file is {len(blob)}"
        )
    return sections


# ---------------------------------------------------------------------------
# wlan_cpu_enable, reset_platform — mac.c:440..454, 513..519
# ---------------------------------------------------------------------------

def wlan_cpu_enable(transport: RTL8814AUTransport, enable: bool) -> None:
    """mac.c:440..454."""
    if enable:
        transport.write8_set(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)
        transport.write8_set(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
    else:
        transport.write8_clr(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
        transport.write8_clr(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)


def reset_platform(transport: RTL8814AUTransport) -> None:
    """mac.c:513..519 — toggle WL_PLATFORM_RST and CPU_CLK_EN."""
    # BIT_WL_PLATFORM_RST = BIT(16) of REG_CPU_DMEM_CON → high byte bit 0
    HIGH_BIT0 = (BIT_WL_PLATFORM_RST >> 16) & 0xFF
    # BIT_CPU_CLK_EN = BIT(14) of REG_SYS_CLK_CTRL → bit 6 of high byte
    CPU_CLK_BIT = 1 << 6

    transport.write8_clr(REG_CPU_DMEM_CON + 2, HIGH_BIT0)
    transport.write8_clr(REG_SYS_CLK_CTRL + 1, CPU_CLK_BIT)
    transport.write8_set(REG_CPU_DMEM_CON + 2, HIGH_BIT0)
    transport.write8_set(REG_SYS_CLK_CTRL + 1, CPU_CLK_BIT)


# ---------------------------------------------------------------------------
# tx_pkt_desc for FW upload (qsel=BEACON, offset=desc_sz, ls=1, size=chunk)
# ---------------------------------------------------------------------------

def build_fw_tx_pkt_desc(chunk_size: int) -> bytes:
    """40-byte TX descriptor for a FW upload chunk (rtw8814a_fill_txdesc_checksum
    → fill_txdesc_checksum_common, 16 words).

      W0[15:0]   TXPKTSIZE = chunk_size
      W0[23:16]  OFFSET    = TX_PKT_DESC_SZ (40)
      W0[26]     LS        = 1
      W1[12:8]   QSEL      = TX_DESC_QSEL_BEACON
      W7[15:0]   CHECKSUM  = XOR of first 16 u16s (with the checksum field zeroed)
    """
    desc = bytearray(TX_PKT_DESC_SZ)

    w0 = (
        (chunk_size & 0xFFFF)
        | ((TX_PKT_DESC_SZ & 0xFF) << 16)   # OFFSET = 40
        | (1 << 26)                         # LS
    )
    w1 = (TX_DESC_QSEL_BEACON & 0x1F) << 8

    struct.pack_into("<I", desc, 0, w0)
    struct.pack_into("<I", desc, 4, w1)

    chksum = 0
    for i in range(16):
        chksum ^= struct.unpack_from("<H", desc, i * 2)[0]
    struct.pack_into("<H", desc, 7 * 4, chksum & 0xFFFF)  # W7 low 16 bits

    return bytes(desc)


# ---------------------------------------------------------------------------
# polling helpers
# ---------------------------------------------------------------------------

def _poll32_mask(transport: RTL8814AUTransport, addr: int, mask: int,
                 target: int, attempts: int = 100,
                 interval_s: float = 0.001) -> bool:
    desired = target & mask
    for _ in range(attempts):
        if (transport.read32(addr) & mask) == desired:
            return True
        time.sleep(interval_s)
    return False


def _poll16_mask(transport: RTL8814AUTransport, addr: int, mask: int,
                 target: int, attempts: int = 100,
                 interval_s: float = 0.001) -> bool:
    desired = target & mask
    for _ in range(attempts):
        if (transport.read16(addr) & mask) == desired:
            return True
        time.sleep(interval_s)
    return False


# ---------------------------------------------------------------------------
# send_firmware_pkt — bulk-OUT of tx_desc + chunk, gated by BCN_VALID_V1
# ---------------------------------------------------------------------------

def send_firmware_pkt(
    dev: usb.core.Device,
    transport: RTL8814AUTransport,
    pg_addr: int,
    data: bytes,
    size: int,
    *,
    rsvd_pg_head: int,
) -> None:
    """fw.c rtw_fw_write_data_rsvd_page (non-8051 path).

    Builds [40-B tx_desc + data[:size]] and writes it to bulk-OUT 0x02. The
    BCN_VALID_V1 bit signals "TX buffered into the rsvd page" — polled before
    continuing. `size` is the FW chunk size with the ZLP +1 already applied by
    the caller (and `data` already carries that trailing byte).
    """
    bckp_bcn_ctrl = transport.read8(REG_BCN_CTRL)

    pg_low = pg_addr & BIT_MASK_BCN_HEAD_1_V1
    transport.write16(REG_FIFOPAGE_CTRL_2, pg_low | BIT_BCN_VALID_V1)

    bckp_cr1 = transport.read8(REG_CR + 1)
    transport.write8(REG_CR + 1, bckp_cr1 | ((BIT_ENSWBCN >> 8) & 0xFF))

    transport.write8(
        REG_BCN_CTRL,
        (bckp_bcn_ctrl & ~BIT_EN_BCN_FUNCTION & 0xFF) | BIT_DIS_TSF_UDT,
    )

    pkt = build_fw_tx_pkt_desc(size) + data[:size]
    written = dev.write(EP_FW_BULK_OUT, pkt, 500)
    if written != len(pkt):
        raise IOError(f"FW bulk-OUT short: wrote {written}, expected {len(pkt)}")

    if not _poll16_mask(transport, REG_FIFOPAGE_CTRL_2,
                        BIT_BCN_VALID_V1, BIT_BCN_VALID_V1,
                        attempts=200, interval_s=0.0005):
        raise IOError("bcn_valid never set — FW chunk wasn't latched")

    transport.write16(REG_FIFOPAGE_CTRL_2,
                      (rsvd_pg_head & BIT_MASK_BCN_HEAD_1_V1) | BIT_BCN_VALID_V1)
    transport.write8(REG_BCN_CTRL, bckp_bcn_ctrl)
    transport.write8(REG_CR + 1, bckp_cr1)


# ---------------------------------------------------------------------------
# iddma_download_firmware — three control writes + poll on BIT_DDMACH0_OWN
# ---------------------------------------------------------------------------

def iddma_download_firmware(
    transport: RTL8814AUTransport,
    src: int,
    dst: int,
    length: int,
    first: bool,
) -> None:
    """mac.c:574..590."""
    if not _poll32_mask(transport, REG_DDMA_CH0CTRL, BIT_DDMACH0_OWN, 0):
        raise IOError("iddma_download_firmware: OWN didn't clear before xfer")

    ch0_ctrl = BIT_DDMACH0_CHKSUM_EN | BIT_DDMACH0_OWN
    ch0_ctrl |= length & BIT_MASK_DDMACH0_DLEN
    if not first:
        ch0_ctrl |= BIT_DDMACH0_CHKSUM_CONT

    transport.write32(REG_DDMA_CH0SA, src & 0xFFFFFFFF)
    transport.write32(REG_DDMA_CH0DA, dst & 0xFFFFFFFF)
    transport.write32(REG_DDMA_CH0CTRL, ch0_ctrl & 0xFFFFFFFF)

    if not _poll32_mask(transport, REG_DDMA_CH0CTRL, BIT_DDMACH0_OWN, 0):
        raise IOError("iddma_download_firmware: transfer never completed (OWN stuck)")


def check_fw_checksum(transport: RTL8814AUTransport, dst: int) -> bool:
    """mac.c:611..643."""
    ddma_ctrl = transport.read32(REG_DDMA_CH0CTRL)
    fw_ctrl = transport.read8(REG_MCUFW_CTRL)

    if ddma_ctrl & BIT_DDMACH0_CHKSUM_STS:
        if dst < OCPBASE_DMEM_88XX:
            fw_ctrl |= (BIT_IMEM_DW_OK & 0xFF)
            fw_ctrl &= ~(BIT_IMEM_CHKSUM_OK & 0xFF) & 0xFF
        else:
            fw_ctrl |= (BIT_DMEM_DW_OK & 0xFF)
            fw_ctrl &= ~(BIT_DMEM_CHKSUM_OK & 0xFF) & 0xFF
        transport.write8(REG_MCUFW_CTRL, fw_ctrl & 0xFF)
        logger.error("FW chksum mismatch (dst=0x%x)", dst)
        return False

    if dst < OCPBASE_DMEM_88XX:
        fw_ctrl |= ((BIT_IMEM_DW_OK | BIT_IMEM_CHKSUM_OK) & 0xFF)
    else:
        fw_ctrl |= ((BIT_DMEM_DW_OK | BIT_DMEM_CHKSUM_OK) & 0xFF)
    transport.write8(REG_MCUFW_CTRL, fw_ctrl & 0xFF)
    return True


# ---------------------------------------------------------------------------
# download_firmware_to_mem — section-level loop (mac.c:645..694)
# ---------------------------------------------------------------------------

def _section_upload(
    dev: usb.core.Device,
    transport: RTL8814AUTransport,
    section_data: bytes,
    dst: int,
    *,
    rsvd_pg_head: int,
    progress_cb: Callable[[int, int], None] | None = None,
    progress_base: int = 0,
    progress_total: int = 1,
) -> None:
    desc_size = TX_PKT_DESC_SZ  # 40 — drives iddma src offset + desc OFFSET field
    max_size = DLFW_MAX_CHUNK_SIZE

    val = transport.read32(REG_DDMA_CH0CTRL)
    val |= BIT_DDMACH0_RESET_CHKSUM_STS
    transport.write32(REG_DDMA_CH0CTRL, val)

    mem_offset = 0
    residue = len(section_data)
    first_part = True

    while residue > 0:
        pkt_size = min(max_size, residue)
        chunk = section_data[mem_offset: mem_offset + pkt_size]

        # send_firmware_pkt ZLP-avoidance (mac.c:550) — uses the kernel's
        # hardcoded 48, NOT the 40-byte desc.
        if ((pkt_size + FW_DLFW_ZLP_TXDESC) & (512 - 1)) == 0:
            chunk = chunk + b"\x00"
            send_size = pkt_size + 1
        else:
            send_size = pkt_size

        send_firmware_pkt(dev, transport, 0, chunk, send_size,
                          rsvd_pg_head=rsvd_pg_head)

        iddma_download_firmware(
            transport,
            OCPBASE_TXBUF_88XX + 0 + desc_size,
            dst + mem_offset,
            pkt_size,
            first=first_part,
        )

        first_part = False
        mem_offset += pkt_size
        residue -= pkt_size
        if progress_cb is not None:
            progress_cb(progress_base + mem_offset, progress_total)

    if not check_fw_checksum(transport, dst):
        raise IOError("FW section checksum failed")


# ---------------------------------------------------------------------------
# Reg backup/restore + end_flow + validate (mac.c:459..510, 761..774, 747..758)
# download_firmware_reg_backup is chip-generic; values verified vs mac.c.
# ---------------------------------------------------------------------------

def _reg_backup_for_dlfw(transport: RTL8814AUTransport) -> list[tuple[int, int, int]]:
    BIT_H2CQ_FULL = 1 << 31
    BIT_LD_RQPN = 1 << 31
    REG_FIFOPAGE_INFO_1 = 0x0230
    bckp: list[tuple[int, int, int]] = []

    # set HIQ to hi priority
    cur = transport.read8(REG_TXDMA_PQ_MAP + 1)
    bckp.append((REG_TXDMA_PQ_MAP + 1, 1, cur))
    transport.write8(REG_TXDMA_PQ_MAP + 1, (RTW_DMA_MAPPING_HIGH << 6) & 0xFF)

    # DLFW only uses HIQ; map HIQ to hi priority
    cur = transport.read8(REG_CR)
    bckp.append((REG_CR, 1, cur))
    bckp.append((REG_H2CQ_CSR, 4, BIT_H2CQ_FULL))  # stores the WRITE value
    transport.write8(REG_CR, (BIT_HCI_TXDMA_EN | BIT_TXDMA_EN) & 0xFF)
    transport.write32(REG_H2CQ_CSR, BIT_H2CQ_FULL)

    # HIQ + public queue page count
    cur16 = transport.read16(REG_FIFOPAGE_INFO_1)
    bckp.append((REG_FIFOPAGE_INFO_1, 2, cur16))
    cur32 = transport.read32(REG_RQPN_CTRL_2) | BIT_LD_RQPN
    bckp.append((REG_RQPN_CTRL_2, 4, cur32))
    transport.write16(REG_FIFOPAGE_INFO_1, 0x0200)
    transport.write32(REG_RQPN_CTRL_2, cur32)

    # disable beacon function
    cur = transport.read8(REG_BCN_CTRL)
    bckp.append((REG_BCN_CTRL, 1, cur))
    transport.write8(
        REG_BCN_CTRL,
        ((cur & ~BIT_EN_BCN_FUNCTION) | BIT_DIS_TSF_UDT) & 0xFF,
    )
    return bckp


def _reg_restore_for_dlfw(transport: RTL8814AUTransport,
                          bckp: list[tuple[int, int, int]]) -> None:
    for addr, size, val in bckp:
        if size == 1:
            transport.write8(addr, val & 0xFF)
        elif size == 2:
            transport.write16(addr, val & 0xFFFF)
        elif size == 4:
            transport.write32(addr, val & 0xFFFFFFFF)


def download_firmware_end_flow(transport: RTL8814AUTransport) -> None:
    """mac.c:761..774."""
    transport.write32(REG_TXDMA_STATUS, BTI_PAGE_OVF)
    fw_ctrl = transport.read16(REG_MCUFW_CTRL)
    if (fw_ctrl & BIT_CHECK_SUM_OK) != BIT_CHECK_SUM_OK:
        logger.warning(
            "download_firmware_end_flow: CHECK_SUM_OK not set "
            "(REG_MCUFW_CTRL=0x%04x); skipping FW_DW_RDY", fw_ctrl,
        )
        return
    fw_ctrl = (fw_ctrl | BIT_FW_DW_RDY) & ~BIT_MCUFWDL_EN & 0xFFFF
    transport.write16(REG_MCUFW_CTRL, fw_ctrl)


def download_firmware_validate(transport: RTL8814AUTransport) -> tuple[bool, int]:
    """mac.c:747..758. Returns (ok, last_MCUFW_CTRL)."""
    last = 0
    for _ in range(200):
        last = transport.read32(REG_MCUFW_CTRL)
        if (last & FW_READY_MASK) == (FW_READY & FW_READY_MASK):
            return True, last
        time.sleep(0.005)
    fw_key = transport.read32(REG_FW_DBG7) & FW_KEY_MASK
    if fw_key == ILLEGAL_KEY_GROUP:
        logger.error("FW validate: invalid FW key (0x%08x)", fw_key)
    return False, last


# ---------------------------------------------------------------------------
# Top-level driver — mirrors __rtw_download_firmware (mac.c:776..833)
# ---------------------------------------------------------------------------

def download_firmware(
    dev: usb.core.Device,
    transport: RTL8814AUTransport,
    fw_blob: bytes,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Upload the firmware via the iDDMA path.

    `fw_blob` is the FULL linux-firmware image (64-byte header + sections), as
    returned by load_firmware_blob(). The header is parsed here and stripped
    before each section body is pushed.

    Does NOT power on the chip — call mac.mac_power_on() first.
    Does NOT confirm the FW is running — call download_firmware_validate() after.
    """
    sections = parse_fw_header(fw_blob)
    total_body = sum(size for _, _, _, size in sections)

    wlan_cpu_enable(transport, False)              # mac.c:792
    bckp = _reg_backup_for_dlfw(transport)
    reset_platform(transport)

    # start_download_firmware (mac.c:716..718): set MCUFWDL_EN, preserve bits 11..13.
    val = transport.read16(REG_MCUFW_CTRL) & 0x3800
    val |= BIT_MCUFWDL_EN
    transport.write16(REG_MCUFW_CTRL, val & 0xFFFF)

    rsvd_pg_head = 0  # fifo.rsvd_boundary not yet configured (matches 8822b M1)
    done = 0
    for name, file_off, dst, size in sections:
        body = fw_blob[file_off: file_off + size]
        logger.debug("uploading %s: %d bytes -> 0x%08x", name, len(body), dst)
        _section_upload(dev, transport, body, dst,
                        rsvd_pg_head=rsvd_pg_head,
                        progress_cb=progress_cb,
                        progress_base=done,
                        progress_total=total_body)
        done += size

    _reg_restore_for_dlfw(transport, bckp)
    download_firmware_end_flow(transport)
    wlan_cpu_enable(transport, True)               # mac.c:805
