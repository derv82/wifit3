"""RTL8822BU MAC power-on — pre-init system cfg, the HALMAC power switch, init system cfg.

``rtw_halmac_poweron`` [SRC] hal/hal_halmac.c:2705 drives three steps:
  1. ``pre_init_system_cfg_8822b`` — RSV_CTRL clear, PIN-mux, BB/RF disabled for power-on.
  2. ``mac_pwr_switch_usb_8822b(POWER_ON)`` — probe the power state, then run the 8822b
     ``card_en_flow`` power sequence. On a *warm* chip (already on) it returns PWR_UNCHANGE
     and the caller forces a power-OFF (``card_dis_flow``) then power-ON again — the
     "warm reboot but device not power off" workaround. A cold chip reads REG_CR == 0xEA
     and runs card-enable directly (no reset), which is what the cold captures show.
  3. ``init_system_cfg_8822b`` — WL platform reset, SYS_FUNC_EN, disable boot-from-flash.

Ported from:
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_init_8822b.c:945  pre_init_system_cfg_8822b
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:32    mac_pwr_switch_usb_8822b
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_init_8822b.c:715  init_system_cfg_8822b
  [SRC] hal/halmac/halmac_88xx/halmac_cfg_wmac_88xx.c:637            enable_bb_rf_88xx
"""
from __future__ import annotations

from dataclasses import dataclass

from . import pwrseq
from .constants import (
    BIT_AUTO_INIT_LLT_V1,
    BIT_BOOT_FSPI_EN,
    BIT_EN_BCN_FUNCTION,
    BIT_EN_EOF_V1,
    BIT_EN_PRE_CALC,
    BIT_FSPI_EN,
    BIT_FWEN,
    BIT_MASK_BLK_DESC_NUM,
    BIT_R_DISABLE_CHECK_VHTSIGB_CRC,
    BIT_RXDMA_AGG_EN,
    BIT_SHIFT_BLK_DESC_NUM,
    BIT_SHIFT_DMA_AGG_TO,
    BIT_WL_PLATFORM_RST,
    BLK_DESC_NUM,
    C2H_PKT_BUF,
    HALMAC_TRNSFER_NORMAL,
    MAC_TRX_ENABLE,
    MCUFW_CTRL_FW_EXIST,
    PG_NUM_NORMAL_3BULKOUT,
    REG_AMPDU_MAX_TIME_V1,
    REG_AUTO_LLT_V1,
    REG_BAR_MODE_CTRL,
    REG_BCN_CTRL,
    REG_BCNDMATIM,
    REG_BCNQ1_BDNY_V1,
    REG_BCNQ_BDNY_V1,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_CR_DISABLED,
    REG_DRVERLYINT,
    REG_EDCA_VI_PARAM,
    REG_EDCA_VO_PARAM,
    REG_FAST_EDCA_BEBK_SETTING,
    REG_FAST_EDCA_VOVI_SETTING,
    REG_FIFOPAGE_CTRL_2,
    REG_FIFOPAGE_INFO_1,
    REG_FIFOPAGE_INFO_2,
    REG_FIFOPAGE_INFO_3,
    REG_FIFOPAGE_INFO_4,
    REG_FIFOPAGE_INFO_5,
    REG_FWFF_CTRL,
    REG_FWFF_PKT_INFO,
    REG_FWHW_TXQ_CTRL,
    REG_GPIO_MUXCFG,
    REG_H2C_HEAD,
    REG_H2C_INFO,
    REG_H2C_PKT_READADDR,
    REG_H2C_PKT_WRITEADDR,
    REG_H2C_READ_ADDR,
    REG_H2C_TAIL,
    REG_H2CQ_CSR,
    REG_INIRTS_RATE_SEL,
    REG_LED_CFG,
    REG_MCUFW_CTRL,
    REG_PAD_CTRL1,
    REG_PIFS,
    REG_PRE_INIT_FE5B,
    REG_PROT_MODE_CTRL,
    REG_RCR,
    REG_RD_NAV_NXT,
    REG_RF_CTRL,
    REG_RPWM,
    REG_RQPN_CTRL_2,
    REG_RSV_CTRL,
    REG_RX_PKT_LIMIT,
    REG_RXDMA_AGG_PG_TH,
    REG_RXFF_BNDY,
    REG_RXFLTMAP0,
    REG_RXFLTMAP2,
    REG_RXTSF_OFFSET_CCK,
    REG_SIFS,
    REG_SLOT,
    REG_SND_PTCL_CTRL,
    REG_SW_AMPDU_BURST_MODE_CTRL,
    REG_SW_MDIO,
    REG_SYS_CFG1,
    REG_SYS_CFG2,
    REG_SYS_FUNC_EN,
    REG_SYS_STATUS1,
    REG_TBTT_PROHIBIT,
    REG_TCR,
    REG_TIMER0_SRC_SEL,
    REG_TX_HANG_CTRL,
    REG_TX_PTCL_CTRL,
    REG_TXDMA_OFFSET_CHK,
    REG_TXDMA_PQ_MAP,
    REG_TXPAUSE,
    REG_WLRF1,
    REG_WMAC_FWPKT_CR,
    REG_WMAC_OPTION_FUNCTION,
    REG_WMAC_TRXPTCL_CTL,
    RQPN_NORMAL_3BULKOUT,
    RSVD_PG_CPU_INSTRUCTION_NUM,
    RSVD_PG_CSIBUF_NUM,
    RSVD_PG_DRV_NUM_8822BU,
    RSVD_PG_FW_TXBUF_NUM,
    RSVD_PG_H2C_EXTRAINFO_NUM,
    RSVD_PG_H2C_STATICINFO_NUM,
    RSVD_PG_H2CQ_NUM,
    RX_FIFO_SIZE_8822B,
    RXAGG_USB_SIZE,
    RXAGG_USB_TIMEOUT_OTHER,
    RXAGG_USB_TIMEOUT_USB3,
    SYS_FUNC_EN,
    TX_FIFO_SIZE_8822B,
    TX_PAGE_SIZE_SHIFT,
    TXDMA_MAP_SHIFTS,
    WLAN_AMPDU_MAX_TIME,
    WLAN_BAR_MODE_CTRL_HI,
    WLAN_BCN_DMA_TIME,
    WLAN_DRV_EARLY_INT,
    WLAN_FAST_EDCA_TH,
    WLAN_MAC_OPT_FUNC2,
    WLAN_MAC_OPT_NORM_FUNC1,
    WLAN_NAV_CFG,
    WLAN_PIFS_TIME,
    WLAN_PROT_MODE_CTRL,
    WLAN_RCR_CFG,
    WLAN_RX_FILTER0,
    WLAN_RX_FILTER2,
    WLAN_RX_TSF_CFG,
    WLAN_RXPKT_MAX_SZ_512,
    WLAN_SIFS_CFG,
    WLAN_SLOT_TIME,
    WLAN_TBTT_TIME,
    WLAN_TX_FUNC_CFG1,
    WLAN_TX_FUNC_CFG2,
    WLAN_VI_TXOP_LIMIT,
    WLAN_VO_TXOP_LIMIT,
)

_SYS_CFG2_USB3 = 0x20            # REG_SYS_CFG2+3 value that marks a USB3 link
_POLL_CAP = 1_000_000


def _enable_bb_rf(t, enable: bool) -> None:
    """enable_bb_rf_88xx [SRC] halmac_cfg_wmac_88xx.c:637 — gate BB/RF clocks.

    Power-on uses the disable path (enable=0): clear the BB enable bits of REG_SYS_FUNC_EN,
    REG_RF_CTRL and REG_WLRF1. The enable path additionally runs board_rf_fine_tune (a cached
    EFUSE read for the 2L-PCB XTAL tweak); it is wired at the MAC-init-for-RX milestone where
    it can be checked against the wire it produces."""
    if enable:
        raise NotImplementedError("RTL8822BU: enable_bb_rf(on) is wired at the MAC-init milestone")
    v = t.read8(REG_SYS_FUNC_EN)
    t.write8(REG_SYS_FUNC_EN, v & ~((1 << 0) | (1 << 1)))
    v = t.read8(REG_RF_CTRL)
    t.write8(REG_RF_CTRL, v & ~((1 << 0) | (1 << 1) | (1 << 2)))
    v = t.read32(REG_WLRF1)
    t.write32(REG_WLRF1, v & ~((1 << 24) | (1 << 25) | (1 << 26)))


def pre_init_system_cfg(t) -> None:
    """pre_init_system_cfg_8822b [SRC] halmac_init_8822b.c:945."""
    t.write8(REG_RSV_CTRL, 0)

    # USB: the 0xFE5B |= BIT(4) tweak is USB3-only (REG_SYS_CFG2+3 == 0x20). The cold
    # captures read 0x80 here, so it is skipped — its USB3 side stays source-ported-but-
    # uncaptured until a USB2 capture exists. (Counter-intuitively, 0x20 == USB3.)
    if t.read8(REG_SYS_CFG2 + 3) == _SYS_CFG2_USB3:
        t.write8(REG_PRE_INIT_FE5B, t.read8(REG_PRE_INIT_FE5B) | (1 << 4))

    # PIN-mux: PAD_CTRL1 set BIT28/29, LED_CFG clear BIT25/26, GPIO_MUXCFG set BIT2.
    v = t.read32(REG_PAD_CTRL1)
    t.write32(REG_PAD_CTRL1, (v & ~((1 << 28) | (1 << 29))) | (1 << 28) | (1 << 29))
    v = t.read32(REG_LED_CFG)
    t.write32(REG_LED_CFG, v & ~((1 << 25) | (1 << 26)))
    v = t.read32(REG_GPIO_MUXCFG)
    t.write32(REG_GPIO_MUXCFG, (v & ~(1 << 2)) | (1 << 2))

    _enable_bb_rf(t, enable=False)

    t.read8(REG_SYS_CFG1 + 2)            # test-mode check: BIT(4) set => WLAN-mode fail (not enforced)


def _mac_pwr_switch(t, chip_ver: int, power_on: bool) -> bool:
    """mac_pwr_switch_usb_8822b [SRC] halmac_usb_8822b.c:32. Returns True iff the chip was
    already in the requested ON state (HALMAC_RET_PWR_UNCHANGE), so the caller can reset."""
    rpwm = t.read8(REG_RPWM)
    if t.read16(REG_MCUFW_CTRL) == MCUFW_CTRL_FW_EXIST:
        t.write8(REG_RPWM, (rpwm ^ (1 << 7)) & 0x80)        # leave 32K

    if t.read8(REG_CR) == REG_CR_DISABLED:                   # 0xEA => disabled/off
        mac_on = False
    else:
        mac_on = not (t.read8(REG_SYS_STATUS1 + 1) & (1 << 0))

    if power_on and mac_on:
        return True                                          # PWR_UNCHANGE

    if not power_on:
        pwrseq.run_pwr_seq(t, pwrseq.CARD_DIS_FLOW, chip_ver)
    else:
        pwrseq.run_pwr_seq(t, pwrseq.CARD_EN_FLOW, chip_ver)
        v = t.read8(REG_SYS_STATUS1 + 1)                     # W8_CLR BIT(0)
        t.write8(REG_SYS_STATUS1 + 1, v & ~(1 << 0))
        t.read8(REG_SW_MDIO + 3)                             # post-power-on read-twice probe
    return False


def init_system_cfg(t) -> None:
    """init_system_cfg_8822b [SRC] halmac_init_8822b.c:715."""
    v = t.read32(REG_CPU_DMEM_CON) | BIT_WL_PLATFORM_RST
    t.write32(REG_CPU_DMEM_CON, v)

    v = t.read8(REG_SYS_FUNC_EN + 1) | SYS_FUNC_EN
    t.write8(REG_SYS_FUNC_EN + 1, v)

    # disable boot-from-flash so the driver can download its own FW
    tmp = t.read32(REG_MCUFW_CTRL)
    if tmp & BIT_BOOT_FSPI_EN:
        t.write32(REG_MCUFW_CTRL, tmp & ~BIT_BOOT_FSPI_EN)
        t.write32(REG_GPIO_MUXCFG, t.read32(REG_GPIO_MUXCFG) & ~BIT_FSPI_EN)


def power_on(t, chip_ver: int) -> None:
    """rtw_halmac_poweron [SRC] hal/hal_halmac.c:2705 — the full USB power-on, including the
    warm-reboot off->on workaround (which only fires on an already-powered chip)."""
    pre_init_system_cfg(t)
    if _mac_pwr_switch(t, chip_ver, power_on=True):
        # warm chip: force off then on again [SRC] hal_halmac.c:2768-2772
        _mac_pwr_switch(t, chip_ver, power_on=False)
        _mac_pwr_switch(t, chip_ver, power_on=True)
    init_system_cfg(t)


# --- MAC init for RX: init_trx_cfg (queue mapping + FIFO/page alloc + TRX enable) ----------
@dataclass
class TxffAlloc:
    """The TX-FIFO page layout set_trx_fifo_info computes for NORMAL mode on this card
    (3 bulk-OUT, 2048-page FIFO). All values are wire-verified."""
    rsvd_boundary: int      # 1996 (0x7CC) — first reserved page = ACQ page count
    rsvd_h2cq_addr: int     # 2036 — H2CQ page base
    rsvd_fw_txbuf_addr: int  # 2044 — FW-TXBUF page base (general-info FW_TX_BOUNDARY)
    high_pg: int            # 64
    low_pg: int             # 64
    normal_pg: int          # 64
    extra_pg: int           # 0
    pub_pg: int             # 1803 (0x70B)


def set_trx_fifo_info() -> TxffAlloc:
    return _set_trx_fifo_info()


def _set_trx_fifo_info() -> TxffAlloc:
    """set_trx_fifo_info_8822b [SRC] halmac_init_8822b.c:643 + pg_num_parser_88xx
    [SRC] halmac_init_88xx.c:812 — pure page arithmetic (no IO). NORMAL mode, no RX-expand/LA."""
    tx_fifo_pg_num = TX_FIFO_SIZE_8822B >> TX_PAGE_SIZE_SHIFT           # 2048
    rsvd_pg_num = (RSVD_PG_DRV_NUM_8822BU + RSVD_PG_H2C_EXTRAINFO_NUM
                   + RSVD_PG_H2C_STATICINFO_NUM + RSVD_PG_H2CQ_NUM
                   + RSVD_PG_CPU_INSTRUCTION_NUM + RSVD_PG_FW_TXBUF_NUM
                   + RSVD_PG_CSIBUF_NUM)                                # 52
    acq_pg_num = tx_fifo_pg_num - rsvd_pg_num                          # 1996
    cur = tx_fifo_pg_num
    cur -= RSVD_PG_CSIBUF_NUM
    cur -= RSVD_PG_FW_TXBUF_NUM
    rsvd_fw_txbuf_addr = cur                                          # 2044
    cur -= RSVD_PG_CPU_INSTRUCTION_NUM
    cur -= RSVD_PG_H2CQ_NUM
    rsvd_h2cq_addr = cur                                               # 2036
    pg = PG_NUM_NORMAL_3BULKOUT
    pub_pg = acq_pg_num - pg["hq"] - pg["lq"] - pg["nq"] - pg["exq"] - pg["gap"]
    return TxffAlloc(rsvd_boundary=acq_pg_num, rsvd_h2cq_addr=rsvd_h2cq_addr,
                     rsvd_fw_txbuf_addr=rsvd_fw_txbuf_addr,
                     high_pg=pg["hq"], low_pg=pg["lq"], normal_pg=pg["nq"],
                     extra_pg=pg["exq"], pub_pg=pub_pg)


def _txdma_queue_mapping(t) -> None:
    """txdma_queue_mapping_8822b [SRC] halmac_init_8822b.c:477 — pack each AC's DMA channel
    (rqpn_parser, NORMAL/3-bulkout) into REG_TXDMA_PQ_MAP. Yields 0xF5A0 on this card."""
    value16 = 0
    for ac, shift in TXDMA_MAP_SHIFTS.items():
        value16 |= RQPN_NORMAL_3BULKOUT[ac] << shift
    t.write16(REG_TXDMA_PQ_MAP, value16)


def _priority_queue_cfg(t, alloc: TxffAlloc) -> None:
    """priority_queue_cfg_8822b [SRC] halmac_init_8822b.c:521 — write the per-queue page counts
    and boundaries, kick the auto-LLT init, and set NORMAL transfer mode."""
    t.write16(REG_FIFOPAGE_INFO_1, alloc.high_pg)
    t.write16(REG_FIFOPAGE_INFO_2, alloc.low_pg)
    t.write16(REG_FIFOPAGE_INFO_3, alloc.normal_pg)
    t.write16(REG_FIFOPAGE_INFO_4, alloc.extra_pg)
    t.write16(REG_FIFOPAGE_INFO_5, alloc.pub_pg)
    t.write32(REG_RQPN_CTRL_2, t.read32(REG_RQPN_CTRL_2) | (1 << 31))
    t.write16(REG_FIFOPAGE_CTRL_2, alloc.rsvd_boundary)
    t.write8(REG_FWHW_TXQ_CTRL + 2, t.read8(REG_FWHW_TXQ_CTRL + 2) | (1 << 4))
    t.write16(REG_BCNQ_BDNY_V1, alloc.rsvd_boundary)               # USB: 16-bit write
    t.write16(REG_FIFOPAGE_CTRL_2 + 2, alloc.rsvd_boundary)
    t.write16(REG_BCNQ1_BDNY_V1, alloc.rsvd_boundary)
    t.write32(REG_RXFF_BNDY, RX_FIFO_SIZE_8822B - C2H_PKT_BUF - 1)

    # USB block-descriptor number + TX-DMA offset check, then auto-init LLT.
    v = t.read8(REG_AUTO_LLT_V1)
    v &= ~(BIT_MASK_BLK_DESC_NUM << BIT_SHIFT_BLK_DESC_NUM)
    v |= BLK_DESC_NUM << BIT_SHIFT_BLK_DESC_NUM
    t.write8(REG_AUTO_LLT_V1, v)
    t.write8(REG_AUTO_LLT_V1 + 3, BLK_DESC_NUM)
    t.write8(REG_TXDMA_OFFSET_CHK + 1, t.read8(REG_TXDMA_OFFSET_CHK + 1) | (1 << 1))

    t.write8(REG_AUTO_LLT_V1, t.read8(REG_AUTO_LLT_V1) | BIT_AUTO_INIT_LLT_V1)
    for _ in range(_POLL_CAP):
        if not (t.read8(REG_AUTO_LLT_V1) & BIT_AUTO_INIT_LLT_V1):
            break
    else:
        raise RuntimeError("RTL8822BU: auto-init LLT timed out")
    t.write8(REG_CR + 3, HALMAC_TRNSFER_NORMAL)


def _init_h2c(t, alloc: TxffAlloc) -> None:
    """init_h2c_8822b [SRC] halmac_init_8822b.c:792 — point the H2C ring at its reserved pages
    and arm it, then read back the free space (a sanity read, no effect on the wire IO)."""
    h2cq_addr = alloc.rsvd_h2cq_addr << TX_PAGE_SIZE_SHIFT
    h2cq_size = RSVD_PG_H2CQ_NUM << TX_PAGE_SIZE_SHIFT
    t.write32(REG_H2C_HEAD, (t.read32(REG_H2C_HEAD) & 0xFFFC0000) | h2cq_addr)
    t.write32(REG_H2C_READ_ADDR, (t.read32(REG_H2C_READ_ADDR) & 0xFFFC0000) | h2cq_addr)
    t.write32(REG_H2C_TAIL, (t.read32(REG_H2C_TAIL) & 0xFFFC0000) | (h2cq_addr + h2cq_size))
    t.write8(REG_H2C_INFO, (t.read8(REG_H2C_INFO) & 0xFC) | 0x01)
    t.write8(REG_H2C_INFO, (t.read8(REG_H2C_INFO) & 0xFB) | 0x04)
    t.write8(REG_TXDMA_OFFSET_CHK + 1, (t.read8(REG_TXDMA_OFFSET_CHK + 1) & 0x7F) | 0x80)
    t.read32(REG_H2C_PKT_WRITEADDR)            # get_h2c_buf_free_space: hw wptr / fw rptr
    t.read32(REG_H2C_PKT_READADDR)


def init_trx_cfg(t) -> None:
    """init_trx_cfg_8822b [SRC] halmac_init_8822b.c — queue mapping, CR TRX enable, the FWFF
    drain, page/priority config, and the H2C ring. (FW-fast-forward is not enabled here, so the
    en_fwff branch reduces to a single REG_WMAC_FWPKT_CR read.)"""
    _txdma_queue_mapping(t)
    en_fwff = t.read8(REG_WMAC_FWPKT_CR) & BIT_FWEN     # 0 on this boot -> no fwff drain
    t.write8(REG_CR, 0)
    t.write16(REG_FWFF_CTRL, t.read16(REG_FWFF_PKT_INFO))
    t.write8(REG_CR, MAC_TRX_ENABLE)
    if en_fwff:
        raise NotImplementedError("RTL8822BU: FW-fast-forward enable path unobserved")
    t.write32(REG_H2CQ_CSR, 1 << 31)
    alloc = _set_trx_fifo_info()
    _priority_queue_cfg(t, alloc)
    _init_h2c(t, alloc)
    t.write8(REG_TXDMA_PQ_MAP, t.read8(REG_TXDMA_PQ_MAP) | (1 << 0))   # USB


def init_protocol_cfg(t) -> None:
    """init_protocol_cfg_8822b [SRC] halmac_init_8822b.c:750 — RTS/AMPDU/BAR + fast-EDCA TH."""
    t.write8(REG_SW_AMPDU_BURST_MODE_CTRL, t.read8(REG_SW_AMPDU_BURST_MODE_CTRL) & ~(1 << 6))
    t.write8(REG_AMPDU_MAX_TIME_V1, WLAN_AMPDU_MAX_TIME)
    t.write8(REG_TX_HANG_CTRL, t.read8(REG_TX_HANG_CTRL) | BIT_EN_EOF_V1)
    t.write32(REG_PROT_MODE_CTRL, WLAN_PROT_MODE_CTRL)
    t.write16(REG_BAR_MODE_CTRL + 2, WLAN_BAR_MODE_CTRL_HI)
    t.write8(REG_FAST_EDCA_VOVI_SETTING, WLAN_FAST_EDCA_TH)
    t.write8(REG_FAST_EDCA_VOVI_SETTING + 2, WLAN_FAST_EDCA_TH)
    t.write8(REG_FAST_EDCA_BEBK_SETTING, WLAN_FAST_EDCA_TH)
    t.write8(REG_FAST_EDCA_BEBK_SETTING + 2, WLAN_FAST_EDCA_TH)
    t.write8(REG_INIRTS_RATE_SEL, t.read8(REG_INIRTS_RATE_SEL) | (1 << 5))


def init_edca_cfg(t) -> None:
    """init_edca_cfg_8822b [SRC] halmac_init_8822b.c:851 — slot/PIFS/SIFS/TXOP/NAV + beacon timing."""
    t.write8(REG_TIMER0_SRC_SEL, t.read8(REG_TIMER0_SRC_SEL) & ~((1 << 4) | (1 << 5) | (1 << 6)))
    t.write16(REG_TXPAUSE, 0x0000)
    t.write8(REG_SLOT, WLAN_SLOT_TIME)
    t.write8(REG_PIFS, WLAN_PIFS_TIME)
    t.write32(REG_SIFS, WLAN_SIFS_CFG)
    t.write16(REG_EDCA_VO_PARAM + 2, WLAN_VO_TXOP_LIMIT)
    t.write16(REG_EDCA_VI_PARAM + 2, WLAN_VI_TXOP_LIMIT)
    t.write32(REG_RD_NAV_NXT, WLAN_NAV_CFG)
    t.write16(REG_RXTSF_OFFSET_CCK, WLAN_RX_TSF_CFG)
    t.write8(REG_BCN_CTRL, t.read8(REG_BCN_CTRL) | BIT_EN_BCN_FUNCTION)
    t.write32(REG_TBTT_PROHIBIT, WLAN_TBTT_TIME)
    t.write8(REG_DRVERLYINT, WLAN_DRV_EARLY_INT)
    t.write8(REG_BCNDMATIM, WLAN_BCN_DMA_TIME)
    t.write8(REG_TX_PTCL_CTRL + 1, t.read8(REG_TX_PTCL_CTRL + 1) & ~(1 << 4))


def init_wmac_cfg(t) -> None:
    """init_wmac_cfg_8822b [SRC] halmac_init_8822b.c:896 — the WMAC RX path: RXFLTMAP, RCR,
    TCR, and the receive option functions. init_low_pwr_8822b is empty (no IO)."""
    t.write32(REG_RXFLTMAP0, WLAN_RX_FILTER0)
    t.write16(REG_RXFLTMAP2, WLAN_RX_FILTER2)
    t.write32(REG_RCR, WLAN_RCR_CFG)
    t.write8(REG_RX_PKT_LIMIT, WLAN_RXPKT_MAX_SZ_512)
    t.write8(REG_TCR + 2, WLAN_TX_FUNC_CFG2)
    t.write8(REG_TCR + 1, WLAN_TX_FUNC_CFG1)
    t.write8(REG_WMAC_TRXPTCL_CTL + 4, t.read8(REG_WMAC_TRXPTCL_CTL + 4) | (1 << 1))
    t.write8(REG_SND_PTCL_CTRL, t.read8(REG_SND_PTCL_CTRL) | BIT_R_DISABLE_CHECK_VHTSIGB_CRC)
    t.write32(REG_WMAC_OPTION_FUNCTION + 8, WLAN_MAC_OPT_FUNC2)
    t.write8(REG_WMAC_OPTION_FUNCTION + 4, WLAN_MAC_OPT_NORM_FUNC1)   # NORMAL transfer mode


def init_mac_cfg(t) -> None:
    """init_mac_cfg_88xx [SRC] halmac_init_88xx.c:504 — the four MAC sub-configs in order."""
    init_trx_cfg(t)
    init_protocol_cfg(t)
    init_edca_cfg(t)
    init_wmac_cfg(t)


def _cfg_usb_rx_agg(t) -> None:
    """cfg_usb_rx_agg_88xx [SRC] halmac_usb_88xx.c:88 — USB RX aggregation (the morrownr default
    rxagg_mode is USB). Enables agg in REG_TXDMA_PQ_MAP, selects USB (not DMA) agg, and sets the
    size/timeout threshold from the link-speed check."""
    dma_usb_agg = t.read8(REG_RXDMA_AGG_PG_TH + 3)
    agg_enable = t.read8(REG_TXDMA_PQ_MAP)
    agg_enable |= BIT_RXDMA_AGG_EN          # RX_AGG_MODE_USB
    dma_usb_agg &= ~(1 << 7)
    if t.read8(REG_SYS_CFG2 + 3) == _SYS_CFG2_USB3:
        size, timeout = RXAGG_USB_SIZE, RXAGG_USB_TIMEOUT_USB3
    else:
        size, timeout = RXAGG_USB_SIZE, RXAGG_USB_TIMEOUT_OTHER
    # size_limit_en is always set (avoid an RX over the driver's buffer size).
    t.write32(REG_RXDMA_AGG_PG_TH, t.read32(REG_RXDMA_AGG_PG_TH) | BIT_EN_PRE_CALC)
    t.write8(REG_TXDMA_PQ_MAP, agg_enable)
    t.write8(REG_RXDMA_AGG_PG_TH + 3, dma_usb_agg)
    t.write16(REG_RXDMA_AGG_PG_TH, size | (timeout << BIT_SHIFT_DMA_AGG_TO))


def init_mac_flow_tail(t) -> None:
    """The driver-side tail of init_mac_flow after init_mac_cfg [SRC] hal_halmac.c:3452:
    sync the RCR cache, enable RTS full-BW (the morrownr build defines CONFIG_RTS_FULL_BW),
    and turn on USB RX aggregation. _init_trx_cfg_drv (PCI-only) and cfg_operation_mode (empty)
    add no IO here."""
    t.read32(REG_RCR)                       # HW_VAR_RCR sync read [SRC] rtl8822b_ops.c:2076
    t.write8(REG_INIRTS_RATE_SEL, t.read8(REG_INIRTS_RATE_SEL) | (1 << 5))   # rts_full_bw(on)
    _cfg_usb_rx_agg(t)
