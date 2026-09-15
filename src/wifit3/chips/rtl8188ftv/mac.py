"""RTL8188FTV MAC init + monitor entry.

Mirror of:
* `rtl8xxxu_init_mac` — `core.c:2166-2206` (the MAC table loop + MAX_AGGR)
* `rtl8xxxu_init_queue_reserved_page` / `init_queue_priority` (queue init)
* 8188F `enable_rf` / `disable_rf` — `8188f.c:1582-1631`
* `rtl8xxxu_start` tail — `core.c:7494-7501` (RX filt map + AGC IGI)
* `rtl8xxxu_configure_filter` — `core.c:6920-7971` (RCR filter, monitor mode)

The MAC init table writes 8-bit values; the MAX_AGGR section is
chip-specific — for 8188F it falls through to `default: break`
(core.c:2202) with no register write.

Warm-detect: probe REG_MCU_FW_DL's MCU_WINT_INIT_READY bit first — it's
readable in both states, while REG_CR only answers once power_on has run
(the kernel never reads CR before power-on, rtl8xxxu core vs 8188f).
Only when FW-running prompts a REG_CR probe (to confirm MAC enabled),
matching how EUS-style warm-detect works without a cold REG_CR read.
"""
from __future__ import annotations

import logging

from . import phy
from .constants import (
    AUTO_LLT_INIT_LLT,
    BEACON_DISABLE_TSF_UPDATE,
    BEACON_DMA_ATIME_INT_TIME,
    CAM_CMD_POLLING,
    CR_MAC_RX_ENABLE,
    CR_MAC_TX_ENABLE,
    FPGA0_RF_ANTSW,
    FPGA0_RF_ANTSWB,
    FPGA0_RF_BD_CTRL_SHIFT,
    FPGA0_RF_PAPE,
    FPGA0_RF_TRSW,
    FPGA0_RF_TRSWB,
    GPIO_MUXCFG_IO_SEL_ENBT,
    HT_SINGLE_AMPDU_ENABLE,
    MCU_WINT_INIT_READY,
    OFDM0_X_AGC_CORE1_IGI_MASK,
    PAGE_NUM_HI_PQ_8188F,
    PAGE_NUM_NORM_PQ_8188F,
    PBP_PAGE_SIZE_256,
    PBP_PAGE_SIZE_RX_SHIFT,
    PBP_PAGE_SIZE_TX_SHIFT,
    RCR_ACCEPT_AP,
    RCR_ACCEPT_BCAST,
    RCR_ACCEPT_CTRL_FRAME,
    RCR_ACCEPT_MCAST,
    RCR_ACCEPT_MGMT_FRAME,
    RCR_ACCEPT_PHYS_MATCH,
    RCR_ACCEPT_PM,
    RCR_APPEND_ICV,
    RCR_APPEND_MIC,
    RCR_APPEND_PHYSTAT,
    RCR_CHECK_BSSID_BEACON,
    RCR_CHECK_BSSID_MATCH,
    RCR_HTC_LOC_CTRL,
    REG_ACKTO,
    REG_AGGLEN_LMT,
    REG_AMPDU_MAX_TIME_8723B,
    REG_AUTO_LLT,
    REG_BAR_MODE_CTRL,
    REG_BEACON_CTRL,
    REG_BEACON_DMA_TIME,
    REG_BEACON_TCFG,
    REG_CAM_CMD,
    REG_CCK_PD_THRESH,
    REG_CR,
    REG_DARFRC,
    REG_DWBCN1_CTRL_8723B,
    REG_EDCA_BE_PARAM,
    REG_EDCA_BK_PARAM,
    REG_EDCA_VI_PARAM,
    REG_EDCA_VO_PARAM,
    REG_FAST_EDCA_CTRL,
    REG_FPGA0_IQK,
    REG_FPGA0_RF_MODE,
    REG_FPGA0_TX_INFO,
    REG_FPGA0_XA_RF_INT_OE,
    REG_FPGA0_XAB_RF_SW_CTRL,
    REG_FWHW_TXQ_CTRL,
    REG_GPIO_MUXCFG,
    REG_HISR0,
    REG_HISR1,
    REG_HT_SINGLE_AMPDU_8723B,
    REG_HWSEQ_CTRL,
    REG_MAC_SPEC_SIFS,
    REG_MAX_AGGR_NUM,
    REG_MCU_FW_DL,
    REG_NHM_TH3_TO_TH0_8723B,
    REG_NHM_TH7_TO_TH4_8723B,
    REG_NHM_TH9_TH10_8723B,
    REG_NHM_TIMER_8723B,
    REG_OFDM0_FA_RSTC,
    REG_OFDM0_XA_AGC_CORE1,
    REG_PBP,
    REG_PIFS,
    REG_PKT_BE_BK_LIFE_TIME,
    REG_PKT_VO_VI_LIFE_TIME,
    REG_RARFRC,
    REG_RCR,
    REG_RESPONSE_RATE_SET,
    REG_RETRY_LIMIT,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_RSV_CTRL,
    REG_RXFLTMAP0,
    REG_RXFLTMAP1,
    REG_RXFLTMAP2,
    REG_RX_DRVINFO_SZ,
    REG_RXDMA_AGG_PG_TH,
    REG_RXDMA_PRO_8723B,
    REG_RX_PKT_LIMIT,
    REG_SIFS_CCK,
    REG_SIFS_OFDM,
    REG_SPEC_SIFS,
    REG_TBTT_PROHIBIT,
    REG_TDECTRL,
    REG_TXDMA_OFFSET_CHK,
    REG_TXPKTBUF_BCNQ_BDNY,
    REG_TXPKTBUF_MGQ_BDNY,
    REG_TXPKTBUF_WMAC_LBK_BF_HD,
    REG_TX_REPORT_CTRL,
    REG_TX_REPORT_TIME,
    REG_TRXDMA_CTRL,
    REG_TRXFF_BNDY,
    REG_USTIME_EDCA,
    REG_USTIME_TSF_8723B,
    RESPONSE_RATE_BITMAP_ALL,
    RESPONSE_RATE_RRSR_CCK_ONLY_1M,
    RXDMA_PRO_DMA_BURST_CNT,
    RXDMA_PRO_DMA_BURST_SIZE,
    RXDMA_PRO_DMA_MODE,
    RXDMA_USB_AGG_ENABLE,
    RSV_CTRL_DIS_PRST,
    RSV_CTRL_WLOCK_1C,
    RQPN_HI_PQ_SHIFT,
    RQPN_LOAD,
    RQPN_NPQ_SHIFT,
    RQPN_PUB_PQ_SHIFT,
    TRXDMA_CTRL_BEQ_SHIFT,
    TRXDMA_CTRL_BKQ_SHIFT,
    TRXDMA_CTRL_HIQ_SHIFT,
    TRXDMA_CTRL_MGQ_SHIFT,
    TRXDMA_CTRL_RXDMA_AGG_EN,
    TRXDMA_CTRL_VIQ_SHIFT,
    TRXDMA_CTRL_VOQ_SHIFT,
    TRXDMA_QUEUE_HIGH,
    TRXDMA_QUEUE_NORMAL,
    TRXFF_BOUNDARY_8188F,
    TXDMA_OFFSET_DROP_DATA_EN,
    TX_REPORT_CTRL_TIMER_ENABLE,
    TX_TOTAL_PAGE_NUM_8188F,
)
from .transport import RTL8188FTVTransport

logger = logging.getLogger(__name__)

# ---- MAC init table (8188f.c:17-305) --------------------------------
# (reg, val) — 8-bit writes, terminated by (0xFFFF, 0xFF).
_MAC_INIT_TABLE: list[tuple[int, int]] = [
    (0x0024, 0xDF), (0x0025, 0x07), (0x002B, 0x1C), (0x0283, 0x20),
    (0x0421, 0x0F), (0x0428, 0x0A), (0x0429, 0x10), (0x0430, 0x00),
    (0x0431, 0x00), (0x0432, 0x00), (0x0433, 0x01), (0x0434, 0x04),
    (0x0435, 0x05), (0x0436, 0x07), (0x0437, 0x08), (0x043C, 0x04),
    (0x043D, 0x05), (0x043E, 0x07), (0x043F, 0x08), (0x0440, 0x5D),
    (0x0441, 0x01), (0x0442, 0x00), (0x0444, 0x10), (0x0445, 0x00),
    (0x0446, 0x00), (0x0447, 0x00), (0x0448, 0x00), (0x0449, 0xF0),
    (0x044A, 0x0F), (0x044B, 0x3E), (0x044C, 0x10), (0x044D, 0x00),
    (0x044E, 0x00), (0x044F, 0x00), (0x0450, 0x00), (0x0451, 0xF0),
    (0x0452, 0x0F), (0x0453, 0x00), (0x0456, 0x5E), (0x0460, 0x44),
    (0x0461, 0x44), (0x04BC, 0xC0), (0x04C8, 0xFF), (0x04C9, 0x08),
    (0x04CC, 0xFF), (0x04CD, 0xFF), (0x04CE, 0x01), (0x0500, 0x26),
    (0x0501, 0xA2), (0x0502, 0x2F), (0x0503, 0x00), (0x0504, 0x28),
    (0x0505, 0xA3), (0x0506, 0x5E), (0x0507, 0x00), (0x0508, 0x2B),
    (0x0509, 0xA4), (0x050A, 0x5E), (0x050B, 0x00), (0x050C, 0x4F),
    (0x050D, 0xA4), (0x050E, 0x00), (0x050F, 0x00), (0x0512, 0x1C),
    (0x0514, 0x0A), (0x0516, 0x0A), (0x0525, 0x4F), (0x0550, 0x10),
    (0x0551, 0x10), (0x0559, 0x02), (0x055C, 0x28), (0x055D, 0xFF),
    (0x0605, 0x30), (0x0608, 0x0E), (0x0609, 0x2A), (0x0620, 0xFF),
    (0x0621, 0xFF), (0x0622, 0xFF), (0x0623, 0xFF), (0x0624, 0xFF),
    (0x0625, 0xFF), (0x0626, 0xFF), (0x0627, 0xFF), (0x0638, 0x28),
    (0x063C, 0x0A), (0x063D, 0x0A), (0x063E, 0x0E), (0x063F, 0x0E),
    (0x0640, 0x40), (0x0642, 0x40), (0x0643, 0x00), (0x0652, 0xC8),
    (0x066E, 0x05), (0x0700, 0x21), (0x0701, 0x43), (0x0702, 0x65),
    (0x0703, 0x87), (0x0708, 0x21), (0x0709, 0x43), (0x070A, 0x65),
    (0x070B, 0x87),
    (0xFFFF, 0xFF),
]

# ---- Warm detect ----------------------------------------------------


def is_chip_warm(t: RTL8188FTVTransport) -> bool:
    """True if a previous wifit3 session left the chip FW-running + MAC-enabled.

    8188F's REG_CR only answers after power_on, so probe the FW state first
    via REG_MCU_FW_DL (readable cold); only touch REG_CR once MCU_WINT_INIT_READY
    says the 8051 booted an image. Returns False on any USB read error — safer
    to run a full cold boot than to skip init the chip still needs.
    """
    try:
        mcu_fw = t.read32(REG_MCU_FW_DL)
    except (IOError, OSError):
        return False
    if not mcu_fw & MCU_WINT_INIT_READY:
        return False
    try:
        cr = t.read32(REG_CR)
    except (IOError, OSError):
        return False
    mac_enabled = (cr & (CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE)) == (
        CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE
    )
    return bool(mcu_fw & MCU_WINT_INIT_READY) and mac_enabled


# ---- MAC table replay -----------------------------------------------


def apply_mac_init_table(t: RTL8188FTVTransport) -> None:
    """Replay the MAC init table (8188f.c:17-305).

    Each entry is a single 8-bit write.  MAX_AGGR for 8188F is the
    `default: break` case (core.c:2202) — no register write.
    """
    for reg, val in _MAC_INIT_TABLE:
        if reg == 0xFFFF and val == 0xFF:
            break
        t.write8(reg, val & 0xFF)


# ---- M2 pre-FW TX queue setup --------------------------------------
# Kernel runs these inside `rtl8xxxu_init_device` right after power-on,
# BEFORE firmware upload (core.c:4055-4058): they route the TX queues to
# DMA channels and partition the TX page FIFO. With them unset the chip
# NAKs bulk-OUT frames (the live errno-110 symptom pre-fix).


def init_queue_reserved_page(t: RTL8188FTVTransport) -> None:
    """Port of `rtl8xxxu_init_queue_reserved_page` (core.c:3918-3941).

    8188F tags HI=0x0c + NORM=0x02 pages; PUB gets the remainder. Matches
    capture-1 ops 945-946 (REG_RQPN_NPQ=0x02, REG_RQPN=0x80e8000c).
    """
    hq = PAGE_NUM_HI_PQ_8188F
    nq = PAGE_NUM_NORM_PQ_8188F
    t.write32(REG_RQPN_NPQ, nq << RQPN_NPQ_SHIFT)
    pubq = TX_TOTAL_PAGE_NUM_8188F - hq - nq - 1
    val32 = RQPN_LOAD | (hq << RQPN_HI_PQ_SHIFT) | (pubq << RQPN_PUB_PQ_SHIFT)
    t.write32(REG_RQPN, val32)


def init_queue_priority_2ep(t: RTL8188FTVTransport) -> None:
    """Port of the 2-bulk-OUT case from `rtl8xxxu_init_queue_priority`.

    Routes VO/VI/MGNT/HIGH to the HIGH lane (EP 0x02 = our MGMT pipe) and
    BE/BK to NORMAL (EP 0x03). Writes REG_TRXDMA_CTRL=0xfaf0, capture-1
    op 948 (RMW preserves the low 3 bits, as init_aggregation uses bit 2).
    """
    hi = TRXDMA_QUEUE_HIGH
    lo = TRXDMA_QUEUE_NORMAL
    val16 = t.read16(REG_TRXDMA_CTRL) & 0x7
    val16 |= (
        (hi << TRXDMA_CTRL_VOQ_SHIFT)
        | (hi << TRXDMA_CTRL_VIQ_SHIFT)
        | (lo << TRXDMA_CTRL_BEQ_SHIFT)
        | (lo << TRXDMA_CTRL_BKQ_SHIFT)
        | (hi << TRXDMA_CTRL_MGQ_SHIFT)
        | (hi << TRXDMA_CTRL_HIQ_SHIFT)
    )
    t.write16(REG_TRXDMA_CTRL, val16)


def set_trxff_rx_page_boundary(t: RTL8188FTVTransport) -> None:
    """Write the pre-FW RX page boundary (REG_TRXFF_BNDY+2 = 0x3f7f)."""
    t.write16(REG_TRXFF_BNDY + 2, TRXFF_BOUNDARY_8188F)


# ---- M3 composite gate ----------------------------------------------


def init_device_post_phy(t: RTL8188FTVTransport, efuse) -> None:
    """Mirror `rtl8xxxu_init_device` from the RFSW block to CCK PD.

    Covers the second half of `core.c:4099-4373` (after `init_phy_rf`):
    RFSW/antenna, TX boundary, PBP, LLT, USB quirks, TX report, RCR,
    SIFS/EDCA/DARFRC, beacon, burst, aggregation, pkt-life-time,
    CCK/OFDM enable, CAM invalidate, set_tx_power, statistics, GPIO.
    """
    t.write32(REG_FPGA0_TX_INFO, 0x00000003)

    val32 = (FPGA0_RF_TRSW | FPGA0_RF_TRSWB | FPGA0_RF_ANTSW |
             FPGA0_RF_ANTSWB |
             ((FPGA0_RF_ANTSW | FPGA0_RF_ANTSWB) << FPGA0_RF_BD_CTRL_SHIFT) |
             FPGA0_RF_PAPE | (FPGA0_RF_PAPE << FPGA0_RF_BD_CTRL_SHIFT))
    t.write32(REG_FPGA0_XAB_RF_SW_CTRL, val32)

    t.write32(REG_FPGA0_XA_RF_INT_OE, 0x66F60210)

    tx_bnd = TX_TOTAL_PAGE_NUM_8188F + 1
    t.write8(REG_TXPKTBUF_BCNQ_BDNY, tx_bnd)
    t.write8(REG_TXPKTBUF_MGQ_BDNY, tx_bnd)
    t.write8(REG_TXPKTBUF_WMAC_LBK_BF_HD, tx_bnd)
    t.write8(REG_TRXFF_BNDY, tx_bnd)
    t.write8(REG_TDECTRL + 1, tx_bnd)

    val8 = (PBP_PAGE_SIZE_256 << PBP_PAGE_SIZE_TX_SHIFT) | \
        (PBP_PAGE_SIZE_256 << PBP_PAGE_SIZE_RX_SHIFT)
    t.write8(REG_PBP, val8)

    val32 = t.read32(REG_AUTO_LLT)
    val32 |= AUTO_LLT_INIT_LLT
    t.write32(REG_AUTO_LLT, val32)
    while t.read32(REG_AUTO_LLT) & AUTO_LLT_INIT_LLT:
        pass

    val16 = t.read16(REG_CR)
    val16 |= (1 << 6) | (1 << 7)
    t.write16(REG_CR, val16)

    val32 = t.read32(REG_TXDMA_OFFSET_CHK)
    val32 |= TXDMA_OFFSET_DROP_DATA_EN
    t.write32(REG_TXDMA_OFFSET_CHK, val32)

    val8 = t.read8(REG_TX_REPORT_CTRL)
    val8 |= TX_REPORT_CTRL_TIMER_ENABLE
    t.write8(REG_TX_REPORT_CTRL, val8)
    t.write8(REG_TX_REPORT_CTRL + 1, 0x02)
    t.write16(REG_TX_REPORT_TIME, 0xCDF0)

    val8 = t.read8(0x00A3)
    val8 &= 0xF8
    t.write8(0x00A3, val8)

    t.write8(REG_RX_DRVINFO_SZ, 4)

    t.write32(REG_HISR0, 0xFFFFFFFF)
    t.write32(REG_HISR1, 0xFFFFFFFF)

    val32 = (RCR_ACCEPT_PHYS_MATCH | RCR_ACCEPT_MCAST | RCR_ACCEPT_BCAST |
             RCR_ACCEPT_MGMT_FRAME | RCR_HTC_LOC_CTRL |
             RCR_APPEND_PHYSTAT | RCR_APPEND_ICV | RCR_APPEND_MIC)
    t.write32(REG_RCR, val32)

    t.write16(REG_RXFLTMAP2, 0xFFFF)
    t.write16(REG_RXFLTMAP1, 0x0400)
    t.write16(REG_RXFLTMAP0, 0xFFFF)

    val32 = t.read32(REG_RESPONSE_RATE_SET)
    val32 &= ~RESPONSE_RATE_BITMAP_ALL
    val32 |= RESPONSE_RATE_RRSR_CCK_ONLY_1M
    t.write32(REG_RESPONSE_RATE_SET, val32)

    t.write16(REG_SPEC_SIFS, (0x10 << 8) | 0x10)
    t.write16(REG_RETRY_LIMIT, (0x30 << 8) | 0x30)
    t.write16(REG_SPEC_SIFS, (0x10 << 8) | 0x0A)

    t.write16(REG_MAC_SPEC_SIFS, 0x100A)
    t.write16(REG_SIFS_CCK, 0x100A)
    t.write16(REG_SIFS_OFDM, 0x100A)

    t.write32(REG_EDCA_BE_PARAM, 0x005EA42B)
    t.write32(REG_EDCA_BK_PARAM, 0x0000A44F)
    t.write32(REG_EDCA_VI_PARAM, 0x005EA324)
    t.write32(REG_EDCA_VO_PARAM, 0x002FA226)

    t.write32(REG_DARFRC, 0x00000000)
    t.write32(REG_DARFRC + 4, 0x10080404)
    t.write32(REG_RARFRC, 0x04030201)
    t.write32(REG_RARFRC + 4, 0x08070605)

    val8 = t.read8(REG_FWHW_TXQ_CTRL)
    val8 |= (1 << 7)
    t.write8(REG_FWHW_TXQ_CTRL, val8)

    t.write8(REG_ACKTO, 0x40)

    val16 = BEACON_DISABLE_TSF_UPDATE | (BEACON_DISABLE_TSF_UPDATE << 8)
    t.write16(REG_BEACON_CTRL, val16)
    t.write16(REG_TBTT_PROHIBIT, 0x6404)
    t.write8(REG_BEACON_DMA_TIME, BEACON_DMA_ATIME_INT_TIME)
    t.write16(REG_BEACON_TCFG, 0x660F)

    init_burst(t)
    init_aggregation(t)

    t.write16(REG_PKT_VO_VI_LIFE_TIME, 0x0400)
    t.write16(REG_PKT_BE_BK_LIFE_TIME, 0x0400)

    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 |= (1 << 24) | (1 << 25)
    t.write32(REG_FPGA0_RF_MODE, val32)

    t.write32(REG_CAM_CMD, CAM_CMD_POLLING | (1 << 30))

    phy.set_tx_power(t, 1, efuse)

    t.write8(REG_HWSEQ_CTRL, 0xFF)
    t.write32(REG_BAR_MODE_CTRL, 0x0201FFFF)

    init_statistics(t)

    val8 = t.read8(REG_GPIO_MUXCFG)
    val8 &= ~GPIO_MUXCFG_IO_SEL_ENBT
    t.write8(REG_GPIO_MUXCFG, val8)

    t.write8(REG_CCK_PD_THRESH, 0x83)


def init_burst(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8xxxu_init_burst` (`core.c:3950-3995`)."""
    val8 = t.read8(REG_RXDMA_PRO_8723B)
    val8 &= ~(RXDMA_PRO_DMA_BURST_SIZE | RXDMA_PRO_DMA_BURST_CNT)
    val8 |= RXDMA_PRO_DMA_BURST_SIZE | RXDMA_PRO_DMA_BURST_CNT
    val8 |= RXDMA_PRO_DMA_MODE
    t.write8(REG_RXDMA_PRO_8723B, val8)

    val8 = t.read8(REG_HT_SINGLE_AMPDU_8723B)
    val8 |= HT_SINGLE_AMPDU_ENABLE
    t.write8(REG_HT_SINGLE_AMPDU_8723B, val8)

    t.write16(REG_MAX_AGGR_NUM, 0x0C14)
    t.write8(REG_AMPDU_MAX_TIME_8723B, 0x70)
    t.write32(REG_AGGLEN_LMT, 0xFFFFFFFF)
    t.write8(REG_RX_PKT_LIMIT, 0x18)
    t.write8(REG_PIFS, 0x00)
    t.write8(REG_FWHW_TXQ_CTRL, 0x80)
    t.write32(REG_FAST_EDCA_CTRL, 0x03086666)
    t.write8(REG_USTIME_TSF_8723B, 0x28)
    t.write8(REG_USTIME_EDCA, 0x28)

    val8 = t.read8(REG_RSV_CTRL)
    val8 |= RSV_CTRL_WLOCK_1C | RSV_CTRL_DIS_PRST
    t.write8(REG_RSV_CTRL, val8)


def init_aggregation(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8188fu_init_aggregation` (`8188f.c:645-686`)."""
    usb_tx_agg_desc_num = 6

    val32 = t.read32(REG_TDECTRL)
    val32 &= ~(0xF << 4)
    val32 |= usb_tx_agg_desc_num << 4
    t.write32(REG_TDECTRL, val32)
    t.write8(REG_DWBCN1_CTRL_8723B, usb_tx_agg_desc_num << 1)

    agg_ctrl = t.read8(REG_TRXDMA_CTRL)
    agg_ctrl &= ~TRXDMA_CTRL_RXDMA_AGG_EN

    agg_rx = t.read32(REG_RXDMA_AGG_PG_TH)
    agg_rx &= ~RXDMA_USB_AGG_ENABLE
    agg_rx &= ~0xFF0F

    rxdma_mode = t.read8(REG_RXDMA_PRO_8723B)
    rxdma_mode &= ~(1 << 1)

    t.write8(REG_TRXDMA_CTRL, agg_ctrl)
    t.write32(REG_RXDMA_AGG_PG_TH, agg_rx)
    t.write8(REG_RXDMA_PRO_8723B, rxdma_mode)


def init_statistics(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8188fu_init_statistics` (`8188f.c:673-683`)."""
    t.write16(REG_NHM_TIMER_8723B + 2, 0xC350)
    t.write16(REG_NHM_TH9_TH10_8723B + 2, 0xFFFF)
    t.write32(REG_NHM_TH3_TO_TH0_8723B, 0xFFFFFF50)
    t.write32(REG_NHM_TH7_TO_TH4_8723B, 0xFFFFFFFF)

    val32 = t.read32(REG_FPGA0_IQK)
    val32 |= 0xFF
    t.write32(REG_FPGA0_IQK, val32)

    val32 = t.read32(REG_NHM_TH9_TH10_8723B)
    val32 &= ~(0x700)
    val32 |= (1 << 8)
    t.write32(REG_NHM_TH9_TH10_8723B, val32)

    val32 = t.read32(REG_OFDM0_FA_RSTC)
    val32 |= (1 << 7)
    t.write32(REG_OFDM0_FA_RSTC, val32)


# ---- RX acceptance + monitor filter (core.c:7494-7971) ----------------


def enable_rx_path(t: RTL8188FTVTransport) -> None:
    """Accept all data and mgmt frames; force IGI to 0x1e.

    Mirror of the `rtl8xxxu_start` tail (core.c:7494-7501).
    Produces 4 ops against the cold-boot capture: two 16-bit filt-map
    writes followed by a masked AGC write.
    """
    t.write16(REG_RXFLTMAP2, 0xFFFF)
    t.write16(REG_RXFLTMAP0, 0xFFFF)

    val32 = t.read32(REG_OFDM0_XA_AGC_CORE1)
    val32 = (val32 & ~OFDM0_X_AGC_CORE1_IGI_MASK) | 0x1e
    t.write32(REG_OFDM0_XA_AGC_CORE1, val32)


_BASE_RCR = (RCR_ACCEPT_PHYS_MATCH | RCR_ACCEPT_MCAST | RCR_ACCEPT_BCAST |
             RCR_ACCEPT_MGMT_FRAME | RCR_HTC_LOC_CTRL |
             RCR_APPEND_PHYSTAT | RCR_APPEND_ICV | RCR_APPEND_MIC)

# FIF_BCN_PRBRESP_PROMISC | FIF_CONTROL | FIF_OTHER_BSS | FIF_PSPOLL
MONITOR_FIF_FLAGS = 0x08 | 0x10 | 0x20 | 0x40


def configure_filter(t: RTL8188FTVTransport,
                     fif_flags: int = MONITOR_FIF_FLAGS) -> None:
    """Mirror of `rtl8xxxu_configure_filter` (core.c:6920-7971).

    8188F is a monitor-only device, so only FIF_BCN_PRBRESP_PROMISC,
    FIF_CONTROL, FIF_OTHER_BSS (accept AP frames) and FIF_PSPOLL matter.
    RCR state lives in software (priv->regrcr), so this is a single
    register write with no read; the capture replays 3 identical calls.
    """
    rcr = _BASE_RCR

    if not (fif_flags & 0x08):  # FIF_BCN_PRBRESP_PROMISC
        rcr |= RCR_CHECK_BSSID_BEACON | RCR_CHECK_BSSID_MATCH

    if fif_flags & 0x10:  # FIF_CONTROL
        rcr |= RCR_ACCEPT_CTRL_FRAME
    if fif_flags & 0x20:  # FIF_OTHER_BSS
        rcr |= RCR_ACCEPT_AP
    if fif_flags & 0x40:  # FIF_PSPOLL
        rcr |= RCR_ACCEPT_PM

    t.write32(REG_RCR, rcr)
