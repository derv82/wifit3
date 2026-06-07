"""RTL8188EUS MAC register configuration (M2a).

``PHY_MACConfig8188E`` [SRC] rtl8188e_phycfg.c:758 loads the MAC register table
through the phydm walker (``odm_config_mac_8188e`` = 8-bit writes), then sets the
AMPDU aggregation number. [WIRE] cap1 ops 797..end-of-table.
"""
from __future__ import annotations

from . import constants as C
from . import phy_cond
from .constants import BIT, MACRXEN, MACTXEN
from .constants import (
    _LLT_NO_ACTIVE,
    _LLT_WRITE_ACCESS,
    LAST_ENTRY_OF_TX_PKT_BUFFER,
    PBP_PAGE_SIZE,
    REG_BCNQ_BDNY,
    REG_LLT_INIT,
    REG_MAX_AGGR_NUM,
    REG_MGQ_BDNY,
    REG_PBP,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_TDECTRL,
    REG_TRXDMA_CTRL,
    REG_TRXFF_BNDY,
    REG_WMAC_LBK_BF_HD,
    RQPN_VALUE,
    RXFF_BOUNDARY,
    TRXDMA_QUEUE_MAP_1EP,
    TX_PAGE_BOUNDARY,
)
from .mac_reg_tbl import MAC_REG

MAX_AGGR_NUM = 0x07  # [SRC] include/Hal8188EPhyCfg.h (USB build; 0x0B is PCI-only)
_LLT_POLL_CAP = 1000


def phy_mac_config(t) -> None:
    """Apply ``array_mp_8188e_mac_reg`` (each taken row is an 8-bit write), then the
    AMPDU aggregation number to REG_MAX_AGGR_NUM."""
    phy_cond.walk_table(MAC_REG, lambda addr, val: t.write8(addr, val & 0xFF))
    val = (MAX_AGGR_NUM << 8) | MAX_AGGR_NUM
    t.write16(REG_MAX_AGGR_NUM, val)


def init_misc01(t) -> None:
    """MISC01 queue/page setup [SRC] usb_halinit.c (the hal_init block before FW):
    _InitQueueReservedPage + _InitQueuePriority + _InitPageBoundary +
    _InitTransferPageSize. Resolved for this card's single bulk-OUT EP."""
    # _InitQueueReservedPage: NPQ first, then RQPN (all pages public, 1-EP).
    t.write8(REG_RQPN_NPQ, 0x00)
    t.write32(REG_RQPN, RQPN_VALUE)
    # _InitQueuePriority: read low 3 bits, OR the 1-EP queue map.
    v = t.read16(REG_TRXDMA_CTRL)
    t.write16(REG_TRXDMA_CTRL, (v & 0x7) | TRXDMA_QUEUE_MAP_1EP)
    # _InitPageBoundary: RX FF boundary.
    t.write16(REG_TRXFF_BNDY + 2, RXFF_BOUNDARY)
    # _InitTransferPageSize: Tx/Rx page size = 128.
    t.write8(REG_PBP, PBP_PAGE_SIZE)


def init_misc02(t) -> None:
    """MISC02 'open the MAC' block [SRC] rtl8188eu_hal_init MISC02 stage — the ~14
    init helpers between InitLLTTable and the turn-on block. Chip-state-dependent
    values (RCR flags, USB-agg config) are resolved to this card's wire-confirmed
    values, like driver1/crystal_cap."""
    # _InitDriverInfoSize
    t.write8(C.REG_RX_DRVINFO_SZ, C.DRVINFO_SZ)
    # _InitInterrupt (HISR clear, HIMR/HIMRE, USB bulk-int select — not full-speed)
    t.write32(C.REG_HISR_88E, 0xFFFFFFFF)
    t.write32(C.REG_HIMR_88E, C.IMR_88E)
    t.write32(C.REG_HIMRE_88E, C.IMR_EX_88E)
    t.write8(C.REG_USB_SPECIAL_OPTION,
             t.read8(C.REG_USB_SPECIAL_OPTION) | C.INT_BULK_SEL)
    # _InitNetworkType (MSR = NT_LINK_AP in REG_CR[17:16])
    v = t.read32(C.REG_CR)
    t.write32(C.REG_CR, (v & ~C.MASK_NETTYPE) | (C.NT_LINK_AP << 16))
    # _InitWMACSetting (STA RCR + accept-all multicast)
    t.write32(C.REG_RCR, C.RCR_STA_INIT)
    t.write32(C.REG_MAR, 0xFFFFFFFF)
    t.write32(C.REG_MAR + 4, 0xFFFFFFFF)
    # _InitAdaptiveCtrl (RRSR, spec SIFS, retry limit)
    v = t.read32(C.REG_RRSR)
    t.write32(C.REG_RRSR, (v & ~C.RATE_BITMAP_ALL) | C.RATE_RRSR_CCK_ONLY_1M)
    t.write16(C.REG_SPEC_SIFS, C.SPEC_SIFS_ADAPTIVE)
    t.write16(C.REG_RL, C.RL_STA)
    # _InitEDCA (SIFS + EDCA AC params)
    t.write16(C.REG_SPEC_SIFS, C.SIFS_VAL)
    t.write16(C.REG_MAC_SPEC_SIFS, C.SIFS_VAL)
    t.write16(C.REG_SIFS_CTX, C.SIFS_VAL)
    t.write16(C.REG_SIFS_TRX, C.SIFS_VAL)
    t.write32(C.REG_EDCA_BE_PARAM, C.EDCA_BE)
    t.write32(C.REG_EDCA_BK_PARAM, C.EDCA_BK)
    t.write32(C.REG_EDCA_VI_PARAM, C.EDCA_VI)
    t.write32(C.REG_EDCA_VO_PARAM, C.EDCA_VO)
    # _InitRetryFunction
    t.write8(C.REG_FWHW_TXQ_CTRL,
             t.read8(C.REG_FWHW_TXQ_CTRL) | C.EN_AMPDU_RTY_NEW)
    t.write8(C.REG_ACKTO, C.ACKTO_VAL)
    # InitUsbAggregationSetting — Tx (TDECTRL BLK_DESC_NUM)
    t.read32(C.REG_TDECTRL)
    t.write32(C.REG_TDECTRL, C.TDECTRL_TXAGG)
    # InitUsbAggregationSetting — Rx, RX_AGG_USB mode: clear DMA-agg, set USB-agg.
    dma = t.read8(C.REG_TRXDMA_CTRL)
    usbv = t.read8(C.REG_USB_SPECIAL_OPTION)
    t.write8(C.REG_TRXDMA_CTRL, dma & ~C.RXDMA_AGG_EN)
    t.write8(C.REG_USB_SPECIAL_OPTION, usbv | C.USB_AGG_EN)
    t.write8(C.REG_RXDMA_AGG_PG_TH + 1, C.RXAGG_USB_TIMEOUT)
    t.write8(C.REG_RXDMA_AGG_PG_TH, C.RXAGG_USB_SIZE)
    # InitBeaconParameters_8188e
    t.write16(C.REG_BCN_CTRL, C.BCN_CTRL_INIT)
    t.write8(C.REG_TBTT_PROHIBIT, C.TBTT_PROHIBIT_SETUP_TIME)
    t.write8(C.REG_TBTT_PROHIBIT + 1, C.TBTT_PROHIBIT_HOLD & 0xFF)
    v = t.read8(C.REG_TBTT_PROHIBIT + 2)
    t.write8(C.REG_TBTT_PROHIBIT + 2, (v & 0xF0) | (C.TBTT_PROHIBIT_HOLD >> 8))
    t.write8(C.REG_DRVERLYINT, C.DRIVER_EARLY_INT_TIME_8188E)
    t.write8(C.REG_BCNDMATIM, C.BCN_DMA_ATIME_INT_TIME_8188E)
    t.write16(C.REG_BCNTCFG, C.BCNTCFG_VAL)
    # _InitBeaconMaxError is empty on 8188e.
    # Enable MACTXEN/MACRXEN (read16 REG_CR, write8 the low byte).
    v = t.read16(C.REG_CR)
    t.write8(C.REG_CR, (v | MACTXEN | MACRXEN) & 0xFF)
    # _InitHardwareDropIncorrectBulkOut
    t.write32(C.REG_TXDMA_OFFSET_CHK,
              t.read32(C.REG_TXDMA_OFFSET_CHK) | C.DROP_DATA_EN)
    # Tx report enable + timer (RATE_ADAPTIVE, !fw_ractrl)
    t.write8(C.REG_TX_RPT_CTRL, t.read8(C.REG_TX_RPT_CTRL) | BIT(1) | BIT(0))
    t.write8(C.REG_TX_RPT_CTRL + 1, 0x02)
    t.write16(C.REG_TX_RPT_TIME, C.TX_RPT_TIME_VAL)
    # Early mode off; no-link MACID; per-AC packet lifetime (TX_MCAST2UNI).
    t.write8(C.REG_EARLY_MODE_CONTROL, 0x00)
    t.write32(C.REG_MACID_NO_LINK_0, 0xFFFFFFFF)
    t.write32(C.REG_MACID_NO_LINK_1, 0xFFFFFFFF)
    t.write16(C.REG_PKT_VO_VI_LIFE_TIME, C.PKT_LIFE_TIME)
    t.write16(C.REG_PKT_BE_BK_LIFE_TIME, C.PKT_LIFE_TIME)


def init_tx_buffer_boundary(t, bndy: int = TX_PAGE_BOUNDARY) -> None:
    """``_InitTxBufferBoundary`` [SRC] usb/usb_halinit.c — program the TX page
    boundary into the beacon/mgmt/loopback/RXFF/TDECTRL boundary registers."""
    t.write8(REG_BCNQ_BDNY, bndy)
    t.write8(REG_MGQ_BDNY, bndy)
    t.write8(REG_WMAC_LBK_BF_HD, bndy)
    t.write8(REG_TRXFF_BNDY, bndy)
    t.write8(REG_TDECTRL + 1, bndy)


def _llt_write(t, address: int, data: int) -> None:
    """``_LLTWrite`` [SRC] rtl8188e_hal_init.c:2815 — one LLT entry, poll to idle."""
    value = (_LLT_WRITE_ACCESS << 30) | ((address & 0xFF) << 8) | (data & 0xFF)
    t.write32(REG_LLT_INIT, value)
    for _ in range(_LLT_POLL_CAP):
        if ((t.read32(REG_LLT_INIT) >> 30) & 0x3) == _LLT_NO_ACTIVE:
            return
    raise RuntimeError("RTL8188EUS: LLT write timeout")


def init_llt(t, bndy: int = TX_PAGE_BOUNDARY,
             last: int = LAST_ENTRY_OF_TX_PKT_BUFFER) -> None:
    """``InitLLTTable`` (direct, non-IOL: this build does not define CONFIG_IOL_LLT)
    [SRC] rtl8188e_hal_init.c:2860 — chain the TX page link-list, ring the rest."""
    for i in range(bndy - 1):
        _llt_write(t, i, i + 1)
    _llt_write(t, bndy - 1, 0xFF)              # end of list
    for i in range(bndy, last):
        _llt_write(t, i, i + 1)
    _llt_write(t, last, bndy)                  # ring buffer: last -> boundary


def invalidate_cam_all(t) -> None:
    """``invalidate_cam_all`` -> HW_VAR_CAM_INVALID_ALL [SRC] rtl8188e_hal_init.c:4064
    — clear every hardware security-cam entry: REG_CAMCMD = CAM_POLLING | CAM_CLR."""
    t.write32(C.REG_CAMCMD, C.CAMCMD_CLEAR_ALL)


def init_misc11_tail(t) -> None:
    """The hal_init MISC11 tail after TX power [SRC] usb_halinit.c:1556-1568: disable
    BAR (REG_BAR_MODE_CTRL) and enable HW sequence numbering (REG_HWSEQ_CTRL=0xFF).

    The two MISC11 helpers around these writes emit nothing on this card:
    ``_InitAntenna_Selection`` is compiled out (CONFIG_ANTENNA_DIVERSITY off), and
    ``PHY_SetRFEReg_8188E`` returns early because this card has no external PA/LNA
    (efuse RFE option 0xCA[3:2] = iPA+iLNA -> ExternalPA_2G == ExternalLNA_2G == 0).
    A board with an external PA/LNA would need PHY_SetRFEReg_8188E ported here."""
    t.write32(C.REG_BAR_MODE_CTRL, C.BAR_MODE_CTRL_DISABLE)
    t.write8(C.REG_HWSEQ_CTRL, 0xFF)
