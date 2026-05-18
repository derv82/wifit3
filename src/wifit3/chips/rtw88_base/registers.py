"""Common rtw88-family MAC/PHY register addresses (reg.h).

These addresses are shared across the rtw88 USB chips (8821a, 8812a, 8822b,
8822c, 8723d, 8814a, ...). Each chip's own `constants.py` may re-export
these and add chip-specific bits.

Source: `data_dumps/rtw88-source-v6.18/reg.h`.
"""

from __future__ import annotations

# --- system control --------------------------------------------------------
REG_SYS_FUNC_EN = 0x0002
REG_SYS_CLKR = 0x0008       # reg.h:28
REG_RSV_CTRL = 0x001C       # reg.h:33
REG_GPIO_MUXCFG = 0x0040    # reg.h:72
REG_LDO_SWR_CTRL = 0x007C   # reg.h:125
REG_MCUFW_CTRL = 0x0080
REG_HIMR0 = 0x00B0
REG_HIMR1 = 0x00B8
REG_SYS_CFG1 = 0x00F0       # chip cut/version/RFE info
REG_SYS_CFG2 = 0x00FC

# --- MAC command / FIFO / queues -------------------------------------------
REG_CR = 0x0100             # Command Register (reg.h:207)
REG_TXDMA_PQ_MAP = 0x010C
REG_TRXFF_BNDY = 0x0114
REG_HMETFR = 0x01CC
REG_LLT_INIT = 0x01E0
REG_RQPN = 0x0200
REG_FIFOPAGE_CTRL_2 = 0x0204
REG_DWBCN0_CTRL = 0x0208
REG_TXDMA_OFFSET_CHK = 0x020C
REG_TXDMA_STATUS = 0x0210
REG_RQPN_NPQ = 0x0214
REG_DWBCN1_CTRL = 0x0228
REG_RQPN_CTRL_1 = 0x0228
REG_RQPN_CTRL_2 = 0x022C
REG_FIFOPAGE_INFO_1 = 0x0230
REG_FIFOPAGE_INFO_2 = 0x0234
REG_FIFOPAGE_INFO_3 = 0x0238
REG_FIFOPAGE_INFO_4 = 0x023C
REG_FIFOPAGE_INFO_5 = 0x0240
REG_RXDMA_STATUS = 0x0288
REG_RXDMA_MODE = 0x0290
REG_H2CQ_CSR = 0x1330              # modern path, NOT 0x0254 (legacy)
BTI_PAGE_OVF = 1 << 2
BIT_BCN_VALID = 1 << 16            # of REG_DWBCN0_CTRL (legacy/8051 path)
BIT_BCN_VALID_V1 = 1 << 15         # of REG_FIFOPAGE_CTRL_2 (modern path)
BIT_MASK_BCN_HEAD_1_V1 = 0xFFF     # of REG_FIFOPAGE_CTRL_2
BIT_ENSWBCN = 1 << 8               # of REG_CR (in upper byte after +1 offset)
BIT_LD_RQPN = 1 << 31              # of REG_RQPN_CTRL_2

# --- TX/HW queue ctrl ------------------------------------------------------
REG_HWSEQ_CTRL = 0x0423
REG_FWHW_TXQ_CTRL = 0x0420
REG_BCNQ_BDNY = 0x0424
REG_MGQ_BDNY = 0x0425
REG_SPEC_SIFS = 0x0428
REG_RETRY_LIMIT = 0x042A

# --- RX rate / aggregation -------------------------------------------------
REG_RRSR = 0x0440
REG_AMPDU_MAX_TIME = 0x0456
REG_AMPDU_MAX_LENGTH = 0x0458
REG_FAST_EDCA_CTRL = 0x0460
REG_SINGLE_AMPDU_CTRL = 0x04C7
REG_MAX_AGGR_NUM = 0x04CA

# --- EDCA / beacon ---------------------------------------------------------
REG_EDCA_VO_PARAM = 0x0500
REG_EDCA_VI_PARAM = 0x0504
REG_EDCA_BE_PARAM = 0x0508
REG_EDCA_BK_PARAM = 0x050C
REG_BCNTCFG = 0x0510
REG_PIFS = 0x0512
REG_SIFS = 0x0514
REG_TBTT_PROHIBIT = 0x0540
REG_BCN_CTRL = 0x0550
REG_DRVERLYINT = 0x0558
REG_BCNDMATIM = 0x0559
REG_USTIME_TSF = 0x055C
REG_BCN_MAX_ERR = 0x055D

# --- RX filter / NAV / drvinfo --------------------------------------------
REG_RX_PKT_LIMIT = 0x060C
REG_RX_DRVINFO_SZ = 0x060F
REG_MAR = 0x0620
REG_USTIME_EDCA = 0x0638
REG_MAC_SPEC_SIFS = 0x063A
REG_ACKTO = 0x0640
REG_NAV_CTRL = 0x0650
REG_RXFLTMAP0 = 0x06A0
REG_RXFLTMAP1 = 0x06A2
REG_RXFLTMAP2 = 0x06A4

# --- USB-specific ----------------------------------------------------------
REG_USB_MOD = 0xF008
REG_USB3_RXITV = 0xF050
REG_USB_HRPWM = 0xFE58

# --- FW header / debug -----------------------------------------------------
REG_FW_DBG7 = 0x00FC + 0x18  # placeholder — chips that use modern FW key check

# --- iDDMA (used by modern FW upload path: 8822b/c, 8814a) -----------------
REG_DDMA_CH0SA = 0x1200
REG_DDMA_CH0DA = 0x1204
REG_DDMA_CH0CTRL = 0x1208

# Modern FW path additionals (reg.h:780..820, mac.c).
REG_CPU_DMEM_CON = 0x1080
BIT_WL_PLATFORM_RST = 1 << 16
BIT_DDMA_EN = 1 << 8
BIT_CPU_CLK_EN = 1 << 14         # of REG_SYS_CLK_CTRL (bit 14, hence high byte bit 6)
REG_CR_EXT = 0x1100
REG_FW_DBG7 = 0x10FC
FW_KEY_MASK = 0xFFFFFF00
ILLEGAL_KEY_GROUP = 0xFAAAAA00
REG_C2HEVT = 0x01A0              # mac.h
C2H_HW_FEATURE_DUMP = 0xFD
C2H_HW_FEATURE_REPORT = 0x19

# REG_SYS_CLK_CTRL (mac.c uses REG_SYS_CLK_CTRL + 1).
REG_SYS_CLK_CTRL = 0x0008
# REG_SYS_STATUS1 (reg.h:202).
REG_SYS_STATUS1 = 0x00F4

# REG_PAD_CTRL1 / REG_GPIO_MUXCFG / REG_LED_CFG bits.
REG_PAD_CTRL1 = 0x0064
BIT_PAPE_WLBT_SEL = 1 << 29
BIT_LNAON_WLBT_SEL = 1 << 28
BIT_FSPI_EN = 1 << 19
BIT_WLRFE_4_5_EN = 1 << 2
BIT_PAPE_SEL_EN = 1 << 25
BIT_LNAON_SEL_EN = 1 << 26

REG_LED_CFG = 0x004C

# REG_SYS_FUNC_EN bits.
BIT_FEN_BB_RSTB = 1 << 0
BIT_FEN_BB_GLB_RST = 1 << 1

# REG_RF_CTRL bits.
REG_RF_CTRL = 0x001F
BIT_RF_SDM_RSTB = 1 << 2
BIT_RF_RSTB = 1 << 1
BIT_RF_EN = 1 << 0

# REG_WLRF1 bits.
REG_WLRF1 = 0x00EC
BIT_WLRF1_BBRF_EN = (1 << 24) | (1 << 25) | (1 << 26)

# REG_RXPSEL.
REG_RXPSEL = 0x0808
BIT_RX_PSEL_RST = (1 << 28) | (1 << 29)

# Bits ----------------------------------------------------------------------
# REG_SYS_FUNC_EN+1
BIT_FEN_CPUEN = 1 << 2

# REG_RSV_CTRL+1
BIT_WLMCU_IOIF = 1 << 0

# REG_MCUFW_CTRL bits (reg.h:131..158). Some bit positions are reused between
# the legacy-MCUFWDL path (8821a/8812a/8723d) and the modern iDDMA path
# (8822b/c, 8814a) — the names below match upstream reg.h exactly.
BIT_ANA_PORT_EN = 1 << 22
BIT_MAC_PORT_EN = 1 << 21
BIT_BOOT_FSPI_EN = 1 << 20
BIT_ROM_DLEN = 1 << 19
BIT_ROM_PGE = 0b111 << 16     # GENMASK(18,16)
BIT_FW_INIT_RDY = 1 << 15
BIT_FW_DW_RDY = 1 << 14
BIT_CPU_CLK_SEL = (1 << 12) | (1 << 13)
BIT_RPWM_TOGGLE = 1 << 7
BIT_RAM_DL_SEL = 1 << 7       # legacy only (alias of RPWM_TOGGLE bit)
BIT_DMEM_CHKSUM_OK = 1 << 6
BIT_WINTINI_RDY = 1 << 6      # legacy only (alias)
BIT_DMEM_DW_OK = 1 << 5
BIT_IMEM_CHKSUM_OK = 1 << 4
BIT_IMEM_DW_OK = 1 << 3
BIT_IMEM_BOOT_LOAD_CHECKSUM_OK = 1 << 2
BIT_FWDL_CHK_RPT = 1 << 2     # legacy only (alias)
BIT_MCUFWDL_RDY = 1 << 1      # legacy only
BIT_MCUFWDL_EN = 1 << 0

# Modern FW_READY (reg.h:152..158). The IDDMA path uses these.
BIT_CHECK_SUM_OK = BIT_IMEM_CHKSUM_OK | BIT_DMEM_CHKSUM_OK
FW_READY = (
    BIT_FW_INIT_RDY | BIT_FW_DW_RDY
    | BIT_IMEM_DW_OK | BIT_DMEM_DW_OK
    | BIT_CHECK_SUM_OK
)
FW_READY_MASK = 0xFFFF & ~BIT_CPU_CLK_SEL   # 0xCFFF

# REG_CR (reg.h:207..220)
BIT_MACRXEN = 1 << 7                  # bit 7 of byte 0 at REG_CR
BIT_MACTXEN = 1 << 6                  # bit 6 of byte 0 at REG_CR
BIT_RXDMA_EN = 1 << 3
BIT_TXDMA_EN = 1 << 2
BIT_HCI_RXDMA_EN = 1 << 1
BIT_HCI_TXDMA_EN = 1 << 0

# REG_BCN_CTRL
BIT_DIS_TSF_UDT = 1 << 4
BIT_EN_BCN_FUNCTION = 1 << 3

# REG_H2CQ_CSR
BIT_H2CQ_FULL = 1 << 31

# REG_DDMA_CH0CTRL (reg.h:815..821)
BIT_DDMACH0_OWN = 1 << 31
BIT_DDMACH0_CHKSUM_EN = 1 << 29
BIT_DDMACH0_CHKSUM_STS = 1 << 27
BIT_DDMACH0_DDMA_MODE = 1 << 26
BIT_DDMACH0_RESET_CHKSUM_STS = 1 << 25
BIT_DDMACH0_CHKSUM_CONT = 1 << 24
BIT_MASK_DDMACH0_DLEN = 0x3FFFF        # 18 bits

# OCP base addresses (mac.c).
OCPBASE_TXBUF_88XX = 0x18780000
OCPBASE_DMEM_88XX = 0x00200000
OCPBASE_RXBUF_FW_88XX = 0x18700000

# --- DMA mapping enum (main.h) --------------------------------------------
RTW_DMA_MAPPING_EXTRA = 0
RTW_DMA_MAPPING_LOW = 1
RTW_DMA_MAPPING_NORMAL = 2
RTW_DMA_MAPPING_HIGH = 3


# --- Common rate/desc enums ------------------------------------------------
DESC_RATE1M = 0x00
DESC_RATE2M = 0x01
DESC_RATE5_5M = 0x02
DESC_RATE11M = 0x03
DESC_RATE6M = 0x04
DESC_RATE9M = 0x05
DESC_RATE12M = 0x06
DESC_RATE18M = 0x07
DESC_RATE24M = 0x08
DESC_RATE36M = 0x09
DESC_RATE48M = 0x0A
DESC_RATE54M = 0x0B

# 2.4 GHz basic rates (1/2/5.5/11/6/12/24M)
BASIC_RATES_2G = (
    (1 << DESC_RATE1M) | (1 << DESC_RATE2M)
    | (1 << DESC_RATE5_5M) | (1 << DESC_RATE11M)
    | (1 << DESC_RATE6M) | (1 << DESC_RATE12M) | (1 << DESC_RATE24M)
)

# rtw_channel_width enum (main.h:96).
RTW_CHANNEL_WIDTH_20 = 0
RTW_CHANNEL_WIDTH_40 = 1
RTW_CHANNEL_WIDTH_80 = 2

# QSEL values (tx.h:62)
TX_DESC_QSEL_TID0 = 0
TX_DESC_QSEL_BEACON = 16
TX_DESC_QSEL_HIGH = 17
TX_DESC_QSEL_MGMT = 18
TX_DESC_QSEL_H2C = 19
