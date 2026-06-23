"""RTL8812AU M2 MAC init: MAC_REG table (phy_cond) + queue/buffer/MISC + REG_CR enable.

Ported 1:1 from ``rtl8812au_hal_init`` (``hal/rtl8812a/usb/usb_halinit.c``), 8812AU-USB
path, in on-wire order: PHY_MACConfig8812 walks ``array_mp_8812a_mac_reg`` (a phy_cond
table — unlike the 8821's flat pairs it carries an IF-USB/ELSE branch on reg 0x11 —
applied with write8 by odm_config_mac_8812a), then the MISC01/MISC02 inits run and the
last write of M2 is REG_CR MACTXEN|MACRXEN.

The 8812 deltas vs the 8821 (all from the ``_8812AUsb`` / 8812 ``else`` branches):
  * reserved page: HPQ=LPQ=0x10, NPQ=0, PUBQ=0xD6 (no EPQ; NPQ is a byte write);
  * TX buffer boundary 0xF7 (TX_PAGE_BOUNDARY_8812);
  * NEW _InitTransferPageSize: REG_PBP = _PSTX(PBP_512) = 0x30;
  * USB agg: TX BLK_DESC_NUM=1 (no DWBCN1 write), RX threshold 0x0608;
  * burst-pkt-len: AMPDU_MAX_TIME=0x70, MAX_AGGR tail clears FWHW_TXQ BIT7 (no FAST_EDCA).
Everything else is the shared ``_8812A`` functions — identical wire to the 8821 port.
The STA-mode RCR written here is replaced by the always-monitor filter at M5.

Endpoint topology of the AWUS036ACH: 3 bulk-OUT (0x02/0x03/0x04) + 1 bulk-IN (0x81),
HS USB 2.0 -> OutEpNumber==3, so the queue/priority init takes the 3-out-EP branch.
"""
from __future__ import annotations

from ..rtl88xxau_base import registers as R
from ..rtl88xxau_base.phy_cond import JaguarParams, apply_table
from .constants import TX_PAGE_BOUNDARY_8812, TX_TOTAL_PAGE_NUMBER_8812
from .mac_reg_tbl import MAC_REG

# --- M2 register addresses [SRC] include/hal_com_reg.h, rtl8812a_spec.h ---
REG_RQPN = 0x0200
REG_RQPN_NPQ = 0x0214
REG_TDECTRL = 0x0208          # REG_DWBCN0_CTRL_8812; +1 (0x0209) = TX-buffer boundary
REG_TRXFF_BNDY = 0x0114       # +2 (0x0116) = RX DMA boundary
REG_TRXDMA_CTRL = 0x010C
REG_BCNQ_BDNY = 0x0424
REG_MGQ_BDNY = 0x0425
REG_WMAC_LBK_BF_HD = 0x045D
REG_HIQ_NO_LMT_EN = 0x05A7
REG_RX_DRVINFO_SZ = 0x060F
REG_PBP = 0x0104              # _InitTransferPageSize_8812AUsb (8812-only)
REG_HIMR0 = 0x00B0
REG_HIMR1 = 0x00B8
REG_RCR = 0x0608
REG_MAR = 0x0620
REG_RXFLTMAP1 = 0x06A2
REG_RRSR = 0x0440
REG_SPEC_SIFS = 0x0428
REG_RETRY_LIMIT = 0x042A
REG_MAC_SPEC_SIFS = 0x063A
REG_SIFS_CTX = 0x0514
REG_SIFS_TRX = 0x0516
REG_EDCA_VO_PARAM = 0x0500
REG_EDCA_VI_PARAM = 0x0504
REG_EDCA_BE_PARAM = 0x0508
REG_EDCA_BK_PARAM = 0x050C
REG_USTIME_TSF = 0x055C
REG_USTIME_EDCA = 0x0638
REG_FWHW_TXQ_CTRL = 0x0420
REG_ACKTO = 0x0640
REG_RXDMA_AGG_PG_TH = 0x0280
REG_BCN_CTRL = 0x0550
REG_TBTT_PROHIBIT = 0x0540
REG_DRVERLYINT = 0x0558
REG_BCNDMATIM = 0x0559
REG_BCNTCFG = 0x0510
REG_BCN_MAX_ERR = 0x055D
REG_RXDMA_STATUS = 0x0288
REG_AMPDU_MAX_TIME_8812 = 0x0456
REG_AMPDU_MAX_LENGTH_8812 = 0x0458
REG_RXDMA_PRO_8812 = 0x0290
REG_HT_SINGLE_AMPDU_8812 = 0x04C7
REG_RX_PKT_LIMIT = 0x060C
REG_PIFS = 0x0512
REG_MAX_AGGR_NUM = 0x04CA
REG_ARFR0_8812 = 0x0444
REG_ARFR1_8812 = 0x044C
REG_ARFR2_8812 = 0x048C
REG_ARFR3_8812 = 0x0494

# REG_CR enable bits [SRC] hal_com_reg.h
MACTXEN = 0x40
MACRXEN = 0x80
MASK_NETTYPE = 0x30000
NT_LINK_AP = 0x2
RXDMA_AGG_EN = 0x04           # BIT2 of REG_TRXDMA_CTRL
EN_AMPDU_RTY_NEW = 0x80       # BIT7 of REG_FWHW_TXQ_CTRL
LD_RQPN = 1 << 31

# Reserved-page math [SRC] rtl8812a_hal.h + _InitQueueReservedPage_8812AUsb. The selected
# out-EP queues (HQ|LQ|NQ here) take their NORMAL_PAGE_NUM_* and PubQ gets the remainder.
_NORMAL_HPQ = 0x10            # NORMAL_PAGE_NUM_HPQ_8812
_NORMAL_LPQ = 0x10            # NORMAL_PAGE_NUM_LPQ_8812
_NORMAL_NPQ = 0x00           # NORMAL_PAGE_NUM_NPQ_8812
_PUBQ = TX_TOTAL_PAGE_NUMBER_8812 - _NORMAL_HPQ - _NORMAL_LPQ - _NORMAL_NPQ   # 0xD6
_TX_BNDY = TX_PAGE_BOUNDARY_8812   # 0xF7
RX_DMA_BOUNDARY_8812 = 0x3E7F  # MAX_RX_DMA_BUFFER(0x3E80) - RSVD(0) - 1


def _mac_write(t, addr: int, val: int) -> None:
    t.write8(addr, val & 0xFF)              # odm_config_mac_8812a takes a u8


def phy_mac_config(t) -> None:
    """PHY_MACConfig8812: walk the 8812a MAC table (phy_cond IF/ELSE) with byte writes."""
    apply_table(MAC_REG, lambda a, v: _mac_write(t, a, v), JaguarParams())


def mac_init_misc(t) -> None:
    """MISC01 + MISC02, ending at REG_CR MACTXEN|MACRXEN (last M2 write)."""
    # _InitQueueReservedPage_8812AUsb (4 out-EP: HPQ=LPQ=0x10, NPQ=0, PUBQ=0xD8; no EPQ)
    t.write8(REG_RQPN_NPQ, _NORMAL_NPQ & 0xFF)                        # _NPQ (byte write)
    t.write32(REG_RQPN, ((_NORMAL_HPQ & 0xFF) | ((_NORMAL_LPQ & 0xFF) << 8)
                         | ((_PUBQ & 0xFF) << 16) | LD_RQPN))         # _HPQ|_LPQ|_PUBQ|LD_RQPN

    # _InitTxBufferBoundary_8812AUsb (boundary 0xF9)
    t.write8(REG_BCNQ_BDNY, _TX_BNDY)
    t.write8(REG_MGQ_BDNY, _TX_BNDY)
    t.write8(REG_WMAC_LBK_BF_HD, _TX_BNDY)
    t.write8(REG_TRXFF_BNDY, _TX_BNDY)
    t.write8(REG_TDECTRL + 1, _TX_BNDY)

    # _InitQueuePriority_8812AUsb (OutEpNumber==3 -> _InitNormalChipThreeOutEpPriority):
    # be=LOW, bk=LOW, vi=NORMAL, vo=HIGH, mgt=HIGH, hi=HIGH ->
    #   _HIQ(3)<<14 | _MGQ(3)<<12 | _BKQ(1)<<10 | _BEQ(1)<<8 | _VIQ(2)<<6 | _VOQ(3)<<4 = 0xF5B0
    # The 3-EP path does NOT run init_hi_queue_config, so there is no REG_HIQ_NO_LMT_EN write
    # (that belongs to the 4-EP path only).
    t.write16(REG_TRXDMA_CTRL, (t.read16(REG_TRXDMA_CTRL) & 0x7) | 0xF5B0)

    # _InitPageBoundary_8812AUsb: RX DMA boundary
    t.write16(REG_TRXFF_BNDY + 2, RX_DMA_BOUNDARY_8812)

    # _InitTransferPageSize_8812AUsb (8812-only): REG_PBP = _PSTX(PBP_512) = 0x3 << 4
    t.write8(REG_PBP, 0x30)

    # _InitDriverInfoSize_8812A(DRVINFO_SZ=4)
    t.write8(REG_RX_DRVINFO_SZ, 0x04)

    # _InitInterrupt_8812AU (HIMR only; CONFIG_SUPPORT_USB_INT off)
    t.write32(REG_HIMR0, 0x00000000)
    t.write32(REG_HIMR1, 0x00000000)

    # _InitNetworkType_8812A: MSR = NT_LINK_AP
    t.write32(R.REG_CR, (t.read32(R.REG_CR) & ~MASK_NETTYPE) | ((NT_LINK_AP & 0x3) << 16))

    # _InitWMACSetting_8812A: STA-mode RCR (HW_VAR_RCR), multicast filter, RXFLTMAP1.
    # The RCR is replaced by the always-monitor RCR at M5; this is the init-time value.
    t.write32(REG_RCR, 0xF40060CE)
    t.write32(REG_MAR, 0xFFFFFFFF)
    t.write32(REG_MAR + 4, 0xFFFFFFFF)
    t.write16(REG_RXFLTMAP1, 0x0420)         # BIT10 ps-poll | BIT5 NDPA (CONFIG_BEAMFORMING)

    # _InitAdaptiveCtrl_8812AUsb: RRSR (read to mask the rate bitmap, then RMW under
    # 0xFFFFF), SIFS, retry limit.
    t.read32(REG_RRSR)
    t.write32(REG_RRSR, (t.read32(REG_RRSR) & ~0xFFFFF) | 0xFFFF1)
    t.write16(REG_SPEC_SIFS, 0x1010)
    t.write16(REG_RETRY_LIMIT, 0x3030)

    # _InitEDCA_8812AUsb
    t.write16(REG_SPEC_SIFS, 0x100A)
    t.write16(REG_MAC_SPEC_SIFS, 0x100A)
    t.write16(REG_SIFS_CTX, 0x100A)
    t.write16(REG_SIFS_TRX, 0x100A)
    t.write32(REG_EDCA_BE_PARAM, 0x005EA42B)
    t.write32(REG_EDCA_BK_PARAM, 0x0000A44F)
    t.write32(REG_EDCA_VI_PARAM, 0x005EA324)
    t.write32(REG_EDCA_VO_PARAM, 0x002FA226)
    t.write8(REG_USTIME_TSF, 0x50)
    t.write8(REG_USTIME_EDCA, 0x50)

    # _InitRetryFunction_8812A
    t.write8(REG_FWHW_TXQ_CTRL, t.read8(REG_FWHW_TXQ_CTRL) | EN_AMPDU_RTY_NEW)
    t.write8(REG_ACKTO, 0x80)

    # init_UsbAggregationSetting_8812A: TX agg (BLK_DESC_NUM = UsbTxAggDescNum = 1 for
    # 8812AU; no DWBCN1 write), then RX agg (RX_AGG_USB). On USB-2.0 HS (not super-speed)
    # without CONFIG_PREALLOC_RX_SKB_BUFFER the size/timeout are 0x5 / 0x20 (the FIFO-overflow
    # reduction tuning, usb_halinit.c:191-192) -> REG_RXDMA_AGG_PG_TH = 0x2005.
    v = (t.read32(REG_TDECTRL) & ~(0xF << 4)) | ((0x01 & 0xF) << 4)
    t.write32(REG_TDECTRL, v)
    value_dma = t.read8(REG_TRXDMA_CTRL) | RXDMA_AGG_EN
    t.write16(REG_RXDMA_AGG_PG_TH, 0x05 | (0x20 << 8))               # rxagg_usb_size | timeout<<8
    t.write8(REG_TRXDMA_CTRL, value_dma)

    # _InitBeaconParameters_8812A
    t.write16(REG_BCN_CTRL, 0x1010)
    t.write8(REG_TBTT_PROHIBIT, 0x04)
    t.write8(REG_TBTT_PROHIBIT + 1, 0x64)
    t.write8(REG_TBTT_PROHIBIT + 2, (t.read8(REG_TBTT_PROHIBIT + 2) & 0xF0) | 0x00)
    t.write8(REG_DRVERLYINT, 0x05)
    t.write8(REG_BCNDMATIM, 0x02)
    t.write16(REG_BCNTCFG, 0x4413)

    # _InitBeaconMaxError_8812A(TRUE)
    t.write8(REG_BCN_MAX_ERR, 0xFF)

    # _InitBurstPktLen (HS USB 2.0, 8812 branch)
    t.write8(0xF050, 0x01)
    t.write16(REG_RXDMA_STATUS, 0x7400)
    t.write8(REG_RXDMA_STATUS + 1, 0xF5)
    t.write8(REG_AMPDU_MAX_TIME_8812, 0x70)                          # 8812 (8821U = 0x5e)
    t.write32(REG_AMPDU_MAX_LENGTH_8812, 0xFFFFFFFF)
    t.write8(REG_USTIME_TSF, 0x50)
    t.write8(REG_USTIME_EDCA, 0x50)
    # speed check: 8812 reads 0xff bit7 (SS marker); HS USB2 -> bit7 set -> burst 512B.
    speed = t.read8(0x00FF)
    if speed & 0x80:                                                 # USB2/1.1
        temp = t.read8(0xFE17)
        if ((temp >> 4) & 0x03) == 0:
            pro = t.read8(REG_RXDMA_PRO_8812)
            t.write8(REG_RXDMA_PRO_8812, (pro | 0x1E) & ~0x20)       # burst 512B (BIT4|3|2|1, ~BIT5)
        else:
            pro = t.read8(REG_RXDMA_PRO_8812)
            t.write8(REG_RXDMA_PRO_8812, (pro | 0x2E) & ~0x10)       # burst 64B (BIT5|3|2|1, ~BIT4)
    else:                                                            # USB3 (not on this HS card)
        pro = t.read8(REG_RXDMA_PRO_8812)
        t.write8(REG_RXDMA_PRO_8812, (pro | 0x0E) & ~0x30)           # burst 1k (BIT3|2|1, ~BIT5|4)
        t.write8(0xF008, t.read8(0xF008) & 0xE7)
    # reset 8051 (REG_SYS_FUNC_EN is read/written as the low byte; ~BIT(10)
    # has no effect on it, so this rewrites the low byte — matches the vendor).
    t.write8(R.REG_SYS_FUNC_EN, t.read8(R.REG_SYS_FUNC_EN) & ~(1 << 10) & 0xFF)
    t.write8(REG_HT_SINGLE_AMPDU_8812, t.read8(REG_HT_SINGLE_AMPDU_8812) | 0x80)
    t.write8(REG_RX_PKT_LIMIT, 0x18)
    t.write8(REG_PIFS, 0x00)
    # 8812 (!8821U) MAX_AGGR tail: clear FWHW_TXQ BIT7 (no FAST_EDCA write).
    t.write16(REG_MAX_AGGR_NUM, 0x1F1F)
    t.write8(REG_FWHW_TXQ_CTRL, t.read8(REG_FWHW_TXQ_CTRL) & ~0x80)
    # AMPDUBurstMode is FALSE on the 8812AU -> REG_AMPDU_BURST_MODE (0x4BC) not written.
    t.write8(R.REG_RSV_CTRL, t.read8(R.REG_RSV_CTRL) | (1 << 5) | (1 << 6))
    # ARFB tables 9-12 (ARFR0/1/2/3 + their +4 halves) — same as the 8821.
    t.write32(REG_ARFR0_8812, 0x00000010)
    t.write32(REG_ARFR0_8812 + 4, 0xFFFFF000)
    t.write32(REG_ARFR1_8812, 0x00000010)
    t.write32(REG_ARFR1_8812 + 4, 0x003FF000)
    t.write32(REG_ARFR2_8812, 0x00000015)
    t.write32(REG_ARFR2_8812 + 4, 0x003FF000)
    t.write32(REG_ARFR3_8812, 0x00000015)
    t.write32(REG_ARFR3_8812 + 4, 0xFFCFF000)

    # Init CR MACTXEN|MACRXEN after RxFF boundary (last write of M2).
    t.write8(R.REG_CR, t.read8(R.REG_CR) | MACTXEN | MACRXEN)


# --- M5 §1: post-tune hal_init "turn-on" tail (rtl8812au_hal_init after the channel
# tune, [SRC] usb_halinit.c:1593-1672). The vendor order is §1a (CAM + MISC) ->
# rtl8812_InitHalDm (dig.init_hal_dm) -> §1b (turn-on writes). The two halves bracket
# InitHalDm. The 8812 omits the 8821's USB_HRPWM write. ------------------------------
REG_CAMCMD = 0x0670
REG_HWSEQ_CTRL = 0x0423
REG_BAR_MODE_CTRL = 0x04CC
REG_NAV_CTRL = 0x0652
REG_QUEUE_CTRL = 0x04C6
REG_EARLY_MODE_CONTROL_8812 = 0x02BC   # +3 = Pretx_en
REG_TX_RPT_TIME = 0x04F0
REG_SDIO_CTRL_8812 = 0x0070
REG_ACLK_MON = 0x003E
CAM_INVALIDATE_ALL = 0xC0000000


def hal_init_misc_pre(t) -> None:
    """§1a: invalidate_cam_all + HW-seq default + BAR-disable + NAV limit."""
    t.write32(REG_CAMCMD, CAM_INVALIDATE_ALL)   # invalidate_cam_all
    t.write8(REG_HWSEQ_CTRL, 0xFF)              # default-enable HW sequence number
    t.write32(REG_BAR_MODE_CTRL, 0x0201FFFF)    # disable BAR
    t.write8(REG_NAV_CTRL, 0x00)                # NAV limit


def hal_init_misc_post(t) -> None:
    """§1b: turn-on writes after InitHalDm (RTS-BW, Tx-report, pre-Tx, USB reset)."""
    t.write8(REG_QUEUE_CTRL, t.read8(REG_QUEUE_CTRL) & 0xF7)  # RTS BW follows CCA
    t.write8(REG_FWHW_TXQ_CTRL + 1, 0x0F)        # enable Tx report
    t.write8(REG_EARLY_MODE_CONTROL_8812 + 3, 0x01)  # Pretx_en (WEP/TKIP SEC)
    t.write16(REG_TX_RPT_TIME, 0x3DF0)
    t.write8(REG_SDIO_CTRL_8812, 0x00)           # reset USB mode-switch setting
    t.write8(REG_ACLK_MON, 0x00)
