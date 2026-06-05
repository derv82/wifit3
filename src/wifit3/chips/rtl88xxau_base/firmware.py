"""RTL88xxAU M1 bring-up mechanics: power-on -> LLT -> firmware download -> FW-ready.

Ported 1:1 from the vendor ``FirmwareDownload8812`` path (``hal/rtl8812a/
rtl8812a_hal_init.c``) and ``_InitPowerOn`` (``hal/rtl8812a/usb/usb_halinit.c``). The
88xxA streams the FW over EP0 control writes (196/8/1-byte chunks to FW_START_ADDRESS),
page bank in REG_MCUFWDL+2[2:0] — it does NOT use a bulk-OUT / IDDMA download.

These mechanics are family-shared; the three chip-varying inputs are injected by the
caller: the power-on **pwrseq flow** (+ its cut/fab/intf masks), the **TX page boundary**
(LLT split point), and the **firmware blob**. The on-wire order:

  card_enable_flow -> CR enable -> LLT init -> drop-incorrect-bulkout
  -> [FW] RAM-code pre-reset -> FWDL enable -> page writes -> chksum -> disable
  -> FW-ready (8051 reset + poll WINTINI_RDY).
"""
from __future__ import annotations

import time

from . import registers as R
from .pwrseq import PWR_CUT_A_MSK, PWR_FAB_ALL_MSK, PWR_INTF_USB_MSK, hal_pwr_seq_cmd_parsing


# --- power-on [SRC] _InitPowerOn usb_halinit.c:304-353 ---
def power_on(t, pwrseq_flow, cut=PWR_CUT_A_MSK, fab=PWR_FAB_ALL_MSK,
             intf=PWR_INTF_USB_MSK, ldo_quirk=False, delay=time.sleep) -> None:
    hal_pwr_seq_cmd_parsing(t, cut, fab, intf, pwrseq_flow, delay)
    # Enable MAC DMA/WMAC/SCHEDULE/SEC + 32k cal timer.
    t.write16(R.REG_CR, 0x0000)
    cr = t.read16(R.REG_CR)
    t.write16(R.REG_CR, cr | R.CR_ENABLE)
    # LDO quirk: if 0xF0[24] (REG_SYS_CFG+3 bit0) -> set 0x7C[6]. [SRC] _InitPowerOn:
    # gated on IS_HARDWARE_TYPE_8821U — the 8812AU does NOT run it.
    if ldo_quirk and (t.read8(R.REG_SYS_CFG + 3) & R.BIT0):
        t.write8(0x7C, t.read8(0x7C) | R.BIT6)


# --- LLT [SRC] InitLLTTable8812A / _LLTWrite_8812A rtl8812a_hal_init.c:29-118 ---
def _llt_write(t, address: int, data: int) -> None:
    value = R._LLT_INIT_ADDR(address) | R._LLT_INIT_DATA(data) | R._LLT_OP(R._LLT_WRITE_ACCESS)
    t.write32(R.REG_LLT_INIT, value)
    for _ in range(R.POLLING_LLT_THRESHOLD + 2):
        if R._LLT_OP_VALUE(t.read32(R.REG_LLT_INIT)) == R._LLT_NO_ACTIVE:
            return
    raise IOError(f"LLT write timeout @entry {address}")


def init_llt(t, txpktbuf_bndy: int) -> None:
    for i in range(txpktbuf_bndy - 1):
        _llt_write(t, i, i + 1)
    _llt_write(t, txpktbuf_bndy - 1, 0xFF)
    for i in range(txpktbuf_bndy, R.LAST_ENTRY_OF_TX_PKT_BUFFER_8812):
        _llt_write(t, i, i + 1)
    _llt_write(t, R.LAST_ENTRY_OF_TX_PKT_BUFFER_8812, txpktbuf_bndy)


def init_drop_incorrect_bulkout(t) -> None:
    # [SRC] _InitHardwareDropIncorrectBulkOut_8812A (ENABLE_USB_DROP_INCORRECT_OUT)
    v = t.read32(R.REG_TXDMA_OFFSET_CHK)
    t.write32(R.REG_TXDMA_OFFSET_CHK, v | R.DROP_DATA_EN)


# --- 8051 reset [SRC] _8051Reset8812 rtl8812a_hal_init.c:374-410 ---
def _8051_reset(t) -> None:
    t.write8(R.REG_RSV_CTRL, t.read8(R.REG_RSV_CTRL) & ~R.BIT1)
    t.write8(R.REG_RSV_CTRL + 1, t.read8(R.REG_RSV_CTRL + 1) & ~R.BIT0)
    sysfn = t.read8(R.REG_SYS_FUNC_EN + 1)
    t.write8(R.REG_SYS_FUNC_EN + 1, sysfn & ~R.BIT2)
    t.write8(R.REG_RSV_CTRL, t.read8(R.REG_RSV_CTRL) & ~R.BIT1)
    t.write8(R.REG_RSV_CTRL + 1, t.read8(R.REG_RSV_CTRL + 1) | R.BIT0)
    t.write8(R.REG_SYS_FUNC_EN + 1, sysfn | R.BIT2)


# --- firmware download [SRC] FirmwareDownload8812 rtl8812a_hal_init.c:146-668 ---
def _fw_download_enable(t, enable: bool) -> None:
    if enable:
        t.write8(R.REG_MCUFWDL, t.read8(R.REG_MCUFWDL) | 0x01)           # MCUFWDL_EN
        t.write8(R.REG_MCUFWDL + 2, t.read8(R.REG_MCUFWDL + 2) & 0xF7)   # clear 8051-reset hold
    else:
        t.write8(R.REG_MCUFWDL, t.read8(R.REG_MCUFWDL) & 0xFE)


def _block_write(t, buf: bytes) -> None:
    # USB phases: 196-byte, then 8-byte remainder, then 1-byte remainder.
    p1, p2 = 196, 8
    n1, rem1 = divmod(len(buf), p1)
    off = 0
    for _ in range(n1):
        t.writeN(R.FW_START_ADDRESS + off, buf[off:off + p1])
        off += p1
    if rem1:
        n2, rem2 = divmod(rem1, p2)
        for _ in range(n2):
            t.writeN(R.FW_START_ADDRESS + off, buf[off:off + p2])
            off += p2
        for _ in range(rem2):
            t.write8(R.FW_START_ADDRESS + off, buf[off])
            off += 1


def _page_write(t, page: int, buf: bytes) -> None:
    v = (t.read8(R.REG_MCUFWDL + 2) & 0xF8) | (page & 0x07)
    t.write8(R.REG_MCUFWDL + 2, v)
    _block_write(t, buf)


def _write_fw(t, body: bytes) -> None:
    page_nums, remain = divmod(len(body), R.MAX_DLFW_PAGE_SIZE)
    for page in range(page_nums):
        off = page * R.MAX_DLFW_PAGE_SIZE
        _page_write(t, page, body[off:off + R.MAX_DLFW_PAGE_SIZE])
    if remain:
        off = page_nums * R.MAX_DLFW_PAGE_SIZE
        _page_write(t, page_nums, body[off:off + remain])


def _polling_fwdl_chksum(t, min_cnt: int, timeout_ms: int, delay=time.sleep) -> bool:
    start = time.perf_counter()
    cnt = 0
    while True:
        cnt += 1
        if t.read32(R.REG_MCUFWDL) & R.FWDL_ChkSum_rpt:
            return True
        delay(0)
        if (time.perf_counter() - start) * 1000 >= timeout_ms and cnt >= min_cnt:
            return False


def _fw_free_to_go(t, min_cnt: int, timeout_ms: int, delay=time.sleep) -> bool:
    v = t.read32(R.REG_MCUFWDL)
    v = ((v | R.MCUFWDL_RDY) & ~R.WINTINI_RDY) & 0xFFFFFFFF
    t.write32(R.REG_MCUFWDL, v)
    _8051_reset(t)
    start = time.perf_counter()
    cnt = 0
    while True:
        cnt += 1
        if t.read32(R.REG_MCUFWDL) & R.WINTINI_RDY:
            return True
        delay(0)
        if (time.perf_counter() - start) * 1000 >= timeout_ms and cnt >= min_cnt:
            return False


def download_firmware(t, fw_blob: bytes, delay=time.sleep) -> bool:
    """FW-source-header path: strip 32-byte header, page-write the body, ack."""
    body = fw_blob[R.FW_HEADER_SIZE:]   # IS_FW_HEADER_EXIST -> shift 32
    # If 8051 is already running RAM code, tell it to reset first.
    if t.read8(R.REG_MCUFWDL) & R.RAM_DL_SEL:
        t.write8(R.REG_MCUFWDL, 0x00)
        _8051_reset(t)
    _fw_download_enable(t, True)
    ok = False
    for _ in range(3):
        t.write8(R.REG_MCUFWDL, t.read8(R.REG_MCUFWDL) | R.FWDL_ChkSum_rpt)
        _write_fw(t, body)
        if _polling_fwdl_chksum(t, 5, 50, delay):
            ok = True
            break
    _fw_download_enable(t, False)
    ready = _fw_free_to_go(t, 10, 200, delay) if ok else False
    # InitializeFirmwareVars8812 runs at FirmwareDownload8812 exit; its only chip
    # write is the H2C-mailbox trigger. [SRC] rtl8812a_hal_init.c:680
    t.write8(R.REG_HMETFR, 0x0F)
    return ready


def bring_up(t, fw_blob: bytes, pwrseq_flow, txpktbuf_bndy: int,
             cut=PWR_CUT_A_MSK, fab=PWR_FAB_ALL_MSK, intf=PWR_INTF_USB_MSK,
             ldo_quirk=False, delay=time.sleep) -> bool:
    """Full M1: power-on -> LLT -> drop-bulkout -> FW download -> FW-ready.
    Returns True once WINTINI_RDY is set (wlan CPU running the firmware)."""
    power_on(t, pwrseq_flow, cut, fab, intf, ldo_quirk, delay)
    init_llt(t, txpktbuf_bndy)
    init_drop_incorrect_bulkout(t)
    return download_firmware(t, fw_blob, delay)
