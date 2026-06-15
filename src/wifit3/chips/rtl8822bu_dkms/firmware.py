"""RTL8822BU firmware — blob loader + WLAN_FW header parse.

The morrownr DKMS driver compiles its firmware in as ``array_mp_8822b_fw_nic``
(v30.20, 161240 B). That blob — NOT the linux-firmware rtw88 one (161176 B, a
different version) — is what the cold captures download, so it is the wire ground
truth and is shipped verbatim in ``assets/rtl8822bu_fw.bin``. The HALMAC iDDMA
download (the register + bulk-OUT sequence that streams it into MCU IMEM/DMEM) is
wired at the firmware-download milestone; the gate then byte-verifies this blob
against the recorded bulk-OUT packets.

[SRC] hal/rtl8822b/hal8822b_fw.c:13389 (array), halmac_fw_88xx.c:115/234 (download),
      halmac_fw_info.h:22-40 (header layout).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    BIT_DDMACH0_CHKSUM_CONT,
    BIT_DDMACH0_CHKSUM_EN,
    BIT_DDMACH0_CHKSUM_STS,
    BIT_DDMACH0_OWN,
    BIT_DDMACH0_RESET_CHKSUM_STS,
    BIT_DMEM_CHKSUM_OK,
    BIT_DMEM_DW_OK,
    BIT_FW_DW_RDY,
    BIT_HCI_TXDMA_EN,
    BIT_IMEM_CHKSUM_OK,
    BIT_IMEM_DW_OK,
    BIT_MASK_BCN_HEAD_1_V1,
    BIT_MASK_DDMACH0_DLEN,
    BIT_TXDMA_EN,
    DLFW_RESTORE_REG_NUM,
    DLFW_USB_PKT_SIZE,
    FW_BLOB_SIZE,
    HALMAC_DMA_MAPPING_HIGH,
    LTECOEX_ACCESS_CTRL,
    LTECOEX_REG_OFFSET_DL,
    MCUFW_CTRL_FW_EXIST,
    MCUFW_CTRL_IDMEM_CHKSUM,
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
    REG_FWHW_TXQ_CTRL,
    REG_H2CQ_CSR,
    REG_LTECOEX_READ_DATA,
    REG_LTECOEX_WRITE_DATA,
    REG_MCUFW_CTRL,
    REG_RQPN_CTRL_2,
    REG_RSV_CTRL,
    REG_SYS_CLK_CTRL,
    REG_SYS_FUNC_EN,
    REG_TXDMA_PQ_MAP,
    REG_TXDMA_STATUS,
    REG_TXPKT_EMPTY,
    RSVD_PG_BOUNDARY_FWDL,
    TX_DESC_SIZE_88XX,
    TXDESC_QSEL_BEACON,
    WLAN_FW_HDR_CHKSUM_SIZE,
    WLAN_FW_HDR_DMEM_ADDR,
    WLAN_FW_HDR_DMEM_SIZE,
    WLAN_FW_HDR_EMEM_ADDR,
    WLAN_FW_HDR_EMEM_SIZE,
    WLAN_FW_HDR_IMEM_ADDR,
    WLAN_FW_HDR_IMEM_SIZE,
    WLAN_FW_HDR_MEM_USAGE,
    WLAN_FW_HDR_SIZE,
)

_POLL_CAP = 1_000_000        # generous; the replay matches on the recorded read

_FW_PATH = Path(__file__).with_name("assets") / "rtl8822bu_fw.bin"


@dataclass
class FwHeader:
    """Parsed WLAN_FW header. Sizes here already include the 8-byte per-segment
    checksum the downloader appends, matching start_dlfw_88xx's arithmetic."""
    dmem_addr: int
    dmem_size: int          # raw dmem + checksum
    imem_addr: int
    imem_size: int          # raw imem + checksum
    emem_addr: int
    emem_size: int          # raw emem + checksum, or 0 when absent


def load_firmware_blob() -> bytes:
    """The 161240-byte morrownr 8822b NIC firmware (wire ground truth)."""
    blob = _FW_PATH.read_bytes()
    if len(blob) != FW_BLOB_SIZE:
        raise ValueError(f"RTL8822BU FW blob is {len(blob)} B, expected {FW_BLOB_SIZE}")
    return blob


def parse_fw_header(blob: bytes) -> FwHeader:
    """start_dlfw_88xx header decode [SRC] halmac_fw_88xx.c:246-257. The MEM_USAGE
    BIT(4) gates emem; each present segment carries an extra 8-byte checksum. The high
    bit of each address is a flag the downloader masks off."""
    def le32(off: int) -> int:
        return struct.unpack_from("<I", blob, off)[0]

    dmem_size = le32(WLAN_FW_HDR_DMEM_SIZE) + WLAN_FW_HDR_CHKSUM_SIZE
    imem_size = le32(WLAN_FW_HDR_IMEM_SIZE) + WLAN_FW_HDR_CHKSUM_SIZE
    emem_size = 0
    if blob[WLAN_FW_HDR_MEM_USAGE] & (1 << 4):
        emem_size = le32(WLAN_FW_HDR_EMEM_SIZE) + WLAN_FW_HDR_CHKSUM_SIZE

    real = WLAN_FW_HDR_SIZE + dmem_size + imem_size + emem_size
    if real != len(blob):
        raise ValueError(f"RTL8822BU FW size {len(blob)} != header-derived {real}")

    return FwHeader(
        dmem_addr=le32(WLAN_FW_HDR_DMEM_ADDR) & ~(1 << 31),
        dmem_size=dmem_size,
        imem_addr=le32(WLAN_FW_HDR_IMEM_ADDR) & ~(1 << 31),
        imem_size=imem_size,
        emem_addr=le32(WLAN_FW_HDR_EMEM_ADDR) & ~(1 << 31),
        emem_size=emem_size,
    )


# --- TX descriptor for a FW (BEACON-qsel rsvd-page) packet -------------------
def _set_le32_bits(buf: bytearray, byte_off: int, shift: int, nbits: int, value: int) -> None:
    w = struct.unpack_from("<I", buf, byte_off)[0]
    mask = ((1 << nbits) - 1) << shift
    struct.pack_into("<I", buf, byte_off, (w & ~mask) | ((value << shift) & mask))


def build_fw_txdesc(pkt_size: int) -> bytes:
    """The 48-byte TX descriptor usb_write_data_not_xmitframe builds for a FW packet:
    TXPKTSIZE + OFFSET(48) + QSEL(beacon), then the XOR-16 checksum over the first 32 bytes.
    [SRC] rtl8822bu_halmac.c:127-184, halmac_common_8822b.c fill_txdesc_check_sum_8822b."""
    d = bytearray(TX_DESC_SIZE_88XX)
    _set_le32_bits(d, 0x00, 0, 16, pkt_size)            # TXPKTSIZE  word0[0:16]
    _set_le32_bits(d, 0x00, 16, 8, TX_DESC_SIZE_88XX)   # OFFSET     word0[16:24]
    _set_le32_bits(d, 0x04, 8, 5, TXDESC_QSEL_BEACON)   # QSEL       word1[8:13]
    _set_le32_bits(d, 0x1C, 0, 16, 0)                   # checksum field cleared for the XOR
    chksum = 0
    for i in range(16):
        chksum ^= struct.unpack_from("<H", d, 2 * i)[0]
    _set_le32_bits(d, 0x1C, 0, 16, chksum)
    return bytes(d)


# --- HALMAC download_firmware_88xx (the register + bulk sequence) -------------
def _txfifo_wait_empty(t) -> None:
    """txfifo_is_empty_88xx(chk_num=10) [SRC] halmac_common_88xx.c:3271 — gate FW DL on a
    drained TX FIFO. Fixed 10 checks of REG_TXPKT_EMPTY == 0xFF and +1 bits[2:1] == 0x06."""
    for _ in range(10):
        if t.read8(REG_TXPKT_EMPTY) != 0xFF:
            raise RuntimeError("RTL8822BU: TX FIFO not empty before FW download")
        if (t.read8(REG_TXPKT_EMPTY + 1) & 0x06) != 0x06:
            raise RuntimeError("RTL8822BU: TX FIFO not empty before FW download")


def _ltecoex_read(t, offset: int) -> int:
    """ltecoex_reg_read_88xx [SRC] halmac_common_88xx.c:3338 — indirect LTE-coex read."""
    for _ in range(_POLL_CAP):
        if t.read8(LTECOEX_ACCESS_CTRL + 3) & (1 << 5):
            break
    else:
        raise RuntimeError("RTL8822BU: ltecoex not ready (read)")
    t.write32(LTECOEX_ACCESS_CTRL, 0x800F0000 | offset)
    return t.read32(REG_LTECOEX_READ_DATA)


def _ltecoex_write(t, offset: int, value: int) -> None:
    """ltecoex_reg_write_88xx [SRC] halmac_common_88xx.c:3369."""
    for _ in range(_POLL_CAP):
        if t.read8(LTECOEX_ACCESS_CTRL + 3) & (1 << 5):
            break
    else:
        raise RuntimeError("RTL8822BU: ltecoex not ready (write)")
    t.write32(REG_LTECOEX_WRITE_DATA, value)
    t.write32(LTECOEX_ACCESS_CTRL, 0xC00F0000 | offset)


def _wlan_cpu_en(t, enable: bool) -> None:
    """wlan_cpu_en_88xx [SRC] halmac_fw_88xx.c:354 — gate the WLAN CPU + its IO interface."""
    if enable:
        t.write8(REG_RSV_CTRL + 1, t.read8(REG_RSV_CTRL + 1) | (1 << 0))
        t.write8(REG_SYS_FUNC_EN + 1, t.read8(REG_SYS_FUNC_EN + 1) | (1 << 2))
    else:
        t.write8(REG_SYS_FUNC_EN + 1, t.read8(REG_SYS_FUNC_EN + 1) & ~(1 << 2))
        t.write8(REG_RSV_CTRL + 1, t.read8(REG_RSV_CTRL + 1) & ~(1 << 0))


def _pltfm_reset(t) -> None:
    """pltfm_reset_88xx [SRC] halmac_fw_88xx.c:384 — DMEM reset + the 8822b clock-sync toggle."""
    t.write8(REG_CPU_DMEM_CON + 2, t.read8(REG_CPU_DMEM_CON + 2) & ~(1 << 0))
    t.write8(REG_SYS_CLK_CTRL + 1, t.read8(REG_SYS_CLK_CTRL + 1) & ~(1 << 6))
    t.write8(REG_CPU_DMEM_CON + 2, t.read8(REG_CPU_DMEM_CON + 2) | (1 << 0))
    t.write8(REG_SYS_CLK_CTRL + 1, t.read8(REG_SYS_CLK_CTRL + 1) | (1 << 6))


def _dl_rsvd_page(t, pg_addr: int, buf: bytes) -> None:
    """dl_rsvd_page_88xx [SRC] halmac_common_88xx.c:314 — bracket the bulk send with the
    FIFOPAGE/CR/TXQ save+set, then send (txdesc + buf) on bulk-OUT, then poll bcn-valid and
    restore."""
    pg_addr &= BIT_MASK_BCN_HEAD_1_V1
    t.write16(REG_FIFOPAGE_CTRL_2, pg_addr | (1 << 15))
    cr1 = t.read8(REG_CR + 1)
    t.write8(REG_CR + 1, cr1 | (1 << 0))
    txq2 = t.read8(REG_FWHW_TXQ_CTRL + 2)
    t.write8(REG_FWHW_TXQ_CTRL + 2, txq2 & ~(1 << 6))

    t.bulk_out(build_fw_txdesc(len(buf)) + buf)

    for _ in range(_POLL_CAP):
        if t.read8(REG_FIFOPAGE_CTRL_2 + 1) & (1 << 7):
            break
    else:
        raise RuntimeError("RTL8822BU: bcn-valid poll timed out in rsvd-page DL")

    t.write16(REG_FIFOPAGE_CTRL_2, RSVD_PG_BOUNDARY_FWDL | (1 << 15))
    t.write8(REG_FWHW_TXQ_CTRL + 2, txq2)
    t.write8(REG_CR + 1, cr1)


def _send_fwpkt(t, pg_addr: int, chunk: bytes) -> None:
    """send_fwpkt_88xx [SRC] halmac_fw_88xx.c:719 — for USB, pad one extra byte when
    (chunk + txdesc) is a 512-multiple, so the bulk transfer is never an exact USB packet."""
    if (len(chunk) + TX_DESC_SIZE_88XX) % 512 == 0:
        chunk = chunk + b"\x00"
    _dl_rsvd_page(t, pg_addr, chunk)


def _iddma_dlfw(t, src: int, dest: int, length: int, first: bool) -> None:
    """iddma_dlfw_88xx [SRC] halmac_fw_88xx.c:753 — DDMA-copy the staged packet from TXBUF
    into MCU mem with a running checksum (continued after the first block)."""
    for _ in range(_POLL_CAP):
        if not (t.read32(REG_DDMA_CH0CTRL) & BIT_DDMACH0_OWN):
            break
    else:
        raise RuntimeError("RTL8822BU: DDMA ch0 not ready")
    ctrl = BIT_DDMACH0_CHKSUM_EN | BIT_DDMACH0_OWN | (length & BIT_MASK_DDMACH0_DLEN)
    if not first:
        ctrl |= BIT_DDMACH0_CHKSUM_CONT
    t.write32(REG_DDMA_CH0SA, src)
    t.write32(REG_DDMA_CH0DA, dest)
    t.write32(REG_DDMA_CH0CTRL, ctrl)
    for _ in range(_POLL_CAP):
        if not (t.read32(REG_DDMA_CH0CTRL) & BIT_DDMACH0_OWN):
            break
    else:
        raise RuntimeError("RTL8822BU: DDMA ch0 copy timed out")


def _check_fw_chksum(t, dest: int) -> None:
    """check_fw_chksum_88xx [SRC] halmac_fw_88xx.c:803 — mark IMEM/DMEM DW+chksum OK in
    REG_MCUFW_CTRL (or raise on a checksum-status flag)."""
    fw_ctrl = t.read8(REG_MCUFW_CTRL)
    fail = bool(t.read32(REG_DDMA_CH0CTRL) & BIT_DDMACH0_CHKSUM_STS)
    if dest < OCPBASE_DMEM_88XX:
        dw_ok, chk_ok = BIT_IMEM_DW_OK, BIT_IMEM_CHKSUM_OK
    else:
        dw_ok, chk_ok = BIT_DMEM_DW_OK, BIT_DMEM_CHKSUM_OK
    if fail:
        t.write8(REG_MCUFW_CTRL, (fw_ctrl | dw_ok) & ~chk_ok)
        raise RuntimeError(f"RTL8822BU: FW checksum fail @0x{dest:x}")
    t.write8(REG_MCUFW_CTRL, fw_ctrl | dw_ok | chk_ok)


def _dlfw_to_mem(t, seg: bytes, dest: int) -> None:
    """dlfw_to_mem_88xx [SRC] halmac_fw_88xx.c:567 — stage each <=4096 B block to the rsvd
    page then DDMA it to ``dest``; one running checksum spans the whole segment."""
    t.write32(REG_DDMA_CH0CTRL, t.read32(REG_DDMA_CH0CTRL) | BIT_DDMACH0_RESET_CHKSUM_STS)
    src_txbuf = OCPBASE_TXBUF_88XX + TX_DESC_SIZE_88XX   # src offset is 0 throughout (pg 0)
    offset, first = 0, True
    while offset < len(seg):
        pkt = seg[offset:offset + DLFW_USB_PKT_SIZE]
        _send_fwpkt(t, 0, pkt)
        _iddma_dlfw(t, src_txbuf, dest + offset, len(pkt), first)
        first = False
        offset += len(pkt)
    _check_fw_chksum(t, dest)


def _start_dlfw(t, blob: bytes, hdr: FwHeader) -> None:
    """start_dlfw_88xx [SRC] halmac_fw_88xx.c:233 — enable FWDL, then download dmem + imem."""
    v16 = (t.read16(REG_MCUFW_CTRL) & 0x3800) | (1 << 0)    # keep boot-sel bits, set FWDL
    t.write16(REG_MCUFW_CTRL, v16)
    body = blob[WLAN_FW_HDR_SIZE:]
    _dlfw_to_mem(t, body[:hdr.dmem_size], hdr.dmem_addr)
    _dlfw_to_mem(t, body[hdr.dmem_size:hdr.dmem_size + hdr.imem_size], hdr.imem_addr)


def _dlfw_end_flow(t) -> None:
    """dlfw_end_flow_88xx [SRC] halmac_fw_88xx.c:647 — finish DDMA, verify the IMEM/DMEM
    checksum bits, set FW-download-ready, enable the CPU, and poll FW-ready (0xC078)."""
    t.write32(REG_TXDMA_STATUS, 1 << 2)
    fw_ctrl = t.read16(REG_MCUFW_CTRL)
    if (fw_ctrl & MCUFW_CTRL_IDMEM_CHKSUM) != MCUFW_CTRL_IDMEM_CHKSUM:
        raise RuntimeError("RTL8822BU: IMEM/DMEM checksum not OK after FW download")
    t.write16(REG_MCUFW_CTRL, (fw_ctrl | BIT_FW_DW_RDY) & ~(1 << 0))
    _wlan_cpu_en(t, enable=True)
    for _ in range(_POLL_CAP):
        if t.read16(REG_MCUFW_CTRL) == MCUFW_CTRL_FW_EXIST:    # 0xC078 == FW ready
            return
    raise RuntimeError("RTL8822BU: FW-ready (0x80==0xC078) poll timed out")


def download(t, blob: bytes) -> None:
    """download_firmware_88xx [SRC] halmac_fw_88xx.c:115 — the full HALMAC iDDMA FW upload:
    wait TX-FIFO empty, back up + reconfigure the TXDMA/HIQ/beacon path, reset the platform,
    stream dmem+imem via the rsvd-page + DDMA loop, restore the saved registers, then run the
    FW-ready end flow. LTE-coex 0x38 is saved/restored around the whole thing."""
    _txfifo_wait_empty(t)
    lte_backup = _ltecoex_read(t, LTECOEX_REG_OFFSET_DL)
    _wlan_cpu_en(t, enable=False)

    # Save the registers the download perturbs, then set the FWDL TXDMA/HIQ/beacon config.
    # The vendor interleaves each save with its set (R, W, R, W, ...) — reproduce that exact
    # order, not all-reads-then-all-writes. [SRC] halmac_fw_88xx.c:146-187
    pq_map = t.read8(REG_TXDMA_PQ_MAP + 1)
    t.write8(REG_TXDMA_PQ_MAP + 1, HALMAC_DMA_MAPPING_HIGH << 6)
    cr = t.read8(REG_CR)                         # H2CQ_CSR is restored to BIT31 (no save read)
    t.write8(REG_CR, BIT_HCI_TXDMA_EN | BIT_TXDMA_EN)
    t.write32(REG_H2CQ_CSR, 1 << 31)
    fifopage = t.read16(REG_FIFOPAGE_INFO_1)
    rqpn = t.read32(REG_RQPN_CTRL_2) | (1 << 31)
    t.write16(REG_FIFOPAGE_INFO_1, 0x200)
    t.write32(REG_RQPN_CTRL_2, rqpn)
    bcn = t.read8(REG_BCN_CTRL)
    t.write8(REG_BCN_CTRL, (bcn & ~(1 << 3)) | (1 << 4))
    backups = [
        (REG_TXDMA_PQ_MAP + 1, 1, pq_map), (REG_CR, 1, cr), (REG_H2CQ_CSR, 4, 1 << 31),
        (REG_FIFOPAGE_INFO_1, 2, fifopage), (REG_RQPN_CTRL_2, 4, rqpn), (REG_BCN_CTRL, 1, bcn),
    ]
    assert len(backups) == DLFW_RESTORE_REG_NUM

    _pltfm_reset(t)
    _start_dlfw(t, blob, parse_fw_header(blob))

    for reg, width, value in backups:           # restore_mac_reg_88xx [SRC] halmac_fw_88xx.c:620
        (t.write8 if width == 1 else t.write16 if width == 2 else t.write32)(reg, value)

    _dlfw_end_flow(t)
    _ltecoex_write(t, LTECOEX_REG_OFFSET_DL, lte_backup)
