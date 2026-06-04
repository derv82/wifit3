"""RTL8821AU (DKMS) M1 bring-up: power-on -> LLT -> firmware download -> FW-ready.

Ported 1:1 from the vendor `FirmwareDownload8812` path (`hal/rtl8812a/
rtl8812a_hal_init.c`) and `_InitPowerOn_8812AU` (`hal/rtl8812a/usb/usb_halinit.c`),
8821a branch. The 8821a streams the FW over EP0 control writes (196/8/1-byte
chunks to FW_START_ADDRESS), page bank in REG_MCUFWDL+2[2:0] — it does NOT use a
bulk-OUT / IDDMA download.

The on-wire order (cold-boot capture, frames ~2543-4351):
  card_enable_flow -> CR enable -> LLT init -> drop-incorrect-bulkout
  -> [FW] RAM-code pre-reset -> FWDL enable -> page writes -> chksum -> disable
  -> FW-ready (8051 reset + poll WINTINI_RDY).
"""
from __future__ import annotations

import time
from pathlib import Path

from . import constants as C
from .pwrseq import (
    CARD_ENABLE_FLOW,
    PWR_CUT_A_MSK,
    PWR_FAB_ALL_MSK,
    PWR_INTF_USB_MSK,
    hal_pwr_seq_cmd_parsing,
)

FW_BIN = Path(__file__).parent / "assets" / "rtl8821au_fw.bin"


def load_firmware_blob() -> bytes:
    """The 8821a NIC firmware (array_mp_8821a_fw_nic), header included."""
    return FW_BIN.read_bytes()


# --- power-on [SRC] _InitPowerOn_8812AU usb_halinit.c:304-353 ---
def power_on(t, delay=time.sleep) -> None:
    # 8821A MP chip on USB -> card_enable_flow, cut=A / fab=ALL / intf=USB.
    hal_pwr_seq_cmd_parsing(t, PWR_CUT_A_MSK, PWR_FAB_ALL_MSK, PWR_INTF_USB_MSK,
                            CARD_ENABLE_FLOW, delay)
    # Enable MAC DMA/WMAC/SCHEDULE/SEC + 32k cal timer.
    t.write16(C.REG_CR, 0x0000)
    cr = t.read16(C.REG_CR)
    t.write16(C.REG_CR, cr | C.CR_ENABLE)
    # 8821U LDO quirk: if 0xF0[24] (REG_SYS_CFG+3 bit0) -> set 0x7C[6].
    if t.read8(C.REG_SYS_CFG + 3) & C.BIT0:
        t.write8(0x7C, t.read8(0x7C) | C.BIT6)


# --- LLT [SRC] InitLLTTable8812A / _LLTWrite_8812A rtl8812a_hal_init.c:29-118 ---
def _llt_write(t, address: int, data: int) -> None:
    value = C._LLT_INIT_ADDR(address) | C._LLT_INIT_DATA(data) | C._LLT_OP(C._LLT_WRITE_ACCESS)
    t.write32(C.REG_LLT_INIT, value)
    for _ in range(C.POLLING_LLT_THRESHOLD + 2):
        if C._LLT_OP_VALUE(t.read32(C.REG_LLT_INIT)) == C._LLT_NO_ACTIVE:
            return
    raise IOError(f"LLT write timeout @entry {address}")


def init_llt(t, txpktbuf_bndy: int = C.TX_PAGE_BOUNDARY_8821) -> None:
    for i in range(txpktbuf_bndy - 1):
        _llt_write(t, i, i + 1)
    _llt_write(t, txpktbuf_bndy - 1, 0xFF)
    for i in range(txpktbuf_bndy, C.LAST_ENTRY_OF_TX_PKT_BUFFER_8812):
        _llt_write(t, i, i + 1)
    _llt_write(t, C.LAST_ENTRY_OF_TX_PKT_BUFFER_8812, txpktbuf_bndy)


def init_drop_incorrect_bulkout(t) -> None:
    # [SRC] _InitHardwareDropIncorrectBulkOut_8812A (ENABLE_USB_DROP_INCORRECT_OUT)
    v = t.read32(C.REG_TXDMA_OFFSET_CHK)
    t.write32(C.REG_TXDMA_OFFSET_CHK, v | C.DROP_DATA_EN)


# --- 8051 reset (8821 branch) [SRC] _8051Reset8812 rtl8812a_hal_init.c:374-410 ---
def _8051_reset(t) -> None:
    t.write8(C.REG_RSV_CTRL, t.read8(C.REG_RSV_CTRL) & ~C.BIT1)
    t.write8(C.REG_RSV_CTRL + 1, t.read8(C.REG_RSV_CTRL + 1) & ~C.BIT0)
    sysfn = t.read8(C.REG_SYS_FUNC_EN + 1)
    t.write8(C.REG_SYS_FUNC_EN + 1, sysfn & ~C.BIT2)
    t.write8(C.REG_RSV_CTRL, t.read8(C.REG_RSV_CTRL) & ~C.BIT1)
    t.write8(C.REG_RSV_CTRL + 1, t.read8(C.REG_RSV_CTRL + 1) | C.BIT0)
    t.write8(C.REG_SYS_FUNC_EN + 1, sysfn | C.BIT2)


# --- firmware download [SRC] FirmwareDownload8812 rtl8812a_hal_init.c:146-668 ---
def _fw_download_enable(t, enable: bool) -> None:
    if enable:
        t.write8(C.REG_MCUFWDL, t.read8(C.REG_MCUFWDL) | 0x01)           # MCUFWDL_EN
        t.write8(C.REG_MCUFWDL + 2, t.read8(C.REG_MCUFWDL + 2) & 0xF7)   # clear 8051-reset hold
    else:
        t.write8(C.REG_MCUFWDL, t.read8(C.REG_MCUFWDL) & 0xFE)


def _block_write(t, buf: bytes) -> None:
    # USB phases: 196-byte, then 8-byte remainder, then 1-byte remainder.
    p1, p2 = 196, 8
    n1, rem1 = divmod(len(buf), p1)
    off = 0
    for _ in range(n1):
        t.writeN(C.FW_START_ADDRESS + off, buf[off:off + p1])
        off += p1
    if rem1:
        n2, rem2 = divmod(rem1, p2)
        for _ in range(n2):
            t.writeN(C.FW_START_ADDRESS + off, buf[off:off + p2])
            off += p2
        for _ in range(rem2):
            t.write8(C.FW_START_ADDRESS + off, buf[off])
            off += 1


def _page_write(t, page: int, buf: bytes) -> None:
    v = (t.read8(C.REG_MCUFWDL + 2) & 0xF8) | (page & 0x07)
    t.write8(C.REG_MCUFWDL + 2, v)
    _block_write(t, buf)


def _write_fw(t, body: bytes) -> None:
    page_nums, remain = divmod(len(body), C.MAX_DLFW_PAGE_SIZE)
    for page in range(page_nums):
        off = page * C.MAX_DLFW_PAGE_SIZE
        _page_write(t, page, body[off:off + C.MAX_DLFW_PAGE_SIZE])
    if remain:
        off = page_nums * C.MAX_DLFW_PAGE_SIZE
        _page_write(t, page_nums, body[off:off + remain])


def _polling_fwdl_chksum(t, min_cnt: int, timeout_ms: int, delay=time.sleep) -> bool:
    start = time.perf_counter()
    cnt = 0
    while True:
        cnt += 1
        if t.read32(C.REG_MCUFWDL) & C.FWDL_ChkSum_rpt:
            return True
        delay(0)
        if (time.perf_counter() - start) * 1000 >= timeout_ms and cnt >= min_cnt:
            return False


def _fw_free_to_go(t, min_cnt: int, timeout_ms: int, delay=time.sleep) -> bool:
    v = t.read32(C.REG_MCUFWDL)
    v = ((v | C.MCUFWDL_RDY) & ~C.WINTINI_RDY) & 0xFFFFFFFF
    t.write32(C.REG_MCUFWDL, v)
    _8051_reset(t)
    start = time.perf_counter()
    cnt = 0
    while True:
        cnt += 1
        if t.read32(C.REG_MCUFWDL) & C.WINTINI_RDY:
            return True
        delay(0)
        if (time.perf_counter() - start) * 1000 >= timeout_ms and cnt >= min_cnt:
            return False


def download_firmware(t, fw_blob: bytes, delay=time.sleep) -> bool:
    """FW-source-header path: strip 32-byte header, page-write the body, ack."""
    body = fw_blob[C.FW_HEADER_SIZE:]   # IS_FW_HEADER_EXIST_8821 -> shift 32
    # If 8051 is already running RAM code, tell it to reset first.
    if t.read8(C.REG_MCUFWDL) & C.RAM_DL_SEL:
        t.write8(C.REG_MCUFWDL, 0x00)
        _8051_reset(t)
    _fw_download_enable(t, True)
    ok = False
    for _ in range(3):
        t.write8(C.REG_MCUFWDL, t.read8(C.REG_MCUFWDL) | C.FWDL_ChkSum_rpt)
        _write_fw(t, body)
        if _polling_fwdl_chksum(t, 5, 50, delay):
            ok = True
            break
    _fw_download_enable(t, False)
    ready = _fw_free_to_go(t, 10, 200, delay) if ok else False
    # InitializeFirmwareVars8812 runs at FirmwareDownload8812 exit; its only chip
    # write is the H2C-mailbox trigger. [SRC] rtl8812a_hal_init.c:680
    t.write8(C.REG_HMETFR, 0x0F)
    return ready


def bring_up(t, fw_blob: bytes, delay=time.sleep) -> bool:
    """Full M1: power-on -> LLT -> drop-bulkout -> FW download -> FW-ready.
    Returns True once WINTINI_RDY is set (wlan CPU running the firmware)."""
    power_on(t, delay)
    init_llt(t)
    init_drop_incorrect_bulkout(t)
    return download_firmware(t, fw_blob, delay)
