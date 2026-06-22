"""RTL8821CU MAC init flow — the HALMAC ``init_mac_cfg`` for 8821C, run after firmware
download (from ``rtw_halmac_dlfw`` -> ``init_mac_flow``).

This sets up TX DMA queue mapping, the TX/RX FIFO page allocation (reserved-page boundaries),
the H2C queue, and the protocol / EDCA / WMAC register blocks — i.e. everything that turns the
powered + FW-loaded chip into a working MAC. Page boundaries are computed from the chip's fixed
FIFO sizes (not hardcoded), so the math stays correct if the reserved-page count changes.

Ported from [SRC] hal/halmac/halmac_88xx/halmac_8821c/halmac_init_8821c.c:
  init_trx_cfg :422 / txdma_queue_mapping :479 / priority_queue_cfg :523 / set_trx_fifo_info :648
  init_h2c :820 / init_protocol_cfg :766 / init_edca_cfg :878 / init_wmac_cfg :924
and the USB RX-aggregation cfg [SRC] hal/halmac/halmac_88xx/halmac_usb_88xx.c:87 cfg_usb_rx_agg.
The bulk-OUT count selects the RQPN / page-num tables; an 8821CU enumerates 3 OUT pipes.
"""
from __future__ import annotations

# --- registers [SRC] halmac_reg2.h -----------------------------------------
REG_CR = 0x0100
REG_TXDMA_PQ_MAP = 0x010C
REG_RXFF_BNDY = 0x011C
REG_C2HEVT_MSG_NORMAL = 0x01A0
REG_FIFOPAGE_CTRL_2 = 0x0204
REG_AUTO_LLT_V1 = 0x0208
REG_TXDMA_OFFSET_CHK = 0x020C
REG_RQPN_CTRL_2 = 0x022C
REG_FIFOPAGE_INFO_1 = 0x0230
REG_H2C_HEAD = 0x0244
REG_H2C_TAIL = 0x0248
REG_H2C_READ_ADDR = 0x024C
REG_H2C_INFO = 0x0254
REG_H2C_PKT_READADDR = 0x10D0
REG_H2C_PKT_WRITEADDR = 0x10D4
REG_RXDMA_AGG_PG_TH = 0x0280
REG_FWFF_CTRL = 0x029C
REG_FWFF_PKT_INFO = 0x02A0
REG_FWHW_TXQ_CTRL = 0x0420
REG_BCNQ_BDNY_V1 = 0x0424
REG_BCNQ1_BDNY_V1 = 0x0456
REG_AMPDU_MAX_TIME_V1 = 0x0455
REG_TX_HANG_CTRL = 0x045E
REG_INIRTS_RATE_SEL = 0x0480
REG_PROT_MODE_CTRL = 0x04C8
REG_BAR_MODE_CTRL = 0x04CC
REG_PRECNT_CTRL = 0x04E5
REG_EDCA_VO_PARAM = 0x0500
REG_EDCA_VI_PARAM = 0x0504
REG_PIFS = 0x0512
REG_SIFS = 0x0514
REG_SLOT = 0x051B
REG_TX_PTCL_CTRL = 0x0520
REG_TXPAUSE = 0x0522
REG_TBTT_PROHIBIT = 0x0540
REG_RD_NAV_NXT = 0x0544
REG_BCN_CTRL = 0x0550
REG_DRVERLYINT = 0x0558
REG_BCNDMATIM = 0x0559
REG_RXTSF_OFFSET_CCK = 0x055E
REG_TIMER0_SRC_SEL = 0x05B4
REG_WMAC_FWPKT_CR = 0x0601
REG_TCR = 0x0604
REG_RCR = 0x0608
REG_RX_PKT_LIMIT = 0x060C
REG_ACKTO_CCK = 0x0639
REG_WMAC_TRXPTCL_CTL_H = 0x066C
REG_RXFLTMAP0 = 0x06A0
REG_RXFLTMAP2 = 0x06A4
REG_SND_PTCL_CTRL = 0x0718
REG_WMAC_OPTION_FUNCTION = 0x07D0
REG_WMAC_OPTION_FUNCTION_2 = 0x07D8
REG_SYS_CFG2 = 0x00FC
REG_H2CQ_CSR = 0x1330
REG_FAST_EDCA_VOVI_SETTING = 0x1448
REG_FAST_EDCA_BEBK_SETTING = 0x144C

# --- FIFO page allocation [SRC] init_8821c.c:648 + halmac_8821c_cfg.h ------
_TX_FIFO_SIZE = 65536
_RX_FIFO_SIZE = 16384
_TX_PAGE_SHIFT = 7
_C2H_PKT_BUF = 256
# reserved-page counts [SRC] halmac_88xx_cfg.h RSVD_PG_*_NUM (drv clamped to 8 by
# cfg_drv_rsvd_pg_num_88xx for 8821c).
_RSVD_DRV, _RSVD_H2C_EXTRA, _RSVD_H2C_STATIC = 8, 24, 8
_RSVD_H2CQ, _RSVD_CPU_INSTR, _RSVD_FW_TXBUF, _RSVD_CSIBUF = 8, 0, 4, 0
_BLK_DESC_NUM = 3                  # USB descriptor blocks [SRC] init_8821c.c:617

# DMA mapping ids [SRC] halmac_type.h ; 3-bulkout NORMAL rqpn/pg tables [SRC] init_8821c.c
_MAP_LOW, _MAP_NORMAL, _MAP_HIGH = 1, 2, 3
_PQ_HI, _PQ_MG, _PQ_BK, _PQ_BE, _PQ_VI, _PQ_VO = (
    _MAP_HIGH, _MAP_HIGH, _MAP_LOW, _MAP_LOW, _MAP_NORMAL, _MAP_NORMAL)
_PG_HQ, _PG_NQ, _PG_LQ, _PG_EXQ, _PG_GAPQ = 16, 16, 16, 0, 1
_MAC_TRX_ENABLE = 0xFF             # CR all-TRX enable [SRC] init_8821c.c:451


def _txff_pages() -> dict:
    """set_trx_fifo_info_8821c: reserved-page boundary math (NORMAL trx, no rx-expand/LA).
    [SRC] init_8821c.c:648-711."""
    tx_pg = _TX_FIFO_SIZE >> _TX_PAGE_SHIFT
    rsvd = (_RSVD_DRV + _RSVD_H2C_EXTRA + _RSVD_H2C_STATIC + _RSVD_H2CQ
            + _RSVD_CPU_INSTR + _RSVD_FW_TXBUF + _RSVD_CSIBUF)
    acq = tx_pg - rsvd
    pubq = acq - _PG_HQ - _PG_LQ - _PG_NQ - _PG_EXQ - _PG_GAPQ
    h2cq_addr = (tx_pg - _RSVD_CSIBUF - _RSVD_FW_TXBUF - _RSVD_CPU_INSTR - _RSVD_H2CQ)
    return {"boundary": tx_pg - rsvd, "pubq": pubq, "h2cq_addr": h2cq_addr}


def _init_trx_cfg(t, bulkout_num: int) -> None:
    """init_trx_cfg_8821c [SRC] init_8821c.c:422 — queue mapping + CR reset/enable + H2CQ."""
    pq = (((_PQ_HI << 14) | (_PQ_MG << 12) | (_PQ_BK << 10) | (_PQ_BE << 8)
           | (_PQ_VI << 6) | (_PQ_VO << 4)) & 0xFFFF)
    if bulkout_num != 3:
        raise NotImplementedError("RTL8821CU: only the 3-bulkout RQPN table is ported")
    t.write16(REG_TXDMA_PQ_MAP, pq)
    if t.read8(REG_WMAC_FWPKT_CR) & 0x80:       # en_fwff (BIT_FWEN) — off on this card
        raise NotImplementedError("RTL8821CU: fwff-enabled path not ported")
    t.write8(REG_CR, 0)
    t.write16(REG_FWFF_CTRL, t.read16(REG_FWFF_PKT_INFO))
    t.write8(REG_CR, _MAC_TRX_ENABLE)
    t.write32(REG_H2CQ_CSR, 1 << 31)


def _priority_queue_cfg(t) -> None:
    """priority_queue_cfg_8821c [SRC] init_8821c.c:523 — FIFO page numbers/boundaries, the
    USB block-desc + auto-LLT init, and the transfer mode."""
    pg = _txff_pages()
    bnd = pg["boundary"]
    t.write16(REG_FIFOPAGE_INFO_1, _PG_HQ)
    t.write16(REG_FIFOPAGE_INFO_1 + 4, _PG_LQ)      # INFO_2
    t.write16(REG_FIFOPAGE_INFO_1 + 8, _PG_NQ)      # INFO_3
    t.write16(REG_FIFOPAGE_INFO_1 + 12, _PG_EXQ)    # INFO_4
    t.write16(REG_FIFOPAGE_INFO_1 + 16, pg["pubq"])  # INFO_5
    t.write32(REG_RQPN_CTRL_2, t.read32(REG_RQPN_CTRL_2) | (1 << 31))
    t.write16(REG_FIFOPAGE_CTRL_2, bnd)
    t.write8(REG_FWHW_TXQ_CTRL + 2, t.read8(REG_FWHW_TXQ_CTRL + 2) | (1 << 4))
    t.write16(REG_BCNQ_BDNY_V1, bnd)
    t.write16(REG_FIFOPAGE_CTRL_2 + 2, bnd)
    t.write16(REG_BCNQ1_BDNY_V1, bnd)
    t.write32(REG_RXFF_BNDY, _RX_FIFO_SIZE - _C2H_PKT_BUF - 1)
    # USB block-desc + auto-init LLT
    v = t.read8(REG_AUTO_LLT_V1)
    t.write8(REG_AUTO_LLT_V1, (v & ~(0xF << 4)) | (_BLK_DESC_NUM << 4))
    t.write8(REG_AUTO_LLT_V1 + 3, _BLK_DESC_NUM)
    t.write8(REG_TXDMA_OFFSET_CHK + 1, t.read8(REG_TXDMA_OFFSET_CHK + 1) | (1 << 1))
    t.write8(REG_AUTO_LLT_V1, t.read8(REG_AUTO_LLT_V1) | (1 << 0))
    for _ in range(1000):
        if not (t.read8(REG_AUTO_LLT_V1) & (1 << 0)):
            break
    else:
        raise RuntimeError("RTL8821CU: auto-init LLT timed out")
    t.write8(REG_CR + 3, 0)                         # HALMAC_TRNSFER_NORMAL


def _init_h2c(t) -> None:
    """init_h2c_8821c [SRC] init_8821c.c:820 — point the H2C queue at its reserved pages."""
    h2cq_addr = _txff_pages()["h2cq_addr"] << _TX_PAGE_SHIFT
    h2cq_size = _RSVD_H2CQ << _TX_PAGE_SHIFT
    t.write32(REG_H2C_HEAD, (t.read32(REG_H2C_HEAD) & 0xFFFC0000) | h2cq_addr)
    t.write32(REG_H2C_READ_ADDR, (t.read32(REG_H2C_READ_ADDR) & 0xFFFC0000) | h2cq_addr)
    t.write32(REG_H2C_TAIL, (t.read32(REG_H2C_TAIL) & 0xFFFC0000) | (h2cq_addr + h2cq_size))
    t.write8(REG_H2C_INFO, (t.read8(REG_H2C_INFO) & 0xFC) | 0x01)
    t.write8(REG_H2C_INFO, (t.read8(REG_H2C_INFO) & 0xFB) | 0x04)
    t.write8(REG_TXDMA_OFFSET_CHK + 1, (t.read8(REG_TXDMA_OFFSET_CHK + 1) & 0x7F) | 0x80)
    # get_h2c_buf_free_space_88xx: read the H2C queue write/read pointers (free-space sanity).
    t.read32(REG_H2C_PKT_WRITEADDR)
    t.read32(REG_H2C_PKT_READADDR)


def _init_protocol_cfg(t) -> None:
    """init_protocol_cfg_8821c [SRC] init_8821c.c:766 — AMPDU/RTS/BAR/fast-EDCA thresholds."""
    t.write8(REG_AMPDU_MAX_TIME_V1, 0x70)
    t.write8(REG_TX_HANG_CTRL, t.read8(REG_TX_HANG_CTRL) | (1 << 2))   # BIT_EN_EOF_V1
    pre_txcnt = 0x1E4 | (1 << 11)                                      # | BIT_EN_PRECNT
    t.write8(REG_PRECNT_CTRL, pre_txcnt & 0xFF)
    t.write8(REG_PRECNT_CTRL + 1, pre_txcnt >> 8)
    t.write32(REG_PROT_MODE_CTRL, 0x101008FF)
    t.write16(REG_BAR_MODE_CTRL + 2, 0x01 | (0x08 << 8))
    t.write8(REG_FAST_EDCA_VOVI_SETTING, 0x06)
    t.write8(REG_FAST_EDCA_VOVI_SETTING + 2, 0x06)
    t.write8(REG_FAST_EDCA_BEBK_SETTING, 0x06)
    t.write8(REG_FAST_EDCA_BEBK_SETTING + 2, 0x06)
    t.write8(REG_INIRTS_RATE_SEL, t.read8(REG_INIRTS_RATE_SEL) | (1 << 5))


def _init_edca_cfg(t) -> None:
    """init_edca_cfg_8821c [SRC] init_8821c.c:878 — slot/SIFS/TXOP/NAV/beacon timing."""
    t.write8(REG_TIMER0_SRC_SEL, t.read8(REG_TIMER0_SRC_SEL) & ~((1 << 4) | (1 << 5) | (1 << 6)))
    t.write16(REG_TXPAUSE, 0)
    t.write8(REG_SLOT, 0x09)
    t.write8(REG_PIFS, 0x19)
    t.write32(REG_SIFS, 0x10100E0A)
    t.write16(REG_EDCA_VO_PARAM + 2, 0x0186)
    t.write16(REG_EDCA_VI_PARAM + 2, 0x03BC)
    t.write32(REG_RD_NAV_NXT, 0x001B0005)
    t.write16(REG_RXTSF_OFFSET_CCK, 0x3030)
    t.write8(REG_BCN_CTRL, t.read8(REG_BCN_CTRL) | (1 << 3))          # BIT_EN_BCN_FUNCTION
    t.write32(REG_TBTT_PROHIBIT, 0x00006404)
    t.write8(REG_DRVERLYINT, 0x04)
    t.write8(REG_BCNDMATIM, 0x02)
    t.write8(REG_TX_PTCL_CTRL + 1, t.read8(REG_TX_PTCL_CTRL + 1) & ~(1 << 4))


def _init_wmac_cfg(t) -> None:
    """init_wmac_cfg_8821c [SRC] init_8821c.c:924 — RX filter maps, RCR, TX/RX function cfg."""
    t.write32(REG_RXFLTMAP0, 0x0FFFFFFF)
    t.write16(REG_RXFLTMAP2, 0xFFFF)
    t.write32(REG_RCR, 0xE400220E)
    t.write8(REG_RX_PKT_LIMIT, 0x18)
    t.write8(REG_TCR + 2, 0x30)
    t.write8(REG_TCR + 1, 0x30)
    t.write8(REG_ACKTO_CCK, 0x40)
    t.write8(REG_WMAC_TRXPTCL_CTL_H, t.read8(REG_WMAC_TRXPTCL_CTL_H) | (1 << 1))
    t.write8(REG_SND_PTCL_CTRL, t.read8(REG_SND_PTCL_CTRL) | (1 << 6))
    t.write32(REG_WMAC_OPTION_FUNCTION_2, 0x30810041)
    t.write8(REG_WMAC_OPTION_FUNCTION + 4, 0x98)                      # NORMAL trx mode


def init_mac_cfg(t, bulkout_num: int = 3) -> None:
    """halmac_init_mac_cfg(NORMAL) for 8821c [SRC] init_8821c.c:382 init_mac_cfg_8821c."""
    _init_trx_cfg(t, bulkout_num)
    _priority_queue_cfg(t)
    _init_h2c(t)
    _init_protocol_cfg(t)
    _init_edca_cfg(t)
    _init_wmac_cfg(t)


def _cfg_usb_rx_agg(t) -> None:
    """cfg_usb_rx_agg_88xx for rx_agg_switch(USB) [SRC] halmac_usb_88xx.c:87 — enable RX DMA
    aggregation (drv_define=0 -> USB2.0 size 5 / timeout 0x20; size_limit_en=1)."""
    dma_usb_agg = t.read8(REG_RXDMA_AGG_PG_TH + 3)
    agg_enable = t.read8(REG_TXDMA_PQ_MAP) | (1 << 2)        # BIT_RXDMA_AGG_EN
    dma_usb_agg &= ~(1 << 7)                                  # USB mode
    size, timeout = (0x5, 0xA) if t.read8(REG_SYS_CFG2 + 3) == 0x20 else (0x5, 0x20)
    t.write32(REG_RXDMA_AGG_PG_TH, t.read32(REG_RXDMA_AGG_PG_TH) | (1 << 29))  # EN_PRE_CALC
    t.write8(REG_TXDMA_PQ_MAP, agg_enable)
    t.write8(REG_RXDMA_AGG_PG_TH + 3, dma_usb_agg)
    t.write16(REG_RXDMA_AGG_PG_TH, (size | (timeout << 8)) & 0xFFFF)


def init_mac_flow(t, info) -> None:
    """init_mac_flow [SRC] hal_halmac.c:3452 — MAC cfg, the RCR-cache sync read, RTS-full-BW
    enable (CONFIG_RTS_FULL_BW on in this build), and the USB RX-aggregation switch."""
    init_mac_cfg(t)
    t.read32(REG_RCR)                                        # HW_VAR_RCR cache sync
    t.write8(REG_INIRTS_RATE_SEL, t.read8(REG_INIRTS_RATE_SEL) | (1 << 5))  # rts_full_bw(TRUE)
    _cfg_usb_rx_agg(t)
