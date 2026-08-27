"""RTL8822BU firmware upload (modern iDDMA path).

Port of `__rtw_download_firmware` (mac.c:776) for the 8822b on USB.

Bulk-OUT TX descriptor stream (data path) + iDDMA register-write triggers
(control path). Layered:

  start_download_firmware
    │
    ├── download_firmware_to_mem  (per section: DMEM, then IMEM)
    │    │
    │    └── loop max_size=4096 chunks:
    │         send_firmware_pkt    (48-B tx_pkt_desc + chunk, bulk-OUT EP 0x05)
    │         iddma_download_firmware (3 reg writes + poll)
    │
    ├── check_fw_checksum (per section)
    └── download_firmware_end_flow + wlan_cpu_enable(true) + validate
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

from wifit3.chips.rtw88_base.registers import BIT_WL_PLATFORM_RST

from .constants import (
    DLFW_MAX_CHUNK_SIZE,
    TX_DESC_QSEL_BEACON,
    TX_PKT_DESC_SZ,
)
from .transport import RTL8822BUTransport

logger = logging.getLogger(__name__)


# FW header fields we need (parsed from linux-firmware rtw8822b_fw.bin:
# 161240 bytes = 64 header + 11216 DMEM (incl 8 chksum) + 149960 IMEM (incl 8 chksum)).
# These match capture-1 and are version 0x001e (signature 0x8822).
FW_BLOB_PATH = Path(__file__).resolve().parent / "assets" / "rtw8822b_fw.bin"
DMEM_ADDR = 0x00200000          # masked with ~BIT(31) from 0x80200000
DMEM_UPLOAD_SIZE = 11216        # 11208 + 8 chksum
IMEM_ADDR = 0x00000000          # masked with ~BIT(31) from 0x80000000
IMEM_UPLOAD_SIZE = 149960       # 149952 + 8 chksum
EMEM_PRESENT = False

# Endpoint on TP-Link T3U: 0x05 = first bulk-OUT (HIGH-priority lane).
# Looked up via dma_mapping_to_ep(dma_map_hi) = HIGH = index 0; descriptor
# order is [0x05, 0x06, 0x08] so out_ep[0] = 0x05.
EP_FW_BULK_OUT = 0x05


def load_firmware_blob() -> bytes:
    """Return the pcap-extracted FW body (DMEM + IMEM concatenated, no header)."""
    if not FW_BLOB_PATH.exists():
        raise FileNotFoundError(
            f"FW blob not found: {FW_BLOB_PATH}\n"
            "Run scripts/chips/rtl8822bu/extract_rtl8822bu_fw.py first."
        )
    data = FW_BLOB_PATH.read_bytes()
    expected = DMEM_UPLOAD_SIZE + IMEM_UPLOAD_SIZE
    if len(data) != expected:
        raise ValueError(
            f"FW blob size mismatch: got {len(data)}, expected {expected} "
            f"(DMEM {DMEM_UPLOAD_SIZE} + IMEM {IMEM_UPLOAD_SIZE})"
        )
    return data


# ---------------------------------------------------------------------------
# wlan_cpu_enable, reset_platform — mac.c:440..520
# ---------------------------------------------------------------------------

def wlan_cpu_enable(transport: RTL8822BUTransport, enable: bool) -> None:
    """mac.c:440..454."""
    if enable:
        transport.write8_set(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)
        transport.write8_set(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
    else:
        transport.write8_clr(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
        transport.write8_clr(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)


def reset_platform(transport: RTL8822BUTransport) -> None:
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
# tx_pkt_desc for FW upload (qsel=BEACON, offset=48, ls=1, tx_pkt_size=size)
# ---------------------------------------------------------------------------

def build_fw_tx_pkt_desc(chunk_size: int) -> bytes:
    """48-byte TX descriptor for a FW upload chunk.

    The TX desc layout for 8822b matches the rtw88 family-shared format
    (tx.h). We set:
      W0[15:0]    TXPKTSIZE = chunk_size
      W0[23:16]   OFFSET    = 48
      W0[26]      LS        = 1  (last segment)
      W1[12:8]    QSEL      = TX_DESC_QSEL_BEACON (=16)
      W4[6:0]     DATARATE  = 0 (not used for FW)
      W7[15:0]    CHECKSUM  — XOR of first 16 u16s

    The rest are 0. The checksum is "fill_txdesc_checksum_common" (tx.c:119).
    """
    desc = bytearray(TX_PKT_DESC_SZ)

    w0 = (
        (chunk_size & 0xFFFF)              # TXPKTSIZE
        | ((TX_PKT_DESC_SZ & 0xFF) << 16)  # OFFSET = 48
        | (1 << 26)                        # LS
    )
    w1 = (TX_DESC_QSEL_BEACON & 0x1F) << 8

    struct.pack_into("<I", desc, 0, w0)
    struct.pack_into("<I", desc, 4, w1)

    # Checksum (W7 low 16): XOR of first 16 u16s.
    chksum = 0
    for i in range(16):
        chksum ^= struct.unpack_from("<H", desc, i * 2)[0]
    struct.pack_into("<H", desc, 7 * 4, chksum & 0xFFFF)

    return bytes(desc)


# ---------------------------------------------------------------------------
# send_firmware_pkt — bulk-OUT of tx_desc + chunk, gated by BCN_VALID_V1 poll
# ---------------------------------------------------------------------------

def _poll32_mask(transport: RTL8822BUTransport, addr: int, mask: int,
                 target: int, attempts: int = 100,
                 interval_s: float = 0.001) -> bool:
    desired = target & mask
    for _ in range(attempts):
        v = transport.read32(addr)
        if (v & mask) == desired:
            return True
        time.sleep(interval_s)
    return False


def _poll16_mask(transport: RTL8822BUTransport, addr: int, mask: int,
                 target: int, attempts: int = 100,
                 interval_s: float = 0.001) -> bool:
    desired = target & mask
    for _ in range(attempts):
        v = transport.read16(addr)
        if (v & mask) == desired:
            return True
        time.sleep(interval_s)
    return False


def send_firmware_pkt(
    dev: usb.core.Device,
    transport: RTL8822BUTransport,
    pg_addr: int,
    data: bytes,
    size: int,
    *,
    rsvd_pg_head: int,
) -> None:
    """fw.c:1466..1535 — rtw_fw_write_data_rsvd_page.

    Builds [tx_desc + data[:size]] and writes it to the HIGH-priority
    bulk-OUT endpoint. The BCN_VALID_V1 bit signals "TX buffered into the
    rsvd page" — we poll it before continuing.

    `size` here is the FW chunk size; the caller has already applied the
    ZLP-avoidance +1 if needed. `data` already contains that trailing byte.
    """
    # bckp[0] = REG_CR+1; bckp[2] = REG_BCN_CTRL
    bckp_bcn_ctrl = transport.read8(REG_BCN_CTRL)

    # Non-8051 path (mac.c:1486..1488):
    pg_low = pg_addr & BIT_MASK_BCN_HEAD_1_V1
    transport.write16(REG_FIFOPAGE_CTRL_2, pg_low | BIT_BCN_VALID_V1)

    bckp_cr1 = transport.read8(REG_CR + 1)
    transport.write8(REG_CR + 1, bckp_cr1 | ((BIT_ENSWBCN >> 8) & 0xFF))

    transport.write8(
        REG_BCN_CTRL,
        (bckp_bcn_ctrl & ~BIT_EN_BCN_FUNCTION & 0xFF) | BIT_DIS_TSF_UDT,
    )

    # PCIE-only block skipped.

    # Bulk-OUT the [tx_desc + chunk] payload.
    pkt = build_fw_tx_pkt_desc(size) + data[:size]
    written = dev.write(EP_FW_BULK_OUT, pkt, 500)
    if written != len(pkt):
        raise IOError(f"FW bulk-OUT short: wrote {written}, expected {len(pkt)}")

    # Poll BCN_VALID_V1 for completion (modern path).
    if not _poll16_mask(transport, REG_FIFOPAGE_CTRL_2,
                        BIT_BCN_VALID_V1, BIT_BCN_VALID_V1,
                        attempts=200, interval_s=0.0005):
        raise IOError("bcn_valid never set — FW chunk wasn't latched")

    # Restore (mac.c:1525..1532)
    transport.write16(REG_FIFOPAGE_CTRL_2,
                      (rsvd_pg_head & BIT_MASK_BCN_HEAD_1_V1) | BIT_BCN_VALID_V1)
    transport.write8(REG_BCN_CTRL, bckp_bcn_ctrl)
    transport.write8(REG_CR + 1, bckp_cr1)


# ---------------------------------------------------------------------------
# iddma_download_firmware — three control writes + poll on BIT_DDMACH0_OWN
# ---------------------------------------------------------------------------

def iddma_download_firmware(
    transport: RTL8822BUTransport,
    src: int,
    dst: int,
    length: int,
    first: bool,
) -> None:
    """mac.c:574..590."""
    if not _poll32_mask(transport, REG_DDMA_CH0CTRL, BIT_DDMACH0_OWN, 0):
        raise IOError("iddma_download_firmware: BIT_DDMACH0_OWN didn't clear before xfer")

    ch0_ctrl = BIT_DDMACH0_CHKSUM_EN | BIT_DDMACH0_OWN
    ch0_ctrl |= length & BIT_MASK_DDMACH0_DLEN
    if not first:
        ch0_ctrl |= BIT_DDMACH0_CHKSUM_CONT

    transport.write32(REG_DDMA_CH0SA, src & 0xFFFFFFFF)
    transport.write32(REG_DDMA_CH0DA, dst & 0xFFFFFFFF)
    transport.write32(REG_DDMA_CH0CTRL, ch0_ctrl & 0xFFFFFFFF)

    if not _poll32_mask(transport, REG_DDMA_CH0CTRL, BIT_DDMACH0_OWN, 0):
        raise IOError("iddma_download_firmware: transfer never completed (OWN stuck)")


def check_fw_checksum(
    transport: RTL8822BUTransport, dst: int, is_imem: bool,
) -> bool:
    """mac.c:611..643.

    Returns True if the checksum status is clean and the corresponding
    DW_OK + CHKSUM_OK bits got latched into REG_MCUFW_CTRL.
    """
    ddma_ctrl = transport.read32(REG_DDMA_CH0CTRL)
    fw_ctrl = transport.read8(REG_MCUFW_CTRL)

    OCPBASE_DMEM_88XX = 0x00200000
    if ddma_ctrl & BIT_DDMACH0_CHKSUM_STS:
        # bad checksum
        if dst < OCPBASE_DMEM_88XX:
            fw_ctrl |= (BIT_IMEM_DW_OK & 0xFF)
            fw_ctrl &= ~(BIT_IMEM_CHKSUM_OK & 0xFF) & 0xFF
        else:
            fw_ctrl |= (BIT_DMEM_DW_OK & 0xFF)
            fw_ctrl &= ~(BIT_DMEM_CHKSUM_OK & 0xFF) & 0xFF
        transport.write8(REG_MCUFW_CTRL, fw_ctrl & 0xFF)
        logger.error("FW chksum mismatch for %s (dst=0x%x)",
                     "IMEM" if is_imem else "DMEM", dst)
        return False

    if dst < OCPBASE_DMEM_88XX:
        fw_ctrl |= ((BIT_IMEM_DW_OK | BIT_IMEM_CHKSUM_OK) & 0xFF)
    else:
        fw_ctrl |= ((BIT_DMEM_DW_OK | BIT_DMEM_CHKSUM_OK) & 0xFF)
    transport.write8(REG_MCUFW_CTRL, fw_ctrl & 0xFF)
    return True


# ---------------------------------------------------------------------------
# download_firmware_to_mem — section-level loop
# ---------------------------------------------------------------------------

def _section_upload(
    dev: usb.core.Device,
    transport: RTL8822BUTransport,
    section_data: bytes,
    dst: int,
    *,
    rsvd_pg_head: int,
    progress_cb: Callable[[int, int], None] | None = None,
    progress_base: int = 0,
    progress_total: int = 1,
) -> None:
    """Mirrors mac.c:645..694."""
    desc_size = TX_PKT_DESC_SZ
    max_size = DLFW_MAX_CHUNK_SIZE

    # Reset chksum status before this section (mac.c:663..665).
    val = transport.read32(REG_DDMA_CH0CTRL)
    val |= BIT_DDMACH0_RESET_CHKSUM_STS
    transport.write32(REG_DDMA_CH0CTRL, val)

    mem_offset = 0
    residue = len(section_data)
    first_part = True
    chunk_no = 0

    while residue > 0:
        pkt_size = min(max_size, residue)
        chunk = section_data[mem_offset: mem_offset + pkt_size]

        # send_firmware_pkt ZLP-avoidance (mac.c:546..554)
        if ((pkt_size + TX_PKT_DESC_SZ) & (512 - 1)) == 0:
            chunk = chunk + b"\x00"
            send_size = pkt_size + 1
        else:
            send_size = pkt_size

        pg_addr = (0 >> 7) & 0xFFFF  # src is always 0 in the iddma path
        send_firmware_pkt(dev, transport, pg_addr, chunk, send_size,
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
        chunk_no += 1
        if progress_cb is not None:
            progress_cb(progress_base + mem_offset, progress_total)

    if not check_fw_checksum(transport, dst, is_imem=(dst < 0x00200000)):
        raise IOError("FW section checksum failed")


# ---------------------------------------------------------------------------
# Reg backup/restore + end_flow + validate
# ---------------------------------------------------------------------------

def _reg_backup_for_dlfw(transport: RTL8822BUTransport) -> list[tuple[int, int, int]]:
    """mac.c:459..510 — DLFW_RESTORE_REG_NUM=6 entries.

    Matches the kernel pattern *exactly* including its quirks: bckp[2] for
    REG_H2CQ_CSR stores `BIT_H2CQ_FULL` (the value the kernel WROTE) rather
    than the read-back; bckp[4] for REG_RQPN_CTRL_2 stores `read | BIT_LD_RQPN`.
    """
    BIT_H2CQ_FULL = 1 << 31
    BIT_LD_RQPN = 1 << 31
    bckp: list[tuple[int, int, int]] = []

    # bckp[0] — REG_TXDMA_PQ_MAP+1 (1B); set HIQ to hi priority
    cur = transport.read8(REG_TXDMA_PQ_MAP + 1)
    bckp.append((REG_TXDMA_PQ_MAP + 1, 1, cur))
    transport.write8(REG_TXDMA_PQ_MAP + 1, (RTW_DMA_MAPPING_HIGH << 6) & 0xFF)

    # bckp[1] — REG_CR (1B); map HIQ to hi priority
    cur = transport.read8(REG_CR)
    bckp.append((REG_CR, 1, cur))

    # bckp[2] — REG_H2CQ_CSR (4B) — stores the WRITE value, not the read.
    bckp.append((REG_H2CQ_CSR, 4, BIT_H2CQ_FULL))
    transport.write8(REG_CR, (BIT_HCI_TXDMA_EN | BIT_TXDMA_EN) & 0xFF)
    transport.write32(REG_H2CQ_CSR, BIT_H2CQ_FULL)

    # bckp[3] — REG_FIFOPAGE_INFO_1 (2B); set HIQ page count to 0x200
    REG_FIFOPAGE_INFO_1 = 0x0230
    cur16 = transport.read16(REG_FIFOPAGE_INFO_1)
    bckp.append((REG_FIFOPAGE_INFO_1, 2, cur16))

    # bckp[4] — REG_RQPN_CTRL_2 (4B); enable BIT_LD_RQPN
    cur32 = transport.read32(REG_RQPN_CTRL_2) | BIT_LD_RQPN
    bckp.append((REG_RQPN_CTRL_2, 4, cur32))
    transport.write16(REG_FIFOPAGE_INFO_1, 0x0200)
    transport.write32(REG_RQPN_CTRL_2, cur32)

    # bckp[5] — REG_BCN_CTRL (1B); disable beacon function
    cur = transport.read8(REG_BCN_CTRL)
    bckp.append((REG_BCN_CTRL, 1, cur))
    transport.write8(
        REG_BCN_CTRL,
        ((cur & ~BIT_EN_BCN_FUNCTION) | BIT_DIS_TSF_UDT) & 0xFF,
    )

    return bckp


def _reg_restore_for_dlfw(transport: RTL8822BUTransport,
                          bckp: list[tuple[int, int, int]]) -> None:
    for addr, size, val in bckp:
        if size == 1:
            transport.write8(addr, val & 0xFF)
        elif size == 2:
            transport.write16(addr, val & 0xFFFF)
        elif size == 4:
            transport.write32(addr, val & 0xFFFFFFFF)


def download_firmware_end_flow(transport: RTL8822BUTransport) -> None:
    """mac.c:761..774."""
    transport.write32(REG_TXDMA_STATUS, BTI_PAGE_OVF)
    fw_ctrl = transport.read16(REG_MCUFW_CTRL)
    if (fw_ctrl & BIT_CHECK_SUM_OK) != BIT_CHECK_SUM_OK:
        logger.warning(
            "download_firmware_end_flow: CHECK_SUM_OK not set "
            "(REG_MCUFW_CTRL=0x%04x); skipping FW_DW_RDY",
            fw_ctrl,
        )
        return
    fw_ctrl = (fw_ctrl | BIT_FW_DW_RDY) & ~BIT_MCUFWDL_EN & 0xFFFF
    transport.write16(REG_MCUFW_CTRL, fw_ctrl)


def download_firmware_validate(transport: RTL8822BUTransport) -> tuple[bool, int]:
    """mac.c:747..758.

    Returns (ok, last_MCUFW_CTRL).
    """
    attempts = 200
    last = 0
    for _ in range(attempts):
        last = transport.read32(REG_MCUFW_CTRL)
        if (last & FW_READY_MASK) == (FW_READY & FW_READY_MASK):
            return True, last
        time.sleep(0.005)
    fw_key = transport.read32(REG_FW_DBG7) & FW_KEY_MASK
    if fw_key == ILLEGAL_KEY_GROUP:
        logger.error("FW validate: invalid FW key (0x%08x)", fw_key)
    return False, last


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

def download_firmware(
    dev: usb.core.Device,
    transport: RTL8822BUTransport,
    fw_blob: bytes,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Modern iDDMA path — uploads DMEM + IMEM + optionally EMEM.

    `fw_blob` is the *header-stripped* upload body (DMEM section followed
    by IMEM section, each with their trailing 8-byte checksum). For
    8822b that's `DMEM_UPLOAD_SIZE + IMEM_UPLOAD_SIZE` = 161176 bytes.

    Does NOT power on the chip — call `mac.mac_power_on(transport)` first.
    Does NOT validate the FW running — call `download_firmware_validate`
    after this returns to confirm the wlan CPU is up.
    """
    if len(fw_blob) != DMEM_UPLOAD_SIZE + IMEM_UPLOAD_SIZE:
        raise ValueError(
            f"FW blob size mismatch: got {len(fw_blob)}, "
            f"expected {DMEM_UPLOAD_SIZE + IMEM_UPLOAD_SIZE}"
        )

    # mac.c:792 — disable CPU before upload.
    wlan_cpu_enable(transport, False)

    bckp = _reg_backup_for_dlfw(transport)
    reset_platform(transport)

    # mac.c:716..718 — set BIT_MCUFWDL_EN in REG_MCUFW_CTRL low 16, preserve
    # bits 11..13 (0x3800).
    val = transport.read16(REG_MCUFW_CTRL) & 0x3800
    val |= BIT_MCUFWDL_EN
    transport.write16(REG_MCUFW_CTRL, val & 0xFFFF)

    rsvd_pg_head = 0  # rtwdev->fifo.rsvd_boundary — not yet set up

    # DMEM section
    dmem = fw_blob[:DMEM_UPLOAD_SIZE]
    logger.debug("uploading DMEM: %d bytes -> 0x%08x", len(dmem), DMEM_ADDR)
    _section_upload(dev, transport, dmem, DMEM_ADDR,
                    rsvd_pg_head=rsvd_pg_head,
                    progress_cb=progress_cb,
                    progress_base=0,
                    progress_total=len(fw_blob))

    # IMEM section
    imem = fw_blob[DMEM_UPLOAD_SIZE:]
    logger.debug("uploading IMEM: %d bytes -> 0x%08x", len(imem), IMEM_ADDR)
    _section_upload(dev, transport, imem, IMEM_ADDR,
                    rsvd_pg_head=rsvd_pg_head,
                    progress_cb=progress_cb,
                    progress_base=len(dmem),
                    progress_total=len(fw_blob))

    _reg_restore_for_dlfw(transport, bckp)
    download_firmware_end_flow(transport)

    # mac.c:805 — re-enable CPU after upload completes.
    wlan_cpu_enable(transport, True)
