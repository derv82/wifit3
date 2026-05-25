"""Realtek RTL8821AU protocol constants (rtw88 family).

Verified facts captured from `data_dumps/rtw88-source-v6.18/` and pcap
`usb_dumps/captures_rtw88_8821au/capture-1.pcap`. Every value here is
either a direct source citation or a wire-confirmed observation. See
`RTL8821AU.md` for provenance.
"""

# USB vendor IDs (rtw_8821au_id_table in rtw8821au.c) ---------------------
USB_VID_REALTEK = 0x0BDA

# AWUS036ACS is the first entry in rtw_8821au_id_table.
USB_PID_AWUS036ACS = 0x0811

# Vendor control-transfer request (usb.h):
#   RTW_USB_CMD_REQ   = 0x05   bRequest for both read and write
#   RTW_USB_CMD_READ  = 0xC0   bmRequestType (vendor IN)
#   RTW_USB_CMD_WRITE = 0x40   bmRequestType (vendor OUT)
#   RTW_USB_VENQT_CMD_IDX = 0x00  wIndex
USB_CMD_REQ = 0x05
USB_REQTYPE_READ = 0xC0
USB_REQTYPE_WRITE = 0x40
USB_VENQT_CMD_IDX = 0x00

# Register offsets (reg.h) ------------------------------------------------
REG_SYS_FUNC_EN = 0x0002
REG_RSV_CTRL = 0x001C    # reg.h:33
REG_SYS_CFG1 = 0x00F0    # chip cut/version/RFE info
REG_SYS_CFG2 = 0x00FC
REG_MCUFW_CTRL = 0x0080

# More registers touched during MAC power-on / FW upload (reg.h)
REG_SYS_CLKR = 0x0008       # reg.h:28
REG_GPIO_MUXCFG = 0x0040    # reg.h:72
REG_LDO_SWR_CTRL = 0x007C   # reg.h:125
REG_CR = 0x0100             # Command Register (reg.h:207)
REG_HWSEQ_CTRL = 0x0423     # reg.h:390

# Bit defs for non-MCUFW registers we touch during FW load
BIT_FEN_CPUEN = 1 << 2      # of REG_SYS_FUNC_EN+1 (reg.h:12)
BIT_WLMCU_IOIF = 1 << 0     # of REG_RSV_CTRL+1   (reg.h:37)
BIT_WAKEPAD_EN = 1 << 3     # of REG_SYS_CLKR     (reg.h:30)
BIT_EN_SIC = 1 << 12        # of REG_GPIO_MUXCFG  (reg.h:74)
BIT_LDO = 1 << 24           # of REG_SYS_CFG1     (reg.h:188)
LDO_SEL = 0xC3              # write8 to REG_LDO_SWR_CTRL when BIT_LDO is set
SPS_SEL = 0x83              # write8 to REG_LDO_SWR_CTRL when BIT_LDO is clear

# REG_CR power-state magic values (mac.c:291)
REG_CR_OFF_VALUE = 0xEA     # value read from REG_CR when card is in disabled state

# REG_RCR receive-config accept-policy bits [SRC rtw88 reg.h:527-534]. The
# WIRE truth: airmon-ng monitor sets REG_RCR = 0xf410400f, whose low byte is
# AAP|APM|AM|AB (promiscuous) with CBSSID_BCN|CBSSID_DATA *cleared*. The kernel
# leaves net-type at MGD_LINKED(2) even in monitor — net-type is NOT the gate.
BIT_CBSSID_BCN = 1 << 7     # check BSSID for beacons   — CLEAR for monitor
BIT_CBSSID_DATA = 1 << 6    # check BSSID for data      — CLEAR for monitor
BIT_AB = 1 << 3             # accept broadcast
BIT_AM = 1 << 2             # accept multicast
BIT_APM = 1 << 1            # accept physical match (our MAC)
BIT_AAP = 1 << 0            # accept ALL physical (promiscuous) — the key monitor bit

# REG_MCUFW_CTRL bits (reg.h) --------------------------------------------
BIT_ROM_DLEN = 1 << 19
BIT_ROM_PGE = 0b111 << 16     # GENMASK(18,16)
BIT_SHIFT_ROM_PGE = 16
BIT_FW_INIT_RDY = 1 << 15
BIT_FW_DW_RDY = 1 << 14
BIT_RAM_DL_SEL = 1 << 7       # legacy only
BIT_WINTINI_RDY = 1 << 6      # legacy only
BIT_FWDL_CHK_RPT = 1 << 2     # legacy only — the *upload-complete ACK*
BIT_MCUFWDL_RDY = 1 << 1      # legacy only
BIT_MCUFWDL_EN = 1 << 0

# Legacy FW READY mask (reg.h FW_READY_LEGACY) — bit pattern that appears
# in REG_MCUFW_CTRL after a successful CPU reset post-upload.
FW_READY_LEGACY = (
    BIT_MCUFWDL_RDY | BIT_FWDL_CHK_RPT | BIT_WINTINI_RDY | BIT_RAM_DL_SEL
)  # = 0xC6

# Firmware upload protocol (fw.h, mac.c, usb.c) --------------------------
FW_START_ADDR_LEGACY = 0x1000   # wValue start for each page upload
DLFW_PAGE_SIZE_LEGACY = 0x1000  # 4096 — one page
DLFW_BLK_SIZE_LEGACY = 4
FW_HDR_LEGACY_SIZE = 32         # sizeof(struct rtw_fw_hdr_legacy)

# Chunk sizes the kernel uses inside rtw_usb_write_firmware_page (for
# non-8723D chips); we stream FW in 196-byte chunks, falling to 8 then 1
# for the tail of the last page.
FW_CHUNK_SIZES_BY_PRIORITY = (196, 8, 1)


# ---------------------------------------------------------------------------
# Post-FW MAC init registers and constants — used by M4b (rtw88xxa_power_on
# continuation, lines 1055..1175 in rtw88xxa.c).
# ---------------------------------------------------------------------------

# Chip parameters (rtw8821a_hw_spec in rtw8821a.c:1143).
TXFF_SIZE = 65536
RXFF_SIZE = 16128
PAGE_SIZE = 256
RSVD_DRV_PG_NUM = 8
CSI_BUF_PG_NUM = 0
USB_TX_AGG_DESC_NUM = 6

# Shared constants from mac.h.
REPORT_BUF = 128
PHY_STATUS_SIZE = 4

# Page table for USB-2-bulkout 8821A (rtw8821a.c:894, index [2]).
PG_TBL_USB2_HQ_NUM = 8
PG_TBL_USB2_NQ_NUM = 0
PG_TBL_USB2_LQ_NUM = 0
PG_TBL_USB2_EXQ_NUM = 0
PG_TBL_USB2_GAPQ_NUM = 1

# RQPN table for USB-2-bulkout 8821A (rtw8821a.c:903, index [2]).
# dma_map_{hi,mg,bk,be,vi,vo} = {NORMAL, NORMAL, LOW, LOW, EXTRA, HIGH}
# (RTW_DMA_MAPPING_* enum: EXTRA=0, LOW=1, NORMAL=2, HIGH=3)
RTW_DMA_MAPPING_EXTRA = 0
RTW_DMA_MAPPING_LOW = 1
RTW_DMA_MAPPING_NORMAL = 2
RTW_DMA_MAPPING_HIGH = 3

RQPN_USB2_HI = RTW_DMA_MAPPING_NORMAL
RQPN_USB2_MG = RTW_DMA_MAPPING_NORMAL
RQPN_USB2_BK = RTW_DMA_MAPPING_LOW
RQPN_USB2_BE = RTW_DMA_MAPPING_LOW
RQPN_USB2_VI = RTW_DMA_MAPPING_EXTRA
RQPN_USB2_VO = RTW_DMA_MAPPING_HIGH

# Register offsets (reg.h).
REG_TXDMA_PQ_MAP = 0x010C
REG_TRXFF_BNDY = 0x0114        # u8 at +0, u16 at +2
REG_HIMR0 = 0x00B0
REG_HIMR1 = 0x00B8
REG_HMETFR = 0x01CC
REG_LLT_INIT = 0x01E0
REG_RQPN = 0x0200
REG_FIFOPAGE_CTRL_2 = 0x0204
REG_DWBCN0_CTRL = 0x0208
REG_TXDMA_OFFSET_CHK = 0x020C
REG_RQPN_NPQ = 0x0214
REG_DWBCN1_CTRL = 0x0228
REG_RXDMA_STATUS = 0x0288
REG_RXDMA_MODE = 0x0290
REG_FWHW_TXQ_CTRL = 0x0420
REG_BCNQ_BDNY = 0x0424
REG_MGQ_BDNY = 0x0425
REG_WMAC_LBK_BF_HD = 0x045D
REG_SPEC_SIFS = 0x0428
REG_RETRY_LIMIT = 0x042A
REG_RRSR = 0x0440
REG_ARFR0 = 0x0444
REG_ARFRH0 = 0x0448
REG_ARFR1_V1 = 0x044C
REG_ARFRH1_V1 = 0x0450
REG_AMPDU_MAX_TIME = 0x0456
REG_AMPDU_MAX_LENGTH = 0x0458
REG_FAST_EDCA_CTRL = 0x0460
REG_ARFR2_V1 = 0x048C
REG_ARFRH2_V1 = 0x0490
REG_ARFR3_V1 = 0x0494
REG_ARFRH3_V1 = 0x0498
REG_SINGLE_AMPDU_CTRL = 0x04C7
REG_MAX_AGGR_NUM = 0x04CA
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
REG_RX_PKT_LIMIT = 0x060C
REG_RX_DRVINFO_SZ = 0x060F
REG_MAR = 0x0620
REG_USTIME_EDCA = 0x0638
REG_MAC_SPEC_SIFS = 0x063A
REG_ACKTO = 0x0640
REG_RXFLTMAP0 = 0x06A0
REG_RXFLTMAP1 = 0x06A2
REG_RXFLTMAP2 = 0x06A4
REG_RCR = 0x0608                       # reg.h:502
REG_WMAC_OPTION_FUNCTION = 0x07D0      # reg.h
REG_USB_MOD = 0xF008
REG_USB3_RXITV = 0xF050

# Bits.
BIT_LLT_WRITE_ACCESS = 1 << 30        # REG_LLT_INIT
BIT_DROP_DATA_EN = 1 << 9             # REG_TXDMA_OFFSET_CHK
BIT_LD_RQPN = 1 << 31                 # REG_RQPN
BIT_EN_SINGLE_APMDU = 1 << 7          # REG_SINGLE_AMPDU_CTRL
BIT_MACRXEN = 1 << 7                  # bit 7 of byte 0 at REG_CR
BIT_MACTXEN = 1 << 6                  # bit 6 of byte 0 at REG_CR
BIT_DIS_TSF_UDT = 1 << 4              # REG_BCN_CTRL (also high byte)
BIT_EN_BCN_FUNCTION = 1 << 3          # REG_BCN_CTRL
BIT_APP_PHYSTS = 1 << 28              # REG_RCR — append phy_status to drv_info

# REG_RXDMA_MODE encoding (used by rtw_usb_init_burst_pkt_len).
BIT_DMA_MODE = 1 << 1
BIT_DMA_BURST_CNT = 0b11 << 2
BIT_MASK_DMA_BURST_SIZE = 0b11 << 4
BIT_SHIFT_DMA_BURST_SIZE = 4
BIT_DMA_BURST_SIZE_64 = 2
BIT_DMA_BURST_SIZE_512 = 1
BIT_DMA_BURST_SIZE_1024 = 0

# Pre-encoded WLAN_TBTT_TIME (rtw88xxa.h:69):
#   WLAN_TBTT_PROHIBIT(0x04) | (WLAN_TBTT_HOLD_TIME(0x64) << 8) = 0x6404
WLAN_TBTT_TIME = (0x04 | (0x64 << 8))   # = 0x6404
WLAN_BCN_DMA_TIME = 0x02

# BIT_TXDMA_*_MAP shifts (REG_TXDMA_PQ_MAP). Each lane is 2 bits.
BIT_SHIFT_TXDMA_VOQ_MAP = 4
BIT_SHIFT_TXDMA_VIQ_MAP = 6
BIT_SHIFT_TXDMA_BEQ_MAP = 8
BIT_SHIFT_TXDMA_BKQ_MAP = 10
BIT_SHIFT_TXDMA_MGQ_MAP = 12
BIT_SHIFT_TXDMA_HIQ_MAP = 14
BIT_MASK_TXDMA_MAP = 0x3

# For the BTcoex EFUSE read we don't yet do — keep here for M4c.
# REG_WL_BT_PWR_CTRL / BIT_BT_FUNC_EN unused for now.


# ---------------------------------------------------------------------------
# M4c: BB/RF/band-switch registers and constants.
# ---------------------------------------------------------------------------

# REG_SYS_FUNC_EN bits.
BIT_FEN_BB_RSTB = 1 << 0
BIT_FEN_BB_GLB_RST = 1 << 1
BIT_FEN_USBA = 1 << 2

# REG_RF_CTRL / REG_RF_B_CTRL bits.
REG_RF_CTRL = 0x001F
REG_RF_B_CTRL = 0x0076
BIT_RF_EN = 1 << 0
BIT_RF_RSTB = 1 << 1
BIT_RF_SDM_RSTB = 1 << 2

# Misc registers.
REG_AFE_CTRL3 = 0x002C
REG_ACLK_MON = 0x003E
REG_LED_CFG = 0x004C
REG_SYS_SDIO_CTRL = 0x0070
REG_CCK_CHECK = 0x0454
REG_QUEUE_CTRL = 0x04C6
REG_BAR_MODE_CTRL = 0x04CC
REG_TX_RPT_TIME = 0x04F0
REG_NAV_CTRL = 0x0650
RTW_SEC_CMD_REG = 0x0670
REG_EARLY_MODE_CONTROL = 0x02BC
REG_CCK_RPT_FORMAT = 0x0804
REG_RXPSEL = 0x0808
REG_TXPSEL = 0x080C
REG_CCK_RX = 0x0A04
REG_TXSCALE_A = 0x0C1C
REG_LSSI_WRITE_A = 0x0C90
REG_RFE_PINMUX_A = 0x0CB0
REG_RFE_INV_A = 0x0CB4
REG_TXSCALE_B = 0x0E1C
REG_LSSI_WRITE_B = 0x0E90
REG_USB_HRPWM = 0xFE58

# Bit defs.
BIT_DPDT_SEL_EN = 1 << 23
BIT_DPDT_WL_SEL = 1 << 24
BIT_RX_PSEL_RST = (1 << 28) | (1 << 29)
BIT_CHECK_CCK_EN = 1 << 7
BIT_CCK_RPT_FORMAT = 1 << 16
BB_SWING_MASK = 0xFFE00000   # GENMASK(31, 21)

# RFREG mask for SIPI writes (phy.h:181).
RFREG_MASK = 0xFFFFF

# Descriptor rate enum (main.h:250) — used for basic_rates bitmask.
DESC_RATE1M = 0x00
DESC_RATE2M = 0x01
DESC_RATE5_5M = 0x02
DESC_RATE11M = 0x03
DESC_RATE6M = 0x04
DESC_RATE12M = 0x06
DESC_RATE24M = 0x08

# 2.4 GHz basic rates: 1M/2M/5.5M/11M/6M/12M/24M
BASIC_RATES_2G = (
    (1 << DESC_RATE1M)
    | (1 << DESC_RATE2M)
    | (1 << DESC_RATE5_5M)
    | (1 << DESC_RATE11M)
    | (1 << DESC_RATE6M)
    | (1 << DESC_RATE12M)
    | (1 << DESC_RATE24M)
)

# TX BB swing table (rtw88xxa.c:644).
BB_SWING_2G_DEFAULT = 0x200    # tx_bb_swing_setting_2g=0 → swing2setting[0]


# ---------------------------------------------------------------------------
# M6: set_channel registers and RF constants.
# ---------------------------------------------------------------------------

REG_DATA_SC = 0x0483
REG_WMAC_TRXPTCL_CTL = 0x0668
REG_CCA2ND = 0x0838
REG_L1PKTH = 0x0848
REG_CLKTRK = 0x0860
REG_ADCCLK = 0x08AC
REG_ADC160 = 0x08C4
REG_HSSI_READ = 0x08B0
REG_BWINDICATION = 0x0834
REG_RXSB = 0x0A00
REG_3WIRE_SWA = 0x0C00
REG_3WIRE_SWB = 0x0E00
REG_PI_READ_A = 0x0D04
REG_SI_READ_A = 0x0D08
REG_PI_READ_B = 0x0D44
REG_SI_READ_B = 0x0D48

BIT_RFMOD = (1 << 7) | (1 << 8)
BIT_RFMOD_40M = 1 << 7
BIT_RFMOD_80M = 1 << 8

RF_CFGCH = 0x18
RF18_BAND_MASK = (1 << 16) | (1 << 9) | (1 << 8)
RF18_CHANNEL_MASK = 0xFF
RF18_RFSI_MASK = (1 << 18) | (1 << 17)
RF18_BW_MASK = (1 << 11) | (1 << 10)

# rtw_channel_width enum (main.h:96).
RTW_CHANNEL_WIDTH_20 = 0
RTW_CHANNEL_WIDTH_40 = 1
RTW_CHANNEL_WIDTH_80 = 2

# RTW_SC_* primary-channel index (main.h:107).
RTW_SC_20_UPPER = 1
RTW_SC_20_LOWER = 2
RTW_SC_20_UPMOST = 3
RTW_SC_20_LOWEST = 4
RTW_SC_40_UPPER = 9
RTW_SC_40_LOWER = 10
