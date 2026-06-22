"""RTL8821CU firmware download (HALMAC iDDMA path).

Reached from ``bringup.read_mac_hidden_rpt`` (the chip-info read powers the chip and pulls FW
capability bits, so the cold-boot FW download happens there). ``rtw_halmac_dlfw`` powers the
chip on again (idempotent), drains the TX FIFO, then ``download_firmware_88xx`` pushes the FW
blob into the 8051's DMEM/IMEM/EMEM.

Mechanism: the FW blob's three sections are each chunked (≤ 4096 B); per chunk the host bulk-OUTs
a 48-byte TX descriptor + the chunk into TXBUF (rsvd-page path), then the iDDMA engine
(REG_DDMA_CH0*) copies TXBUF→target memory and runs a rolling checksum. End flow sets FW-ready
and polls REG_MCUFW_CTRL == 0xC078.

Ported from:
  [SRC] hal/hal_halmac.c:3964 rtw_halmac_dlfw / :3350 download_fw / :3802 txfifo_wait_empty
  [SRC] hal/halmac/halmac_88xx/halmac_fw_88xx.c:115 download_firmware_88xx / :233 start_dlfw_88xx
        / :567 dlfw_to_mem_88xx / :720 send_fwpkt_88xx / :754 iddma_dlfw_88xx / :784 iddma_en_88xx
        / :647 dlfw_end_flow_88xx / :802 check_fw_chksum_88xx
  [SRC] hal/halmac/halmac_88xx/halmac_common_88xx.c:314 dl_rsvd_page_88xx / :3338 ltecoex_reg_read
        / :3368 ltecoex_reg_write / :3271 txfifo_is_empty_88xx / :355 wlan_cpu_en_88xx
  [SRC] hal/rtl8821c/usb/rtl8821cu_halmac.c:130 usb_write_data (TX-desc build)
  [SRC] hal/halmac/halmac_88xx/halmac_8821c/halmac_common_8821c.c:138 fill_txdesc_check_sum
Register addrs/bits pasted from halmac_reg2.h / halmac_bit_8821c.h.
"""
from __future__ import annotations

from pathlib import Path

from . import mac, tx

# --- registers [SRC] halmac_reg2.h -----------------------------------------
REG_SYS_FUNC_EN = 0x0002
REG_SYS_CLK_CTRL = 0x0008           # :64
REG_RSV_CTRL = 0x001C
REG_MCUFW_CTRL = 0x0080
REG_CR = 0x0100
REG_TXDMA_PQ_MAP = 0x010C           # :844
REG_FIFOPAGE_CTRL_2 = 0x0204        # :1223 (rsvd-page window + bcn-valid at +1)
REG_TXDMA_STATUS = 0x0210           # :1255
REG_TXPKT_EMPTY = 0x041A            # :2580
REG_FWHW_TXQ_CTRL = 0x0420          # :2591
REG_RQPN_CTRL_2 = 0x022C            # :1349
REG_FIFOPAGE_INFO_1 = 0x0230        # :1363
REG_BCN_CTRL = 0x0550               # :3646
REG_CPU_DMEM_CON = 0x1080           # :6204
REG_DDMA_CH0SA = 0x1200             # :6657
REG_DDMA_CH0DA = 0x1204             # :6658
REG_DDMA_CH0CTRL = 0x1208           # :6659
REG_H2CQ_CSR = 0x1330               # :6772
REG_LTECOEX_CTRL = 0x1700           # REG_WL2LTECOEX_INDIRECT_ACCESS_CTRL_V1 :8232
REG_LTECOEX_WDATA = 0x1704          # :8233
REG_LTECOEX_RDATA = 0x1708          # :8234
# general-info H2C path
REG_PKTBUF_DBG_CTRL = 0x0140        # :? FIFO page select for dump
REG_HMETFR = 0x01CC                 # H2C box fw-read flags
REG_HMEBOX0 = 0x01D0                # H2C message box 0
REG_HMEBOX_E0 = 0x01F0              # H2C ext message box 0
REG_RCR = 0x0608

_QSEL_H2C = 0x13                    # [SRC] halmac_type.h HALMAC_TXDESC_QSEL_H2C_CMD
_H2C_PKT_SIZE = 32                  # [SRC] halmac_88xx_cfg.h:32 H2C_PKT_SIZE_88XX
_SUB_CMD_GENERAL_INFO = 0x0D       # [SRC] halmac_fw_offload_h2c_nic.h:88
_SUB_CMD_PHYDM_INFO = 0x11         # [SRC] halmac_fw_offload_h2c_nic.h:92
_HALMAC_RF_1T1R = 4                # [SRC] halmac_type.h:1080
_BB_PATH_A = 1                     # rx/tx ant status for the 1T1R A path

# --- bits [SRC] halmac_bit_8821c.h ; CR/iDDMA semantics ---------------------
_BIT_HCI_TXDMA_EN = 1 << 0
_BIT_TXDMA_EN = 1 << 2
_BIT_FW_DW_RDY = 1 << 14            # :1622
_BIT_IMEM_DW_OK = 1 << 3
_BIT_IMEM_CHKSUM_OK = 1 << 4
_BIT_DMEM_DW_OK = 1 << 5
_BIT_DMEM_CHKSUM_OK = 1 << 6
_BIT_DDMACH0_OWN = 1 << 31
_BIT_DDMACH0_CHKSUM_EN = 1 << 29
_BIT_DDMACH0_CHKSUM_STS = 1 << 27
_BIT_DDMACH0_RESET_CHKSUM_STS = 1 << 25
_BIT_DDMACH0_CHKSUM_CONT = 1 << 24
_DDMACH0_DLEN_MASK = 0x3FFFF

# --- transfer / FW-header constants ----------------------------------------
_OCPBASE_TXBUF = 0x18780000        # [SRC] halmac_88xx_cfg.h:38
_OCPBASE_DMEM = 0x00200000         # [SRC] halmac_88xx_cfg.h:39
_TX_DESC_SIZE = 48                 # [SRC] rtw_xmit.h:219 TXDESC_SIZE (HALMAC_TX_DESC_SIZE_8821C)
_PACKET_OFFSET_SZ = 8              # [SRC] rtw_xmit.h:239
_USB_BULK_OUT_SIZE = 512           # USB2 bulk-OUT max packet (UsbBulkOutSize)
_DLFW_PKT_SIZE = 4096              # adapter->dlfw_pkt_size for 8821c USB (recorded chunk size)
_QSEL_BEACON = 0x10                # [SRC] halmac_type.h:627 HALMAC_TXDESC_QSEL_BEACON
_DMA_MAPPING_HIGH = 3              # [SRC] halmac_type.h HALMAC_DMA_MAPPING_HIGH
_CHKSUM_SIZE = 8                   # WLAN_FW_HDR_CHKSUM_SIZE
_HDR_SIZE = 64                     # WLAN_FW_HDR_SIZE
_HDR_MEM_USAGE, _HDR_DMEM_ADDR, _HDR_DMEM_SIZE = 24, 32, 36
_HDR_IMEM_SIZE, _HDR_EMEM_SIZE, _HDR_EMEM_ADDR, _HDR_IMEM_ADDR = 48, 52, 56, 60

_FW_READY = 0xC078                 # REG_MCUFW_CTRL value once the CPU is up
_POLL = 20000

_FW_BIN = Path(__file__).with_name("assets") / "rtl8821cu_fw_nic.bin"


# --- little helpers ---------------------------------------------------------
def _le32(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off + 4], "little")


def _set_field(buf: bytearray, off: int, start: int, length: int, value: int) -> None:
    """SET_BITS_TO_LE_4BYTE — set bits [start, start+length) of the LE u32 at buf[off]."""
    word = int.from_bytes(buf[off:off + 4], "little")
    mask = ((1 << length) - 1) << start
    word = (word & ~mask) | ((value << start) & mask)
    buf[off:off + 4] = (word & 0xFFFFFFFF).to_bytes(4, "little")


def _txfifo_wait_empty(t) -> None:
    """txfifo_is_empty_88xx(chk_num=10): 10x (TXPKT_EMPTY == 0xFF and +1 & 0x06 == 0x06); the
    wait loop breaks on the first empty check, which a cold chip passes. [SRC] common_88xx.c:3271."""
    for _ in range(10):
        if t.read8(REG_TXPKT_EMPTY) != 0xFF or (t.read8(REG_TXPKT_EMPTY + 1) & 0x06) != 0x06:
            raise RuntimeError("RTL8821CU: TX FIFO not empty before FW download")


def _ltecoex_read(t, offset: int) -> int:
    """ltecoex_reg_read_88xx [SRC] common_88xx.c:3338 — wait ready, issue read cmd, take data."""
    for _ in range(_POLL):
        if t.read8(REG_LTECOEX_CTRL + 3) & (1 << 5):
            break
    else:
        raise RuntimeError("RTL8821CU: ltecoex read not ready")
    t.write32(REG_LTECOEX_CTRL, 0x800F0000 | offset)
    return t.read32(REG_LTECOEX_RDATA)


def _ltecoex_write(t, offset: int, value: int) -> None:
    """ltecoex_reg_write_88xx [SRC] common_88xx.c:3368 — wait ready, set data, issue write cmd."""
    for _ in range(_POLL):
        if t.read8(REG_LTECOEX_CTRL + 3) & (1 << 5):
            break
    else:
        raise RuntimeError("RTL8821CU: ltecoex write not ready")
    t.write32(REG_LTECOEX_WDATA, value)
    t.write32(REG_LTECOEX_CTRL, 0xC00F0000 | offset)


def _wlan_cpu_en(t, enable: bool) -> None:
    """wlan_cpu_en_88xx [SRC] common_88xx.c:355 — gate the WL CPU IO + clock. Enable toggles
    RSV_CTRL+1 BIT0 then SYS_FUNC_EN+1 BIT2; disable does the reverse order."""
    if enable:
        t.write8(REG_RSV_CTRL + 1, t.read8(REG_RSV_CTRL + 1) | (1 << 0))
        t.write8(REG_SYS_FUNC_EN + 1, t.read8(REG_SYS_FUNC_EN + 1) | (1 << 2))
    else:
        t.write8(REG_SYS_FUNC_EN + 1, t.read8(REG_SYS_FUNC_EN + 1) & ~(1 << 2))
        t.write8(REG_RSV_CTRL + 1, t.read8(REG_RSV_CTRL + 1) & ~(1 << 0))


# --- the rsvd-page TX packet (txdesc + chunk) -------------------------------
def _build_txdesc_pkt(chunk: bytes, qsel: int = _QSEL_BEACON, set_offset: bool = True) -> bytes:
    """usb_write_data [SRC] rtl8821cu_halmac.c:142-182: a zeroed 48-byte TX desc + the chunk with
    the HW XOR checksum. The BEACON (rsvd-page) path sets OFFSET and the +8 PACKET_OFFSET arm
    (which fires only when (48+size) aligns to the USB bulk size — not on this card's chunks); the
    H2C_CMD path sets only TXPKTSIZE + QSEL."""
    size = len(chunk)
    extra = _PACKET_OFFSET_SZ if (set_offset and (_TX_DESC_SIZE + size) % _USB_BULK_OUT_SIZE == 0) else 0
    buf = bytearray(_TX_DESC_SIZE + extra + size)
    buf[_TX_DESC_SIZE + extra:] = chunk
    _set_field(buf, 0x00, 0, 16, size)                  # TXPKTSIZE
    if set_offset:
        _set_field(buf, 0x00, 16, 8, _TX_DESC_SIZE + extra)  # OFFSET
        if extra:
            _set_field(buf, 0x04, 24, 5, 1)             # PKT_OFFSET
    _set_field(buf, 0x04, 8, 5, qsel)                   # QSEL
    # fill_txdesc_check_sum_8821c: XOR the 16 LE halfwords of the first 32 B into TXDESC_CHECKSUM.
    _set_field(buf, 0x1C, 0, 16, 0)
    chksum = 0
    for i in range(16):
        chksum ^= int.from_bytes(buf[2 * i:2 * i + 2], "little")
    _set_field(buf, 0x1C, 0, 16, chksum & 0xFFFF)
    return bytes(buf)


def _send_rsvd_page(t, chunk: bytes, full: bool = False, rsvd_boundary: int = 0) -> None:
    """dl_rsvd_page_88xx [SRC] common_88xx.c:314 (+ send_fwpkt's USB dummy byte, fw_88xx.c:726):
    open the rsvd-page window, bulk the txdesc+chunk, poll bcn-valid, restore CR/TXQ/window.

    ``full`` selects the descriptor: cold init (not_xmitframe_fw_dl=1) takes the minimal
    ``usb_write_data_not_xmitframe`` path; the real hal_init (airmon) leaves the flag 0 so the
    chunk goes through ``dump_mgntframe`` -> the full ``update_txdesc`` builder [SRC]
    rtl8821cu_xmit.c:35. ``rsvd_boundary`` is the window restored at the end (``txff_alloc
    .rsvd_boundary``): 0 on the cold FW download (it runs before the first init_mac_flow), and
    the MAC reserved-page boundary afterwards (it persists in adapter state into the airmon dl)."""
    if (len(chunk) + _TX_DESC_SIZE) % _USB_BULK_OUT_SIZE == 0:
        chunk = chunk + b"\x00"             # send_fwpkt dummy (untested here — never aligns)
    t.write16(REG_FIFOPAGE_CTRL_2, 0 | (1 << 15))       # pg_addr 0 (src fixed at 0)
    r_cr = t.read8(REG_CR + 1)
    t.write8(REG_CR + 1, r_cr | (1 << 0))
    r_txq = t.read8(REG_FWHW_TXQ_CTRL + 2)
    t.write8(REG_FWHW_TXQ_CTRL + 2, r_txq & ~(1 << 6))
    pkt = tx.build_mgnt_txdesc(chunk, qsel=_QSEL_BEACON) if full else _build_txdesc_pkt(chunk)
    t.bulk_out(pkt)
    for _ in range(1000):
        if t.read8(REG_FIFOPAGE_CTRL_2 + 1) & (1 << 7):
            break
    else:
        raise RuntimeError("RTL8821CU: rsvd-page bcn-valid poll timed out")
    t.write16(REG_FIFOPAGE_CTRL_2, rsvd_boundary | (1 << 15))
    t.write8(REG_FWHW_TXQ_CTRL + 2, r_txq)
    t.write8(REG_CR + 1, r_cr)


def _iddma_dlfw(t, src: int, dest: int, length: int, first: bool) -> None:
    """iddma_dlfw_88xx + iddma_en_88xx [SRC] fw_88xx.c:754/:784 — wait CH0 idle, then program
    SA/DA/CTRL (OWN | CHKSUM_EN | len, + CHKSUM_CONT after the first chunk) and poll OWN clear."""
    for _ in range(_POLL):
        if not (t.read32(REG_DDMA_CH0CTRL) & _BIT_DDMACH0_OWN):
            break
    else:
        raise RuntimeError("RTL8821CU: iDDMA CH0 not ready")
    ctrl = _BIT_DDMACH0_CHKSUM_EN | _BIT_DDMACH0_OWN | (length & _DDMACH0_DLEN_MASK)
    if not first:
        ctrl |= _BIT_DDMACH0_CHKSUM_CONT
    t.write32(REG_DDMA_CH0SA, src)
    t.write32(REG_DDMA_CH0DA, dest)
    t.write32(REG_DDMA_CH0CTRL, ctrl)
    for _ in range(_POLL):
        if not (t.read32(REG_DDMA_CH0CTRL) & _BIT_DDMACH0_OWN):
            break
    else:
        raise RuntimeError("RTL8821CU: iDDMA CH0 transfer timed out")


def _check_fw_chksum(t, mem_addr: int) -> None:
    """check_fw_chksum_88xx [SRC] fw_88xx.c:802 — mark IMEM/DMEM download+checksum OK in
    REG_MCUFW_CTRL (DMEM bits when the dest is in DMEM space, else IMEM bits)."""
    fw_ctrl = t.read8(REG_MCUFW_CTRL)
    failed = bool(t.read32(REG_DDMA_CH0CTRL) & _BIT_DDMACH0_CHKSUM_STS)
    if mem_addr < _OCPBASE_DMEM:
        ok = _BIT_IMEM_DW_OK if failed else (_BIT_IMEM_DW_OK | _BIT_IMEM_CHKSUM_OK)
        fw_ctrl = (fw_ctrl & ~_BIT_IMEM_CHKSUM_OK) | ok if failed else fw_ctrl | ok
    else:
        ok = _BIT_DMEM_DW_OK if failed else (_BIT_DMEM_DW_OK | _BIT_DMEM_CHKSUM_OK)
        fw_ctrl = (fw_ctrl & ~_BIT_DMEM_CHKSUM_OK) | ok if failed else fw_ctrl | ok
    t.write8(REG_MCUFW_CTRL, fw_ctrl & 0xFF)
    if failed:
        raise RuntimeError(f"RTL8821CU: FW checksum failed for mem 0x{mem_addr:08x}")


def _dlfw_to_mem(t, data: bytes, dest: int, size: int, full: bool = False,
                 rsvd_boundary: int = 0) -> None:
    """dlfw_to_mem_88xx [SRC] fw_88xx.c:567 — reset the rolling checksum, then chunk the section:
    send each chunk to TXBUF and iDDMA it to dest+offset; finally verify the checksum."""
    t.write32(REG_DDMA_CH0CTRL, t.read32(REG_DDMA_CH0CTRL) | _BIT_DDMACH0_RESET_CHKSUM_STS)
    offset = 0
    first = True
    while offset < size:
        pkt = min(size - offset, _DLFW_PKT_SIZE)
        _send_rsvd_page(t, data[offset:offset + pkt], full, rsvd_boundary)
        _iddma_dlfw(t, _OCPBASE_TXBUF + _TX_DESC_SIZE, dest + offset, pkt, first)
        first = False
        offset += pkt
    _check_fw_chksum(t, dest)


def download_firmware(t, info, full: bool = False, rsvd_boundary: int = 0) -> None:
    """download_firmware_88xx [SRC] fw_88xx.c:115 — back up LTE-coex + the MAC regs the iDDMA
    download borrows, push DMEM/IMEM/EMEM, restore, then end-flow (FW-ready + CPU enable)."""
    fw = _FW_BIN.read_bytes()
    dmem_size = _le32(fw, _HDR_DMEM_SIZE) + _CHKSUM_SIZE
    imem_size = _le32(fw, _HDR_IMEM_SIZE) + _CHKSUM_SIZE
    emem_size = (_le32(fw, _HDR_EMEM_SIZE) + _CHKSUM_SIZE) if (fw[_HDR_MEM_USAGE] & (1 << 4)) else 0
    dmem_dest = _le32(fw, _HDR_DMEM_ADDR) & ~(1 << 31)
    imem_dest = _le32(fw, _HDR_IMEM_ADDR) & ~(1 << 31)
    emem_dest = _le32(fw, _HDR_EMEM_ADDR) & ~(1 << 31)

    lte_backup = _ltecoex_read(t, 0x38)
    _wlan_cpu_en(t, False)

    # Back up + repurpose the MAC regs the HIQ/iDDMA download path needs.
    b_pq = t.read8(REG_TXDMA_PQ_MAP + 1)
    t.write8(REG_TXDMA_PQ_MAP + 1, _DMA_MAPPING_HIGH << 6)
    b_cr = t.read8(REG_CR)
    t.write8(REG_CR, _BIT_HCI_TXDMA_EN | _BIT_TXDMA_EN)
    t.write32(REG_H2CQ_CSR, 1 << 31)
    b_fifo = t.read16(REG_FIFOPAGE_INFO_1)
    b_rqpn = t.read32(REG_RQPN_CTRL_2) | (1 << 31)
    t.write16(REG_FIFOPAGE_INFO_1, 0x200)
    t.write32(REG_RQPN_CTRL_2, b_rqpn)
    b_bcn = t.read8(REG_BCN_CTRL)
    t.write8(REG_BCN_CTRL, (b_bcn & ~(1 << 3)) | (1 << 4))

    # pltfm_reset_88xx: toggle the WL-CPU reset + clock-sync bits.
    t.write8(REG_CPU_DMEM_CON + 2, t.read8(REG_CPU_DMEM_CON + 2) & ~(1 << 0))
    t.write8(REG_SYS_CLK_CTRL + 1, t.read8(REG_SYS_CLK_CTRL + 1) & ~(1 << 6))
    t.write8(REG_CPU_DMEM_CON + 2, t.read8(REG_CPU_DMEM_CON + 2) | (1 << 0))
    t.write8(REG_SYS_CLK_CTRL + 1, t.read8(REG_SYS_CLK_CTRL + 1) | (1 << 6))

    # start_dlfw_88xx: enable FW-download, push the sections.
    t.write16(REG_MCUFW_CTRL, (t.read16(REG_MCUFW_CTRL) & 0x3800) | (1 << 0))
    base = _HDR_SIZE
    _dlfw_to_mem(t, fw[base:base + dmem_size], dmem_dest, dmem_size, full, rsvd_boundary)
    base += dmem_size
    _dlfw_to_mem(t, fw[base:base + imem_size], imem_dest, imem_size, full, rsvd_boundary)
    base += imem_size
    if emem_size:
        _dlfw_to_mem(t, fw[base:base + emem_size], emem_dest, emem_size, full, rsvd_boundary)

    # restore_mac_reg_88xx (in backup order).
    t.write8(REG_TXDMA_PQ_MAP + 1, b_pq)
    t.write8(REG_CR, b_cr)
    t.write32(REG_H2CQ_CSR, 1 << 31)
    t.write16(REG_FIFOPAGE_INFO_1, b_fifo)
    t.write32(REG_RQPN_CTRL_2, b_rqpn)
    t.write8(REG_BCN_CTRL, b_bcn)

    # dlfw_end_flow_88xx: confirm IMEM+DMEM checksums, set FW-ready, boot the CPU, wait ready.
    t.write32(REG_TXDMA_STATUS, 1 << 2)
    fw_ctrl = t.read16(REG_MCUFW_CTRL)
    if (fw_ctrl & 0x50) != 0x50:
        raise RuntimeError("RTL8821CU: IMEM/DMEM checksum not OK after download")
    t.write16(REG_MCUFW_CTRL, (fw_ctrl | _BIT_FW_DW_RDY) & ~(1 << 0))
    _wlan_cpu_en(t, True)
    for _ in range(5000):
        if t.read16(REG_MCUFW_CTRL) == _FW_READY:
            break
    else:
        raise RuntimeError("RTL8821CU: FW not ready (0x80 != 0xC078)")

    _ltecoex_write(t, 0x38, lte_backup)


def download_fw(t, info, full: bool = False, rsvd_boundary: int = 0) -> None:
    """[SRC] download_fw hal_halmac.c:3350 — drain TX FIFO, then download firmware."""
    _txfifo_wait_empty(t)
    download_firmware(t, info, full, rsvd_boundary)


def _h2c_header(buf: bytearray, sub_cmd: int, content_size: int, seq: int) -> None:
    """set_h2c_pkt_hdr_88xx [SRC] common_88xx.c:614 — the 8-byte FW-offload H2C header
    (category 0x01, cmd 0xFF, sub-cmd, total len, seq)."""
    _set_field(buf, 0x00, 0, 7, 0x01)
    _set_field(buf, 0x00, 8, 8, 0xFF)
    _set_field(buf, 0x00, 16, 16, sub_cmd)
    _set_field(buf, 0x04, 0, 16, 8 + content_size)
    _set_field(buf, 0x04, 16, 16, seq)


def _send_h2c_pkt(t, h2c: bytes) -> None:
    """send_h2c_pkt_88xx -> PLTFM_SEND_H2C_PKT (usb_write_data H2C_CMD): wrap the 32-byte H2C in a
    TX desc (QSEL=H2C, no OFFSET) and bulk it out. [SRC] common_88xx.c:640 / rtl8821cu_halmac.c:165.
    The free-space guard never fires (buf_fs starts at the full h2cq size), so this is one bulk."""
    t.bulk_out(_build_txdesc_pkt(h2c, qsel=_QSEL_H2C, set_offset=False))


def _h2c_dump_poll(t) -> None:
    """send_general_info_88xx h2cq verify [SRC] fw_88xx.c:1086 -> dump_fifo_88xx: read the queued
    H2C back from the TX FIFO (page-select via PKTBUF_DBG_CTRL, RX clock gated) and confirm it."""
    h2cq_addr = mac.txff_pages()["h2cq_addr"] << 7
    tmp8 = t.read8(REG_RCR + 2)
    t.write8(REG_RCR + 2, t.read8(REG_RCR + 2) | (1 << 3))      # rx_clk_gate(enable=0)
    start_pg = (h2cq_addr >> 12) + 0x780
    residue = h2cq_addr & 0xFFF
    value32 = t.read16(REG_PKTBUF_DBG_CTRL) & 0xF000
    t.write16(REG_PKTBUF_DBG_CTRL, (start_pg | value32) & 0xFFFF)
    t.read32(0x8000 + residue)                                 # h2cq_ele == {0x01,0xFF}
    t.write16(REG_PKTBUF_DBG_CTRL, value32 & 0xFFFF)
    t.write8(REG_RCR + 2, tmp8)


def _send_general_info_by_reg(t, info) -> None:
    """_send_general_info_by_reg [SRC] hal_halmac.c:3035 -> rtw_halmac_send_h2c HMEBOX box0:
    an 8-byte general-info-reg H2C (drv rf_type 0 for 1T1R, cut = chip_ver) written to the box."""
    h2c = bytearray(8)
    _set_field(h2c, 0, 0, 5, 0x0C)                  # CMD_ID_GENERAL_INFO_REG
    _set_field(h2c, 0, 5, 3, 0x02)                  # CLASS_GENERAL_INFO_REG
    _set_field(h2c, 0, 8, 8, info.rfe_type)
    _set_field(h2c, 0, 16, 8, 0)                    # RF_TYPE drv (RF_1T1R == 0)
    _set_field(h2c, 0, 24, 8, info.chip_ver)        # CUT_VERSION (phydm)
    _set_field(h2c, 4, 0, 4, _BB_PATH_A)            # RX_ANT_STATUS
    _set_field(h2c, 4, 4, 4, _BB_PATH_A)            # TX_ANT_STATUS
    if t.read8(REG_HMETFR) & (1 << 0):              # _is_fw_read_cmd_down(box0)
        raise RuntimeError("RTL8821CU: H2C box0 not free")
    t.write32(REG_HMEBOX_E0, int.from_bytes(h2c[4:8], "little"))
    t.write32(REG_HMEBOX0, int.from_bytes(h2c[0:4], "little"))


def send_general_info(t, info) -> None:
    """_send_general_info [SRC] hal_halmac.c:3073 — two FW-offload H2C info packets (general-info
    FW_TX_BOUNDARY + phydm rfe/rf/cut/ant), verified via the h2cq dump, then the by-reg copy."""
    gen = bytearray(_H2C_PKT_SIZE)
    _set_field(gen, 0x08, 16, 8, mac.txff_pages()["fw_tx_boundary"])  # FW_TX_BOUNDARY
    _h2c_header(gen, _SUB_CMD_GENERAL_INFO, 4, 0)
    _send_h2c_pkt(t, bytes(gen))

    phy = bytearray(_H2C_PKT_SIZE)
    _set_field(phy, 0x08, 0, 8, info.rfe_type)      # REF_TYPE
    _set_field(phy, 0x08, 8, 8, _HALMAC_RF_1T1R)    # RF_TYPE (halmac)
    _set_field(phy, 0x08, 16, 8, info.chip_ver)     # CUT_VER
    _set_field(phy, 0x08, 24, 4, _BB_PATH_A)        # RX_ANT_STATUS
    _set_field(phy, 0x08, 28, 4, _BB_PATH_A)        # TX_ANT_STATUS
    _set_field(phy, 0x0C, 0, 8, 0)                  # EXT_PA (driver hardcodes 0)
    _set_field(phy, 0x0C, 8, 8, info.package_type)  # PACKAGE_TYPE (0 until MAC-hidden rpt read)
    _set_field(phy, 0x0C, 16, 1, 0)                 # MP_MODE (mp_mode=0)
    _h2c_header(phy, _SUB_CMD_PHYDM_INFO, 8, 1)
    _send_h2c_pkt(t, bytes(phy))

    _h2c_dump_poll(t)
    _send_general_info_by_reg(t, info)


def fw_dl(t, info, already_on: bool, power_on_fn) -> None:
    """[SRC] rtw_halmac_dlfw hal_halmac.c:3964 — power on (idempotent), download_fw, init MAC,
    send the FW general/phydm info. HW init is not yet complete during the MAC-hidden readback,
    so the full path runs. ``power_on_fn`` is passed in to avoid a bringup<->firmware cycle."""
    power_on_fn(t, info, already_on=already_on)
    download_fw(t, info)
    mac.init_mac_flow(t, info)
    send_general_info(t, info)
