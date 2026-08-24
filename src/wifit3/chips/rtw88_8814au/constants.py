"""Realtek RTL8814AU (rtw88 family, WCPU_3081 / iDDMA FW path) constants.

Verified against `driver_sources/rtw88-source-v6.18/rtw8814a{,u}.{c,h}` +
`driver_captures/captures_rtw88_8814au/capture-1.pcap`. See RTL8814AU.md for the
provenance of each fact.

The 8814A's firmware/MAC path is the same modern iDDMA path as the 8822B
(both `RTW_WCPU_3081`), so this re-exports the family-shared register surface
from :mod:`wifit3.chips.rtw88_base.registers` and adds the 8814a-specific
values. The chip diverges from the 8822b only in PHY/RF (4T4R), which is M3+.
"""

from __future__ import annotations

# --- Re-export the common rtw88 register surface ---------------------------
from wifit3.chips.rtw88_base.registers import (  # noqa: F401
    BIT_CHECK_SUM_OK,
    BIT_DDMACH0_CHKSUM_CONT,
    BIT_DDMACH0_CHKSUM_EN,
    BIT_DDMACH0_CHKSUM_STS,
    BIT_DDMACH0_OWN,
    BIT_DDMACH0_RESET_CHKSUM_STS,
    BIT_DIS_TSF_UDT,
    BIT_DMEM_CHKSUM_OK,
    BIT_DMEM_DW_OK,
    BIT_EN_BCN_FUNCTION,
    BIT_FEN_CPUEN,
    BIT_FW_DW_RDY,
    BIT_FW_INIT_RDY,
    BIT_H2CQ_FULL,
    BIT_HCI_TXDMA_EN,
    BIT_IMEM_CHKSUM_OK,
    BIT_IMEM_DW_OK,
    BIT_MASK_DDMACH0_DLEN,
    BIT_MCUFWDL_EN,
    BIT_TXDMA_EN,
    BIT_WLMCU_IOIF,
    FW_READY,
    FW_READY_MASK,
    OCPBASE_DMEM_88XX,
    OCPBASE_TXBUF_88XX,
    REG_CR,
    REG_DDMA_CH0CTRL,
    REG_DDMA_CH0DA,
    REG_DDMA_CH0SA,
    REG_H2CQ_CSR,
    REG_MCUFW_CTRL,
    REG_RSV_CTRL,
    REG_SYS_CFG1,
    REG_SYS_FUNC_EN,
    REG_SYS_STATUS1,
    REG_TXDMA_PQ_MAP,
    REG_TXDMA_STATUS,
    TX_DESC_QSEL_BEACON,
)
from wifit3.chips.products import ALFA, ASUS, DLink, Edimax, Elecom, Hawking, Netgear, TPLink, TRENDnet

# --- USB IDs (rtw_8814au_id_table in rtw8814au.c) --------------------------
# The Alfa AWUS1900 enumerates as the Realtek default 0x0bda:0x8813. The rest
# are the kernel's full table (other vendors' 8814AU dongles).
USB_IDS_8814AU: tuple[tuple[int, int, str, str | None, str | None], ...] = (
    (0x0BDA, 0x8813, "RTL8814AU", None, ALFA.AWUS1900),
    (0x056E, 0x400B, "RTL8814AU", None, Elecom.WDC_1300SU2),
    (0x056E, 0x400D, "RTL8814AU", None, Elecom.WDC_1300SU3),
    (0x0846, 0x9054, "RTL8814AU", None, Netgear.A7000),
    (0x0B05, 0x1817, "RTL8814AU", None, ASUS.USB_AC68),
    (0x0B05, 0x1852, "RTL8814AU", None, ASUS.USB_AC68),
    (0x0B05, 0x1853, "RTL8814AU", None, ASUS.USB_AC68),
    (0x0E66, 0x0026, "RTL8814AU", None, Hawking.HW17ACU),
    (0x2001, 0x331A, "RTL8814AU", None, DLink.DWA_192),
    (0x20F4, 0x809A, "RTL8814AU", None, TRENDnet.TEW_809UB),
    (0x20F4, 0x809B, "RTL8814AU", None, TRENDnet.TEW_809UB),
    (0x2357, 0x0106, "RTL8814AU", None, TPLink.ARCHER_T9UH),
    (0x7392, 0xA834, "RTL8814AU", None, Edimax.EW_7833UAC),
    (0x7392, 0xA833, "RTL8814AU", None, Edimax.EW_7833UAC),
)

# --- Chip parameters (rtw8814a_hw_spec, rtw8814a.c:2180) -------------------
TX_PKT_DESC_SZ = 40              # .tx_pkt_desc_sz — 40, NOT the 8822b's 48
RX_PKT_DESC_SZ = 24              # .rx_pkt_desc_sz
PAGE_SIZE = 128                  # .page_size = TX_PAGE_SIZE = 1<<7 (main.h:35)
TXFF_SIZE = (2048 - 10) * 128    # .txff_size
RXFF_SIZE = 23552                # .rxff_size
SYS_FUNC_EN_8814A = 0xDC         # .sys_func_en
MAX_POWER_INDEX = 0x3F           # .max_power_index

# 4T4R RF paths (.rf_base_addr / .rf_sipi_addr). Used from M3 onward.
RF_BASE_ADDR = (0x2800, 0x2C00, 0x3800, 0x3C00)
RF_SIPI_ADDR = (0xC90, 0xE90, 0x1890, 0x1A90)

# --- FW upload (modern iDDMA path; see mac.c:776 __rtw_download_firmware) --
# Sizes/addresses are verified [WIRE] against capture-1 AND the FW header
# (assets/rtw8814a_fw-linux_firmware.bin): see RTL8814AU.md §1.3.1.
FW_HDR_SIZE = 64                 # rtw_fw_hdr (fw.h:316)
FW_HDR_CHKSUM_SIZE = 8           # fw.h:13
DLFW_MAX_CHUNK_SIZE = 0x1000     # 4096 bytes per tx_pkt + iddma cycle

# The actual FW-upload TX descriptor is `chip->tx_pkt_desc_sz` (= 40, drives
# both the descriptor build and the iddma source offset). But `send_firmware_pkt`
# (mac.c:550) computes its ZLP-avoidance `%512` decision against the kernel's
# HARDCODED `#define TX_DESC_SIZE 48` (mac.c:528) — NOT the chip's real desc
# size. For 8822b these coincide (both 48); for the 8814a they differ, so the
# two values are kept separate here.
FW_DLFW_ZLP_TXDESC = 48          # mac.c:528 #define TX_DESC_SIZE (ZLP check only)

DMEM_ADDR = 0x00200000           # header dmem_addr 0x80200000 & ~BIT(31)
DMEM_UPLOAD_SIZE = 5792          # 5784 body + 8 chksum
IMEM_ADDR = 0x00000000           # header imem_addr 0x80000000 & ~BIT(31)
IMEM_UPLOAD_SIZE = 62464         # 62456 body + 8 chksum
EMEM_PRESENT = False             # mem_usage bit 4 clear — no EMEM segment

# FW-upload bulk-OUT endpoint for the AWUS1900 (out_ep[0]) — [WIRE] capture-1.
EP_FW_BULK_OUT = 0x02

# ===========================================================================
# M2 — MAC init + FIFO/queue config
# ===========================================================================
# Family-shared FIFO/queue register addresses that already live in
# rtw88_base.registers (re-exported here so fifo.py reads them off `constants`):
from wifit3.chips.rtw88_base.registers import (  # noqa: E402,F401
    REG_FIFOPAGE_CTRL_2,
    REG_FIFOPAGE_INFO_1,
    REG_FWHW_TXQ_CTRL,
    REG_RQPN_CTRL_2,
)

# --- MAC-init register addresses (verbatim from reg.h) ---------------------
MAC_TRX_ENABLE = 0xFF            # reg.h:219 (all 8 low bits of REG_CR)
REG_RRSR = 0x0440
REG_RETRY_LIMIT = 0x042A
REG_MAX_AGGR_NUM = 0x04CA
REG_SPEC_SIFS = 0x0428
REG_MAC_SPEC_SIFS = 0x063A
REG_SIFS = 0x0514
REG_EDCA_VO_PARAM = 0x0500
REG_EDCA_VI_PARAM = 0x0504
REG_EDCA_BE_PARAM = 0x0508
REG_EDCA_BK_PARAM = 0x050C
REG_ACKTO = 0x0640
REG_TBTT_PROHIBIT = 0x0540
REG_DRVERLYINT = 0x0558
REG_BCNDMATIM = 0x0559
REG_BCNTCFG = 0x0510
REG_BCN_MAX_ERR = 0x055D
REG_FAST_EDCA_VOVI_SETTING = 0x1448
REG_FAST_EDCA_BEBK_SETTING = 0x144C
REG_USB_MOD = 0xF008
REG_SW_AMPDU_BURST_MODE_CTRL = 0x04BC
REG_HIMR0 = 0x00B0
REG_HIMR1 = 0x00B8
REG_RXFLTMAP0 = 0x06A0
REG_RXFLTMAP1 = 0x06A2
REG_RXFLTMAP2 = 0x06A4
REG_AUTO_LLT_V1 = 0x0208
REG_FIFOPAGE_INFO_2 = 0x0234
REG_FIFOPAGE_INFO_3 = 0x0238
REG_FIFOPAGE_INFO_4 = 0x023C
REG_FIFOPAGE_INFO_5 = 0x0240
REG_BCNQ_BDNY_V1 = 0x0424
REG_BCNQ1_BDNY_V1 = 0x0456
REG_RXFF_BNDY = 0x011C
REG_TXDMA_OFFSET_CHK = 0x020C
REG_H2C_HEAD = 0x0244
REG_H2C_TAIL = 0x0248
REG_H2C_READ_ADDR = 0x024C
REG_H2C_INFO = 0x0254
REG_H2C_PKT_READADDR = 0x10D0
REG_H2C_PKT_WRITEADDR = 0x10D4
REG_RX_DRVINFO_SZ = 0x060F
REG_TRXFF_BNDY = 0x0114
REG_RCR = 0x0608
REG_WMAC_OPTION_FUNCTION = 0x07D0

# --- MAC-init bit macros (verbatim from reg.h) -----------------------------
BIT_MAC_SEC_EN = 1 << 9
BIT_32K_CAL_TMR_EN = 1 << 10
BIT_APP_PHYSTS = 1 << 28
BIT_AUTO_INIT_LLT_V1 = 1 << 0
BIT_LD_RQPN = 1 << 31
BIT_EN_WR_FREE_TAIL = 1 << 20
BIT_MASK_BLK_DESC_NUM = 0xF << 4         # GENMASK(7,4)
BIT_PRE_TX_CMD = 1 << 6
BIT_RXDMA_ARBBW_EN = 1 << 0

# REG_TXDMA_PQ_MAP field shifts (2-bit fields, mask 0x3) — reg.h
TXDMA_MAP_SHIFTS = {            # queue → (shift, dma_mapping value source)
    "vo": 4, "vi": 6, "be": 8, "bk": 10, "mg": 12, "hi": 14,
}
TXDMA_MAP_MASK = 0x3

# --- MAC-init scalar constants ---------------------------------------------
WLAN_BCN_DMA_TIME = 0x02         # rtw88xxa.h:66
# WLAN_TBTT_TIME = WLAN_TBTT_PROHIBIT(0x04) | (WLAN_TBTT_HOLD_TIME(0x64) << 8)
WLAN_TBTT_TIME = 0x04 | (0x64 << 8)   # rtw88xxa.h:69 → 0x6404
C2H_PKT_BUF = 256                # mac.h
PHY_STATUS_SIZE = 4              # mac.h
USB_TX_AGG_DESC_NUM = 3          # rtw8814a_hw_spec.usb_tx_agg_desc_num

# --- FIFO partition params (rtw8814a_hw_spec) ------------------------------
RSVD_DRV_PG_NUM = 8              # .rsvd_drv_pg_num
CSI_BUF_PG_NUM = 0               # .csi_buf_pg_num
# Reserved-page counts for the non-8051 path (mac.h:25..29) — chip-generic.
RSVD_PG_H2C_EXTRAINFO_NUM = 24
RSVD_PG_H2C_STATICINFO_NUM = 8
RSVD_PG_H2CQ_NUM = 8
RSVD_PG_CPU_INSTRUCTION_NUM = 0
RSVD_PG_FW_TXBUF_NUM = 4
TX_PAGE_SIZE_SHIFT = 7           # main.h:34

# ===========================================================================
# M3.b — phy_set_param (BB/RF domain enable + RF readback)
# ===========================================================================
REG_SYS_CFG3_8814A = 0x1000      # BB glb-rst/rstb live at +2
BIT_FEN_USBA = 1 << 2            # REG_SYS_FUNC_EN: power on BB/RF for USB
REG_RF_CTRL1 = 0x0020            # RF path B enable
REG_RF_CTRL2 = 0x0021            # RF path C enable
REG_RF_CTRL3 = 0x0076            # RF path D enable
REG_RXPSEL = 0x0808
BIT_RX_PSEL_RST = (1 << 28) | (1 << 29)
RF_RCK1_V1 = 0x1C                # RF reg read back after rf-table load
REG_AFE_CTRL3 = 0x002C           # crystal_cap (xtal_k) — reference clock trim
AFE_CTRL3_XCAP_MASK = 0x07FF8000

# ===========================================================================
# M3.c — channel tune
# ===========================================================================
RTW_BAND_2G = 1                  # BIT(NL80211_BAND_2GHZ)
RTW_BAND_5G = 2                  # BIT(NL80211_BAND_5GHZ)
RTW_CHANNEL_WIDTH_20 = 0
RTW_CHANNEL_WIDTH_40 = 1
RTW_CHANNEL_WIDTH_80 = 2

REG_CCK_CHECK = 0x0454
BIT_CHECK_CCK_EN = 1 << 7
REG_CLKTRK = 0x0860
REG_AGC_TABLE = 0x0958
REG_CCK0_TX_FILTER1 = 0x0A20
REG_CCK0_TX_FILTER2 = 0x0A24
REG_CCK0_DEBUG_PORT = 0x0A28
REG_TXPSEL = 0x080C
REG_CCK_RX = 0x0A04
REG_RFE_PINMUX_A = 0x0CB0
REG_RFE_PINMUX_B = 0x0EB0
REG_RFE_PINMUX_C = 0x18B4
REG_RFE_PINMUX_D = 0x1AB4
REG_RFE_INVSEL_D = 0x1ABC
BIT_RFE_SELSW0_D = 0x0FF00000    # GENMASK(27, 20)
REG_WMAC_TRXPTCL_CTL = 0x0668
BIT_RFMOD = (1 << 7) | (1 << 8)
BIT_RFMOD_40M = 1 << 7
BIT_RFMOD_80M = 1 << 8
REG_ADCCLK = 0x08AC
REG_CCASEL = 0x082C
REG_DATA_SC = 0x0483
REG_CCK_TX_EN = 0x0A80           # literal 0xa80 in switch_band (BIT(18) = CCK TX en)

# Spur calibration (rtw8814a_spur_calibration) — NBI/CSI notch. [SRC] reg.h:613-619
REG_PDMFTH = 0x0830
REG_CSI_MASK_SETTING1 = 0x0874
REG_NBI_SETTING = 0x087C
BIT_NBI_ENABLE = 1 << 13
REG_CSI_FIX_MASK0 = 0x0880
REG_CSI_FIX_MASK1 = 0x0884
REG_CSI_FIX_MASK6 = 0x0898
REG_CSI_FIX_MASK7 = 0x089C

# RF18 (RF_CFGCH) field masks
RF_CFGCH = 0x18
RF18_RFSI_MASK = (1 << 18) | (1 << 17)
RF18_BAND_MASK = (1 << 16) | (1 << 9) | (1 << 8)
RF18_CHANNEL_MASK = 0xFF
RF18_BW_MASK = (1 << 11) | (1 << 10)

# ===========================================================================
# M5 — RX / monitor
# ===========================================================================
REG_RXDMA_MODE = 0x0290
REG_RXPKT_NUM = 0x0284           # RX-DMA state: low16=pending pkt count; BIT(17)=idle
BIT_RXDMA_IDLE = 1 << 17
# RX aggregation (rtw_usb_dynamic_rx_agg_v1 — 8814a). WITHOUT this the chip does
# not frame-align bulk-IN transfers, so reads land mid-frame and the per-URB
# rx_pkt_desc parse fails. size=0x5 pages, timeout=0x20.
REG_RXDMA_AGG_PG_TH = 0x0280
BIT_RXDMA_AGG_EN = 1 << 2        # BIT(2) of REG_TXDMA_PQ_MAP (0x010C)
# RX aggregation OFF — the values rtw_usb_dynamic_rx_agg_v1(enable=false) writes,
# which is what the kernel runs in monitor/unassociated mode (confirmed in the
# cold-boot pcap: REG_RXDMA_AGG_PG_TH=0x0100 every time, never the 0x2005 enable
# value). size=0 => the RX-DMA flushes each frame immediately instead of
# accumulating pages; accumulation is what intermittently wedged the DMA at cold
# boot (deliver-once-then-halt). (root-cause, not a band-aid; docs/porting/METHODOLOGY.md Step 3.)
RXDMA_AGG_SIZE = 0x00
RXDMA_AGG_TIMEOUT = 0x01
# Promiscuous monitor RCR (AAP|APM|AM|AB + APP_PHYSTS, CBSSID_* cleared) —
# family-shared rtw88 value, same as rtl8821au/8822bu monitor.
RCR_MONITOR = 0xF410400F
# 8814a STA RXFLTMAP defaults (rtw8814a_mac_init) — accept mgmt/ctrl/data.
RXFLTMAP0_8814A = 0xFFFF
RXFLTMAP1_8814A = 0x0400
RXFLTMAP2_8814A = 0xFFFF

# --- Monitor CCK RX sensitivity (config_cck_rx_antenna_init + CCK PD) -------
# Beacons are 1 Mbps CCK; the kernel tunes the CCK packet-detect threshold via
# a dynamic watchdog (cck_pd_set) we don't run. For monitor we force the most
# sensitive level (LV0) and enable 2R-CCA + MRC + RX diversity.
REG_RXSB_CCK = 0x0A00            # = REG_RXSB
BIT_RXSB_ANA_DIV = 1 << 15
REG_CCA = 0x0A70
BIT_CCA_CO = 1 << 7
REG_ANTSEL = 0x0A74
BIT_ANT_BYCO = 1 << 8
REG_PRECTRL = 0x0A14
BIT_DIS_CO_PATHSEL = 1 << 7
REG_CCA_MF = 0x0A20              # aliases REG_CCK0_TX_FILTER1; MBC weighting field
BIT_MBC_WIN = 0x30               # GENMASK(5, 4)
REG_CCKTX = 0x0A84
BIT_CMB_CCA_2R = 1 << 28
REG_CCK_PD_TH = 0x0A0A
CCK_PD_TH_MAX_SENS = 0x40        # pd[CCK_PD_LV0] (rtw8814a_phy_cck_pd_set)

# --- PHY RX counters (rtw8814a_false_alarm_statistics) — for RX diagnostics --
# CRC regs: low 16 = OK count (demodulated), high 16 = err count.
REG_FA_CCK = 0x0A5C              # CCK false-alarm (16b)
REG_FA_OFDM = 0x0F48             # OFDM false-alarm (16b)
REG_CRC_CCK = 0x0F04
REG_CRC_OFDM = 0x0F14
REG_CRC_HT = 0x0F10
REG_CRC_VHT = 0x0F0C
REG_CCA_OFDM = 0x0F08            # CCA count in high 16
REG_CCA_CCK = 0x0FCC             # CCA count in low 16
REG_CNTRST = 0x0B58              # BIT(0): counter reset
REG_FAS = 0x09A4                 # BIT(17): FA counter reset
REG_CCK0_FAREPORT = 0x0A2C       # BIT(15): CCK FA counter reset
BIT_CCK0_2RX = 1 << 18           # [SRC] reg.h:653 — config_trx_path
BIT_CCK0_MRC = 1 << 22           # [SRC] reg.h:654
BIT_RXPSEL_CCK_EN = 1 << 28      # REG_RXPSEL bit: CCK demod enabled (FA accounting)

# --- DIG: Dynamic Initial Gain (rtw_phy_dig, phy.c; rtw8814a_dig table) -------
# Per-path OFDM initial-gain index registers (7-bit). [SRC] rtw8814a.c:2139
REG_DIG_PATH = (0x0C50, 0x0E50, 0x1850, 0x1A50)
DIG_IGI_MASK = 0x7F
DIG_MIN = 0x1C                   # chip->dig_min / DIG_CVRG_MIN (max coverage)
# Coverage-mode (no-link / monitor) constants. [SRC] phy.c:365-371
DIG_CVRG_MIN = 0x1C
DIG_CVRG_MID = 0x26
DIG_CVRG_MAX = 0x2A
DIG_CVRG_FA_TH_LOW = 2000
DIG_CVRG_FA_TH_HIGH = 4000
DIG_CVRG_FA_TH_EXTRA_HIGH = 5000
DIG_RSSI_GAIN_OFFSET = 15
