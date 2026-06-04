"""RTL8821AU (DKMS) M2 MAC init: MAC_REG table + queue/buffer/MISC + REG_CR enable.

Ported 1:1 from `rtl8812au_hal_init` (`hal/rtl8812a/usb/usb_halinit.c`) lines
1510-1556, 8821a-USB path, in on-wire order. `phy_mac_config` applies the
`array_mp_8821a_mac_reg` byte table (PHY_MACConfig8812); `mac_init_misc` runs the
queue/reserved-page/buffer-boundary/page-boundary inits, the MISC02 block, and
finally sets REG_CR MACTXEN|MACRXEN — the last write of M2.

Endpoint topology of the AWUS036ACS: 4 bulk-OUT + 1 bulk-IN + 1 int-IN, HS USB 2.0
-> the 4-out-EP + USB2 branches throughout. Build flags: RX FCS appended (RCR
BIT31), no USB-INT, wifi_spec=0. The STA-mode RCR written here is replaced by the
always-monitor filter in a later (RX) milestone, per the monitor-mode deviation.
# TODO(8812au): 8812 uses the _8812AUsb reserved-page/transfer-page variants, a
# different burst-pkt-len branch (0x456=0x70), and the path-B RF resets at hal-init top.
"""
from __future__ import annotations

from . import constants as C
from .mac_reg_tbl import MAC_REG_TABLE

# --- M2 register addresses [SRC] include/hal_com_reg.h, rtl8812a_spec.h ---
REG_RQPN = 0x0200
REG_RQPN_NPQ = 0x0214
REG_TDECTRL = 0x0208          # REG_DWBCN0_CTRL_8812; +1 (0x0209) = TX-buffer boundary
REG_DWBCN1_CTRL = 0x0228      # [SRC] rtl8812a_spec.h:79
REG_TRXFF_BNDY = 0x0114       # +2 (0x0116) = RX DMA boundary
REG_TRXDMA_CTRL = 0x010C
REG_BCNQ_BDNY = 0x0424
REG_MGQ_BDNY = 0x0425
REG_WMAC_LBK_BF_HD = 0x045D
REG_HIQ_NO_LMT_EN = 0x05A7
REG_RX_DRVINFO_SZ = 0x060F
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
REG_AMPDU_MAX_TIME = 0x0456
REG_AMPDU_MAX_LENGTH = 0x0458
REG_RXDMA_PRO = 0x0290
REG_HT_SINGLE_AMPDU = 0x04C7
REG_RX_PKT_LIMIT = 0x060C
REG_PIFS = 0x0512
REG_MAX_AGGR_NUM = 0x04CA
REG_FAST_EDCA_CTRL = 0x0460

# --- M5 §1 post-tune hal_init tail [SRC] usb_halinit.c (around :1601, :1650-1710) ---
REG_CAMCMD = 0x0670            # invalidate_cam_all: POLLING(BIT31)|CLR(BIT30)
REG_HWSEQ_CTRL = 0x0423
REG_BAR_MODE_CTRL = 0x04CC
REG_QUEUE_CTRL = 0x04C6        # BIT3: 0 = RTS BW follows CCA/secondary-CCA
REG_EARLY_MODE_CONTROL_8812 = 0x02BC  # +3 = Pretx_en for WEP/TKIP
REG_TX_RPT_TIME = 0x04F0       # 2 byte
REG_SDIO_CTRL_8812 = 0x0070
REG_ACLK_MON_M5 = 0x003E
REG_USB_HRPWM = 0xFE58
CAM_INVALIDATE_ALL = 0xC0000000

# REG_CR enable bits [SRC] hal_com_reg.h:1351-1352, 1358-1362
MACTXEN = 0x40
MACRXEN = 0x80
MASK_NETTYPE = 0x30000
NT_LINK_AP = 0x2

RXDMA_AGG_EN = 0x04           # BIT2 of REG_TRXDMA_CTRL
EN_AMPDU_RTY_NEW = 0x80       # BIT7 of REG_FWHW_TXQ_CTRL

# Reserved-page math (4 out-EP, normal) [SRC] rtl8812a_hal.h:214,223-226
_TX_BNDY = C.TX_PAGE_BOUNDARY_8821                 # 0xF8
_PUBQ = C.TX_TOTAL_PAGE_NUMBER_8821 - 8 - 8 - 0 - 4  # 0xE3
RX_DMA_BOUNDARY_8821 = 0x3E7F                       # MAX_RX_DMA_BUFFER(0x3E80)-RSVD(0)-1


def phy_mac_config(t) -> None:
    """PHY_MACConfig8812: the 8821a MAC register table, byte writes in order."""
    for addr, val in MAC_REG_TABLE:
        t.write8(addr, val)


def mac_init_misc(t) -> None:
    """MISC01 + MISC02 init, ending at REG_CR MACTXEN|MACRXEN (last M2 write)."""
    # _InitQueueReservedPage_8821AUsb (HPQ=8, LPQ=8, NPQ=0, EPQ=4, PUBQ=0xE3)
    t.write32(REG_RQPN_NPQ, (0 & 0xFF) | ((4 & 0xFF) << 16))          # _NPQ|_EPQ
    t.write32(REG_RQPN, (8 & 0xFF) | ((8 & 0xFF) << 8)
              | ((_PUBQ & 0xFF) << 16) | (1 << 31))                   # _HPQ|_LPQ|_PUBQ|LD_RQPN

    # _InitTxBufferBoundary_8821AUsb (boundary 0xF8)
    t.write8(REG_BCNQ_BDNY, _TX_BNDY)
    t.write8(REG_MGQ_BDNY, _TX_BNDY)
    t.write8(REG_WMAC_LBK_BF_HD, _TX_BNDY)
    t.write8(REG_TRXFF_BNDY, _TX_BNDY)
    t.write8(REG_TDECTRL + 1, _TX_BNDY)

    # _InitQueuePriority_8812AUsb (4 out-EP): queue->DMA map + HIQ no-limit
    t.write16(REG_TRXDMA_CTRL, (t.read16(REG_TRXDMA_CTRL) & 0x7) | 0xC5A0)
    t.write8(REG_HIQ_NO_LMT_EN, 0xFF)

    # _InitPageBoundary_8812AUsb (8821): RX DMA boundary
    t.write16(REG_TRXFF_BNDY + 2, RX_DMA_BOUNDARY_8821)

    # _InitDriverInfoSize_8812A(DRVINFO_SZ=4)
    t.write8(REG_RX_DRVINFO_SZ, 0x04)

    # _InitInterrupt_8812AU (HIMR only; CONFIG_SUPPORT_USB_INT off)
    t.write32(REG_HIMR0, 0x00000000)
    t.write32(REG_HIMR1, 0x00000000)

    # _InitNetworkType_8812A: MSR = NT_LINK_AP
    t.write32(C.REG_CR, (t.read32(C.REG_CR) & ~MASK_NETTYPE) | ((NT_LINK_AP & 0x3) << 16))

    # _InitWMACSetting_8812A: RCR (STA-mode), multicast filter, RXFLTMAP1
    t.write32(REG_RCR, 0xF40060CE)
    t.write32(REG_MAR, 0xFFFFFFFF)
    t.write32(REG_MAR + 4, 0xFFFFFFFF)
    t.write16(REG_RXFLTMAP1, 0x0400)

    # _InitAdaptiveCtrl_8812AUsb: RRSR (CCK 1M), SIFS, retry limit.
    # The function first reads RRSR to mask off the rate bitmap, then
    # rtw_phydm_set_rrsr -> odm_set_mac_reg does its own read-modify-write under
    # mask 0xFFFFF — so two reads of 0x0440 precede the write.
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

    # init_UsbAggregationSetting_8812A: TX agg (BLK_DESC_NUM=6, mask 0xF shift 4) then RX agg.
    v = (t.read32(REG_TDECTRL) & ~(0xF << 4)) | ((6 & 0xF) << 4)
    t.write32(REG_TDECTRL, v)
    t.write8(REG_DWBCN1_CTRL, (6 << 1) & 0xFF)                        # 8821U DWBCN1
    # RX (RX_AGG_USB): read TRXDMA_CTRL first, then threshold, then write CTRL back.
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

    # _InitBeaconMaxError_8812A(TRUE): CONFIG_ADHOC_WORKAROUND_SETTING on -> 0xFF.
    t.write8(REG_BCN_MAX_ERR, 0xFF)
    # _InitBurstPktLen (8821U, HS USB 2.0)
    t.write8(0xF050, 0x01)
    t.write16(REG_RXDMA_STATUS, 0x7400)
    t.write8(REG_RXDMA_STATUS + 1, 0xF5)
    t.write8(REG_AMPDU_MAX_TIME, 0x5E)
    t.write32(REG_AMPDU_MAX_LENGTH, 0xFFFFFFFF)
    t.write8(REG_USTIME_TSF, 0x50)
    t.write8(REG_USTIME_EDCA, 0x50)
    t.read8(0xFE17)                                                  # HS/FS detect (no write)
    t.write8(REG_RXDMA_PRO, (t.read8(REG_RXDMA_PRO) | 0x1E) & ~0x20)  # BIT4|3|2|1, clear BIT5
    t.write8(C.REG_SYS_FUNC_EN, t.read8(C.REG_SYS_FUNC_EN) & ~(1 << 10) & 0xFF)
    t.write8(REG_HT_SINGLE_AMPDU, t.read8(REG_HT_SINGLE_AMPDU) | 0x80)
    t.write8(REG_RX_PKT_LIMIT, 0x18)
    t.write8(REG_PIFS, 0x00)
    t.write16(REG_MAX_AGGR_NUM, 0x1F1F)                             # 8821U && !wifi_spec
    t.write8(REG_FWHW_TXQ_CTRL, 0x80)
    t.write32(REG_FAST_EDCA_CTRL, 0x03087777)
    t.write8(C.REG_RSV_CTRL, t.read8(C.REG_RSV_CTRL) | (1 << 5) | (1 << 6))
    # ARFR rate tables (ARFR0/1/2/4 + their +4 halves)
    t.write32(0x0444, 0x00000010)
    t.write32(0x0448, 0xFFFFF000)
    t.write32(0x044C, 0x00000010)
    t.write32(0x0450, 0x003FF000)
    t.write32(0x048C, 0x00000015)
    t.write32(0x0490, 0x003FF000)
    t.write32(0x0494, 0x00000015)
    t.write32(0x0498, 0xFFCFF000)

    # Init CR MACTXEN|MACRXEN after RxFF boundary (last write of M2).
    t.write8(C.REG_CR, t.read8(C.REG_CR) | MACTXEN | MACRXEN)


# ---------------------------------------------------------------------------
# M5 §1: post-tune hal_init "turn-on" tail (rtl8812au_hal_init after the channel
# tune, [SRC] usb_halinit.c). The vendor order is: §1a (security + MISC11) ->
# rtl8812_InitHalDm (§2, dig.init_hal_dm) -> §1b (the turn-on writes). The two
# halves bracket InitHalDm, so the driver / verify_pcap call them in that order.
# wifi_spec-gated (FAST_EDCA=0) and commented (IQK/PWtrack/LCK) steps are no-ops
# here, as are the CONFIG_XMIT_ACK FWHW_TXQ BIT12 set — none reach the wire.
# ---------------------------------------------------------------------------

def hal_init_misc_pre(t) -> None:
    """§1a: invalidate_cam_all + MISC11 (HW-seq, BAR-disable, NAV limit)."""
    t.write32(REG_CAMCMD, CAM_INVALIDATE_ALL)   # invalidate_cam_all (poll+clear, 1 write)
    t.write8(REG_HWSEQ_CTRL, 0xFF)              # default-enable HW sequence number
    t.write32(REG_BAR_MODE_CTRL, 0x0201FFFF)    # disable BAR
    t.write8(0x0652, 0x00)                       # NAV limit


def hal_init_misc_post(t) -> None:
    """§1b: turn-on writes after InitHalDm (RTS-BW, Tx-report, pre-Tx, USB reset)."""
    t.write8(REG_QUEUE_CTRL, t.read8(REG_QUEUE_CTRL) & 0xF7)  # RTS BW follows CCA
    t.write8(REG_FWHW_TXQ_CTRL + 1, 0x0F)        # enable Tx report
    t.write8(REG_EARLY_MODE_CONTROL_8812 + 3, 0x01)  # Pretx_en (WEP/TKIP SEC)
    t.write16(REG_TX_RPT_TIME, 0x3DF0)
    t.write8(REG_SDIO_CTRL_8812, 0x00)           # reset USB mode-switch setting
    t.write8(REG_ACLK_MON_M5, 0x00)
    t.write8(REG_USB_HRPWM, 0x00)
