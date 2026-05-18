"""RTL8812AU protocol constants (rtw88 family, M1 = FW upload scope only).

For M1 we only need the bits required to:
  - identify the device on the USB bus
  - run the 8812a-specific RF reset writes before the power sequence
  - drive `card_enable_flow_8812a` (in power_seq.py)
  - run the legacy MCUFWDL FW upload (handled by rtw88_base.firmware_legacy)
  - validate FW is running (FW_READY_LEGACY in REG_MCUFW_CTRL)

Anything beyond that (post-FW MAC init, PHY tables, channel tune, RX/TX)
is M2+ scope and lives outside this file.

Reference: `data_dumps/rtw88-source-v6.18/rtw8812a.c` + `rtw8812a.h`.
"""

from __future__ import annotations

# --- USB IDs (rtw_8812au_id_table in rtw8812au.c) -------------------------
USB_VID_REALTEK = 0x0BDA

# AWUS036ACH ships as 0BDA:8812 (first entry of rtw_8812au_id_table).
USB_PID_AWUS036ACH = 0x8812


# --- 8812A chip_info knobs needed for FIFO computation (rtw8812a.c:1038) --
# `rtw_chip_info` fields used by `rtw_set_trx_fifo_info` (mac.c:1138):
TXFF_SIZE = 131072       # vs 8821a's 65536
PAGE_SIZE = 512          # vs 8821a's 256
RSVD_DRV_PG_NUM = 9      # vs 8821a's 8
# txff_pg_num   = 131072 / 512 = 256
# rsvd_pg_num   = rsvd_drv_pg_num = 9 (8051 path)
# acq_pg_num    = 256 - 9 = 247
# rsvd_boundary = 247


# --- 8812a-specific pre-power-seq RF reset (rtw88xxa.c:1037..1041) --------
# Both RF paths are brought out of reset via REG_RF_CTRL (path A) and
# REG_RF_B_CTRL (path B) — 8812a is 2T2R so it touches both. The pair of
# writes (5 then 7) clears then asserts SDM_RSTB|RSTB|EN.
REG_RF_CTRL = 0x001F     # rtw88_base/registers.py also exports this
REG_RF_B_CTRL = 0x0076

# Values written verbatim from the kernel:
RF_CTRL_RESET_STEP1 = 0x05   # BIT_RF_RSTB | BIT_RF_EN (~SDM_RSTB)
RF_CTRL_RESET_STEP2 = 0x07   # BIT_RF_SDM_RSTB | BIT_RF_RSTB | BIT_RF_EN


# --- REG_CR setup post-power_seq (mac_init_system_cfg_legacy, mac.c:355) --
# Matches the 8821au "init_sys_cfg_legacy" sequence verbatim — same
# wlan-CPU type (8051), same chip family, so the same register-poke
# pattern works.
#
# These are referenced from mac.py.
REG_CR = 0x0100
REG_HWSEQ_CTRL = 0x0423
REG_SYS_CLKR = 0x0008
REG_GPIO_MUXCFG = 0x0040
BIT_WAKEPAD_EN = 1 << 3   # of REG_SYS_CLKR (reg.h:30)
BIT_EN_SIC = 1 << 12      # of REG_GPIO_MUXCFG (reg.h:74)

# REG_SYS_FUNC_EN / REG_RSV_CTRL / REG_LDO_SWR_CTRL — for mac_pre_system_cfg.
REG_SYS_FUNC_EN = 0x0002
REG_RSV_CTRL = 0x001C
REG_SYS_CFG1 = 0x00F0
REG_SYS_CFG2 = 0x00FC
REG_LDO_SWR_CTRL = 0x007C
BIT_LDO = 1 << 24         # of REG_SYS_CFG1 (reg.h:188)
LDO_SEL = 0xC3            # write8 to REG_LDO_SWR_CTRL when BIT_LDO is set
SPS_SEL = 0x83            # write8 to REG_LDO_SWR_CTRL when BIT_LDO is clear

# REG_CR power-state magic (mac.c:291).
REG_CR_OFF_VALUE = 0xEA   # value read from REG_CR when card is in disabled state

# --- LLT init (rtw88xxa.c:391) — REG_LLT_INIT bitfield --------------------
REG_LLT_INIT = 0x01E0
BIT_LLT_WRITE_ACCESS = 1 << 30

# --- REG_TXDMA_OFFSET_CHK / DROP_DATA_EN (rtw88xxa.c:1067) ----------------
REG_TXDMA_OFFSET_CHK = 0x020C
BIT_DROP_DATA_EN = 1 << 9


# ---------------------------------------------------------------------------
# M2-b: post-FW MAC init registers and constants (rtw88xxa.c:1083..1175).
# Most reg addresses are family-shared (rtw88_base.registers); chip-specific
# values live here.
# ---------------------------------------------------------------------------

# Chip parameters (rtw8812a_hw_spec in rtw8812a.c:1038).
RXFF_SIZE = 16128
REPORT_BUF = 128
PHY_STATUS_SIZE = 4
USB_TX_AGG_DESC_NUM = 1        # vs 8821a's 6

# Page table for USB-3-bulkout 8812A (rtw8812a.c:948, index [3]).
# AWUS036ACH has 3 bulk-OUT endpoints (0x02/0x03/0x04). If we ever want to
# support an 8812A card with 2 or 4 bulkout endpoints we'd add the other
# tables and detect bulkout count at runtime.
PG_TBL_3BO_HQ_NUM = 16
PG_TBL_3BO_NQ_NUM = 0
PG_TBL_3BO_LQ_NUM = 16
PG_TBL_3BO_EXQ_NUM = 0
PG_TBL_3BO_GAPQ_NUM = 1

# DMA-mapping enum (main.h).
RTW_DMA_MAPPING_EXTRA = 0
RTW_DMA_MAPPING_LOW = 1
RTW_DMA_MAPPING_NORMAL = 2
RTW_DMA_MAPPING_HIGH = 3

# RQPN table for USB-3-bulkout 8812A (rtw8812a.c:957, index [3]).
# {hi=HIGH, mg=NORMAL, bk=LOW, be=LOW, vi=HIGH, vo=HIGH}.
RQPN_3BO_HI = RTW_DMA_MAPPING_HIGH
RQPN_3BO_MG = RTW_DMA_MAPPING_NORMAL
RQPN_3BO_BK = RTW_DMA_MAPPING_LOW
RQPN_3BO_BE = RTW_DMA_MAPPING_LOW
RQPN_3BO_VI = RTW_DMA_MAPPING_HIGH
RQPN_3BO_VO = RTW_DMA_MAPPING_HIGH

# Pre-encoded WLAN_TBTT_TIME (rtw88xxa.h:69):
# WLAN_TBTT_PROHIBIT(0x04) | (WLAN_TBTT_HOLD_TIME(0x64) << 8) = 0x6404
WLAN_TBTT_TIME = (0x04 | (0x64 << 8))

# BIT_TXDMA_*_MAP shifts (REG_TXDMA_PQ_MAP). Each lane is 2 bits.
BIT_SHIFT_TXDMA_VOQ_MAP = 4
BIT_SHIFT_TXDMA_VIQ_MAP = 6
BIT_SHIFT_TXDMA_BEQ_MAP = 8
BIT_SHIFT_TXDMA_BKQ_MAP = 10
BIT_SHIFT_TXDMA_MGQ_MAP = 12
BIT_SHIFT_TXDMA_HIQ_MAP = 14
BIT_MASK_TXDMA_MAP = 0x3

# REG_BCN_CTRL bits.
BIT_DIS_TSF_UDT = 1 << 4
BIT_EN_BCN_FUNCTION = 1 << 3

# REG_RXDMA_MODE encoding (used by rtw_usb_init_burst_pkt_len).
BIT_DMA_MODE = 1 << 1
BIT_DMA_BURST_CNT = 0b11 << 2
BIT_MASK_DMA_BURST_SIZE = 0b11 << 4
BIT_SHIFT_DMA_BURST_SIZE = 4
BIT_DMA_BURST_SIZE_64 = 2
BIT_DMA_BURST_SIZE_512 = 1
BIT_DMA_BURST_SIZE_1024 = 0

# Misc 8812a register addresses (used in post_fw_mac_init).
REG_RQPN = 0x0200
REG_RQPN_NPQ = 0x0214
REG_FIFOPAGE_CTRL_2 = 0x0204
REG_DWBCN0_CTRL = 0x0208
REG_TXDMA_PQ_MAP = 0x010C
REG_TRXFF_BNDY = 0x0114
REG_HMETFR = 0x01CC
REG_BCNQ_BDNY = 0x0424
REG_MGQ_BDNY = 0x0425
REG_WMAC_LBK_BF_HD = 0x045D
REG_RX_DRVINFO_SZ = 0x060F
REG_RCR = 0x0608
REG_WMAC_OPTION_FUNCTION = 0x07D0
# REG_RCR bit definitions (reg.h:500..534)
BIT_APP_FCS = 1 << 31
BIT_APP_MIC = 1 << 30
BIT_APP_ICV = 1 << 29
BIT_APP_PHYSTS = 1 << 28      # ← master switch: append phy_status to RX frames
BIT_AB = 1 << 3               # accept broadcast
BIT_AM = 1 << 2               # accept multicast
BIT_APM = 1 << 1              # accept physical-match
BIT_AAP = 1 << 0              # accept ALL packets (= promiscuous / monitor)
REG_HIMR0 = 0x00B0
REG_HIMR1 = 0x00B8
REG_RXFLTMAP0 = 0x06A0
REG_RXFLTMAP1 = 0x06A2
REG_RXFLTMAP2 = 0x06A4
REG_MAR = 0x0620
REG_RRSR = 0x0440
REG_RETRY_LIMIT = 0x042A
REG_SPEC_SIFS = 0x0428
REG_MAC_SPEC_SIFS = 0x063A
REG_SIFS = 0x0514
REG_EDCA_VO_PARAM = 0x0500
REG_EDCA_VI_PARAM = 0x0504
REG_EDCA_BE_PARAM = 0x0508
REG_EDCA_BK_PARAM = 0x050C
REG_USTIME_TSF = 0x055C
REG_USTIME_EDCA = 0x0638
REG_FWHW_TXQ_CTRL = 0x0420
REG_ACKTO = 0x0640
REG_TBTT_PROHIBIT = 0x0540
REG_BCN_CTRL = 0x0550
REG_DRVERLYINT = 0x0558
REG_BCNDMATIM = 0x0559
REG_BCNTCFG = 0x0510
REG_BCN_MAX_ERR = 0x055D
REG_USB3_RXITV = 0xF050
REG_RXDMA_STATUS = 0x0288
REG_RXDMA_MODE = 0x0290
REG_AMPDU_MAX_TIME = 0x0456
REG_AMPDU_MAX_LENGTH = 0x0458
REG_SINGLE_AMPDU_CTRL = 0x04C7
REG_RX_PKT_LIMIT = 0x060C
REG_PIFS = 0x0512
REG_MAX_AGGR_NUM = 0x04CA
REG_ARFR0 = 0x0444
REG_ARFRH0 = 0x0448
REG_ARFR1_V1 = 0x044C
REG_ARFRH1_V1 = 0x0450
REG_ARFR2_V1 = 0x048C
REG_ARFRH2_V1 = 0x0490
REG_ARFR3_V1 = 0x0494
REG_ARFRH3_V1 = 0x0498

# REG_PBP — only 8812A writes this (rtw88xxa.c:1093). reg.h:222
REG_PBP = 0x0104
PBP_RX_MASK = 0x0F
PBP_TX_MASK = 0xF0
PBP_64 = 0x0
PBP_512 = 0x3

# Bits.
BIT_EN_SINGLE_APMDU = 1 << 7          # REG_SINGLE_AMPDU_CTRL
BIT_MACRXEN = 1 << 7                  # REG_CR
BIT_MACTXEN = 1 << 6                  # REG_CR
BIT_LD_RQPN = 1 << 31                 # REG_RQPN


# ---------------------------------------------------------------------------
# M2-d: PHY init (BB/RF/band-switch) registers and constants.
# rtw88xxa.c:572..1004
# ---------------------------------------------------------------------------

REG_AFE_CTRL3 = 0x002C
REG_CCK_CHECK = 0x0454
REG_CCK_RPT_FORMAT = 0x0804
REG_RXPSEL = 0x0808
REG_TXPSEL = 0x080C
REG_CCASEL = 0x082C
REG_PDMFTH = 0x0830
REG_BWINDICATION = 0x0834
REG_ANTSEL_SW = 0x0900
REG_CCK_RX = 0x0A04
REG_3WIRE_SWA = 0x0C00
REG_TXSCALE_A = 0x0C1C
REG_LSSI_WRITE_A = 0x0C90
REG_RFE_PINMUX_A = 0x0CB0
REG_RFE_INV_A = 0x0CB4
REG_3WIRE_SWB = 0x0E00
REG_TXSCALE_B = 0x0E1C
REG_LSSI_WRITE_B = 0x0E90
REG_RFE_PINMUX_B = 0x0EB0
REG_RFE_INV_B = 0x0EB4

# Post-PHY inline pokes (rtw88xxa.c:1185..1217)
REG_NAV_CTRL = 0x0650
REG_QUEUE_CTRL = 0x04C6
REG_EARLY_MODE_CONTROL = 0x02BC
REG_TX_RPT_TIME = 0x04F0
REG_SYS_SDIO_CTRL = 0x0070
REG_ACLK_MON = 0x003E
REG_USB_HRPWM = 0xFE58
REG_BAR_MODE_CTRL = 0x04CC
RTW_SEC_CMD_REG = 0x0670

# REG_SYS_FUNC_EN bits.
BIT_FEN_BB_RSTB = 1 << 0
BIT_FEN_BB_GLB_RST = 1 << 1
BIT_FEN_USBA = 1 << 2

# REG_RF_CTRL bits (used in phy_bb_config).
BIT_RF_EN = 1 << 0
BIT_RF_RSTB = 1 << 1
BIT_RF_SDM_RSTB = 1 << 2

# REG_RXPSEL bits.
BIT_RX_PSEL_RST = (1 << 28) | (1 << 29)

# REG_CCK_CHECK bits.
BIT_CHECK_CCK_EN = 1 << 7

# REG_CCK_RPT_FORMAT bits.
BIT_CCK_RPT_FORMAT = 1 << 16

# RFE_INV_MASK (reg.h:717).
RFE_INV_MASK = 0x3FF00000

# RFREG mask for SIPI writes (phy.h:181).
RFREG_MASK = 0xFFFFF

# BB swing (M2-d, rfe=0 defaults — swing2setting[0]).
BB_SWING_MASK = 0xFFE00000   # GENMASK(31, 21)
BB_SWING_2G_DEFAULT = 0x200

# Descriptor rate enum (main.h:250) — used for basic_rates bitmask.
DESC_RATE1M = 0x00
DESC_RATE2M = 0x01
DESC_RATE5_5M = 0x02
DESC_RATE11M = 0x03
DESC_RATE6M = 0x04
DESC_RATE12M = 0x06
DESC_RATE24M = 0x08

# 2.4 GHz basic rates bitmask (REG_RRSR low 20 bits).
BASIC_RATES_2G = (
    (1 << DESC_RATE1M) | (1 << DESC_RATE2M)
    | (1 << DESC_RATE5_5M) | (1 << DESC_RATE11M)
    | (1 << DESC_RATE6M) | (1 << DESC_RATE12M) | (1 << DESC_RATE24M)
)


# ---------------------------------------------------------------------------
# M3-a: channel tune registers + RF18 (CFGCH) bit masks.
# rtw88xxa.c:1292..1521
# ---------------------------------------------------------------------------

REG_DATA_SC = 0x0483
REG_WMAC_TRXPTCL_CTL = 0x0668
REG_RXSB = 0x0A00
REG_CCA2ND = 0x0838
REG_CLKTRK = 0x0860
REG_ADCCLK = 0x08AC
REG_ADC160 = 0x08C4
REG_L1PKTH = 0x0848

# REG_WMAC_TRXPTCL_CTL bits (used by post_set_bw_mode).
BIT_RFMOD = (1 << 7) | (1 << 8)
BIT_RFMOD_40M = 1 << 7
BIT_RFMOD_80M = 1 << 8

# rtw_channel_width enum (main.h:96).
RTW_CHANNEL_WIDTH_20 = 0
RTW_CHANNEL_WIDTH_40 = 1
RTW_CHANNEL_WIDTH_80 = 2

# RF register CFGCH (RF18) bit masks (rtw88xxa.h / phy.h).
RF_CFGCH = 0x18
RF18_BAND_MASK = (1 << 16) | (1 << 9) | (1 << 8)
RF18_CHANNEL_MASK = 0xFF
RF18_RFSI_MASK = (1 << 18) | (1 << 17)
RF18_BW_MASK = (1 << 11) | (1 << 10)
