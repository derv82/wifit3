"""RTL8188EUS — MAC register addresses + bit flags needed for M1.

Symbols mirror `data_dumps/rtl8xxxu-source-v6.18/regs.h` (line numbers
cited inline). Only the constants the FW-upload + 8051-ready path needs
are pulled in for M1; MAC/PHY/RF init constants come in later milestones.
"""
from __future__ import annotations

# ---- USB vendor-control wire protocol --------------------------------
# `rtl8xxxu.h:34-36`
USB_CMD_REQ = 0x05
USB_REQTYPE_READ = 0xC0
USB_REQTYPE_WRITE = 0x40
USB_VENQT_CMD_IDX = 0x00
USB_CONTROL_TIMEOUT_MS = 500  # `rtl8xxxu.h:28  RTW_USB_CONTROL_MSG_TIMEOUT`

# ---- MAC register addresses (regs.h) ---------------------------------
REG_SYS_ISO_CTRL = 0x0000           # regs.h:9
REG_SYS_FUNC = 0x0002               # regs.h:16
REG_APS_FSMCO = 0x0004              # regs.h:34
REG_SYS_CLKR = 0x0008               # regs.h:47
REG_RSV_CTRL = 0x001C               # regs.h:72
REG_LDOA15_CTRL = 0x0020            # regs.h:81
REG_LDOV12D_CTRL = 0x0021           # regs.h:88
REG_LPLDO_CTRL = 0x0023             # regs.h:95
REG_AFE_XTAL_CTRL = 0x0024          # regs.h:99
REG_MCU_FW_DL = 0x0080              # regs.h:219
REG_SYS_CFG = 0x00F0                # regs.h:312
REG_CR = 0x0100                     # regs.h:370
REG_PBP = 0x0104                    # regs.h:391
REG_TRXFF_BNDY = 0x0114             # regs.h:423
REG_HMTFR = 0x01CC                  # regs.h:456
REG_LLT_INIT = 0x01E0               # regs.h:462
REG_RQPN = 0x0200                   # regs.h:477
REG_TDECTRL = 0x0208                # regs.h:484
REG_RQPN_NPQ = 0x0214               # regs.h:492
REG_TXPKTBUF_BCNQ_BDNY = 0x0424     # regs.h:550
REG_TXPKTBUF_MGQ_BDNY = 0x0425      # regs.h:551
REG_MAX_AGGR_NUM = 0x04CA           # regs.h:632
REG_TXPKTBUF_WMAC_LBK_BF_HD = 0x045D  # regs.h:609
REG_FW_START_ADDRESS = 0x1000       # regs.h:1200

# ---- REG_SYS_FUNC bits (regs.h:17-30) --------------------------------
SYS_FUNC_BBRSTB = 1 << 0
SYS_FUNC_BB_GLB_RSTN = 1 << 1
SYS_FUNC_CPU_ENABLE = 1 << 10
SYS_FUNC_DIO_RF = 1 << 13

# ---- REG_APS_FSMCO bits (regs.h:35-45) -------------------------------
APS_FSMCO_MAC_ENABLE = 1 << 8
APS_FSMCO_HW_SUSPEND = 1 << 11
APS_FSMCO_PCIE = 1 << 12
APS_FSMCO_HW_POWERDOWN = 1 << 15
APS_FSMCO_POWER_READY = 1 << 17     # checked in 8188e.c:1012 ("0x04[17]=1 power ready")

# ---- REG_MCU_FW_DL bits (regs.h:220-227) -----------------------------
MCU_FW_DL_ENABLE = 1 << 0
MCU_FW_DL_READY = 1 << 1
MCU_FW_DL_CSUM_REPORT = 1 << 2
MCU_WINT_INIT_READY = 1 << 6        # this is the "8051 ready" ack bit
MCU_FW_RAM_SEL = 1 << 7             # 1 = FW already running in RAM
MCU_FW_DL_8051_RESET_BIT = 1 << 19  # bit cleared in REG_MCU_FW_DL to reset 8051 (core.c:2049)

# ---- REG_CR bits (regs.h:371-381) ------------------------------------
CR_HCI_TXDMA_ENABLE = 1 << 0
CR_HCI_RXDMA_ENABLE = 1 << 1
CR_TXDMA_ENABLE = 1 << 2
CR_RXDMA_ENABLE = 1 << 3
CR_PROTOCOL_ENABLE = 1 << 4
CR_SCHEDULE_ENABLE = 1 << 5
CR_MAC_TX_ENABLE = 1 << 6           # regs.h:377 — flipped on in M2 (8188e.c:1300)
CR_MAC_RX_ENABLE = 1 << 7           # regs.h:378 — flipped on in M2
CR_SECURITY_ENABLE = 1 << 9
CR_CALTIMER_ENABLE = 1 << 10

# Aggregate as written by `rtl8188eu_power_on` (8188e.c:1185-1188).
# MAC_TX_ENABLE/MAC_RX_ENABLE are intentionally NOT included here —
# the 88E has a HW bug requiring those to be set only *after* REG_TRXFF_BNDY,
# which happens in later milestones (see comment 8188e.c:1180-1183).
CR_INIT_POWER_ON = (
    CR_HCI_TXDMA_ENABLE
    | CR_HCI_RXDMA_ENABLE
    | CR_TXDMA_ENABLE
    | CR_RXDMA_ENABLE
    | CR_PROTOCOL_ENABLE
    | CR_SCHEDULE_ENABLE
    | CR_SECURITY_ENABLE
    | CR_CALTIMER_ENABLE
)

# ---- Firmware upload tunables ----------------------------------------
RTL_FW_PAGE_SIZE = 4096             # rtl8xxxu.h:76
RTL8XXXU_FIRMWARE_POLL_MAX = 1000   # rtl8xxxu.h:77
RTL8XXXU_MAX_REG_POLL = 500         # rtl8xxxu.h:29
FW_WRITE_BLOCK_SIZE = 196           # `rtl8188eu_fops.writeN_block_size` (8188e.c:1863)
FW_HEADER_SIZE = 32                 # sizeof(struct rtl8xxxu_firmware_header) (rtl8xxxu.h:908)

# Expected signature in FW header (signature & 0xfff0) — `core.c:2136`
FW_SIGNATURE_88E = 0x88E0

# ---- LLT operation encoding (regs.h:463-466) -------------------------
LLT_OP_INACTIVE = 0x0
LLT_OP_WRITE = 0x1 << 30
LLT_OP_MASK = 0x3 << 30

# ---- REG_PBP fields (regs.h:392-395) ---------------------------------
PBP_PAGE_SIZE_RX_SHIFT = 0
PBP_PAGE_SIZE_TX_SHIFT = 4
PBP_PAGE_SIZE_128 = 0x1

# ---- REG_RQPN fields (regs.h:478-481, 493-494) -----------------------
RQPN_HI_PQ_SHIFT = 0
RQPN_LO_PQ_SHIFT = 8
RQPN_PUB_PQ_SHIFT = 16
RQPN_LOAD = 1 << 31
RQPN_NPQ_SHIFT = 0
RQPN_EPQ_SHIFT = 16

# ---- 8188e fileops values (8188e.c:1863-1884) ------------------------
TRXFF_BOUNDARY_8188E = 0x25FF       # `.trxff_boundary` (8188e.c:1876)
TOTAL_PAGE_NUM_8188E = 0xA9         # `.total_page_num = TX_TOTAL_PAGE_NUM_8188E` (rtl8xxxu.h:41)
PAGE_NUM_HI_PQ_8188E = 0x29         # `.page_num_hi` (rtl8xxxu.h:57)
PAGE_NUM_LO_PQ_8188E = 0x1C         # `.page_num_lo` (rtl8xxxu.h:58)
PAGE_NUM_NORM_PQ_8188E = 0x1C       # `.page_num_norm` (rtl8xxxu.h:59)
LAST_LLT_ENTRY_8188E = 175          # `.last_llt_entry = 175` (8188e.c:1884)
MAX_AGGR_NUM_8188E = 0x0707         # `rtl8xxxu_init_mac`'s 8188E branch (core.c:2218-2220)

# ---- M2 polling tunables ---------------------------------------------
LLT_WRITE_POLL_MAX = 20             # `rtl8xxxu_llt_write` poll cap (core.c:2514)

# ---- REG_RF_CTRL (regs.h:76-79) + REG_SYS_FUNC extras (regs.h:19-21) -
REG_RF_CTRL = 0x001F
RF_ENABLE = 1 << 0
RF_RSTB = 1 << 1
RF_SDMRSTB = 1 << 2

SYS_FUNC_USBA = 1 << 2              # regs.h:19
SYS_FUNC_USBD = 1 << 4              # regs.h:21

# ---- FPGA0 / LSSI registers for RF SIPI (regs.h:905-953, 920) --------
REG_FPGA0_XA_HSSI_PARM2 = 0x0824    # regs.h:905
REG_FPGA0_XB_HSSI_PARM2 = 0x082C    # regs.h:907
REG_FPGA0_XA_LSSI_PARM = 0x0840     # regs.h:920
REG_FPGA0_XA_RF_INT_OE = 0x0860     # regs.h:931
REG_FPGA0_XB_RF_INT_OE = 0x0864     # regs.h:932
REG_FPGA0_XA_RF_SW_CTRL = 0x0870    # regs.h:943 (16-bit)
REG_FPGA0_XB_RF_SW_CTRL = 0x0872    # regs.h:944 (16-bit)

FPGA0_HSSI_3WIRE_DATA_LEN = 0x800   # regs.h:908
FPGA0_HSSI_3WIRE_ADDR_LEN = 0x400   # regs.h:909
FPGA0_RF_RFENV = 1 << 4             # regs.h:953

# rtl8xxxu_write_rfreg encoding (core.c:922-923 + regs.h:922-924)
FPGA0_LSSI_PARM_ADDR_SHIFT = 20
FPGA0_LSSI_PARM_DATA_MASK = 0x000FFFFF   # 20-bit RF data

# ---- SIPI READ primitives (regs.h:903-913, 983-986) -----------------
REG_FPGA0_XA_HSSI_PARM1 = 0x0820         # regs.h:903 (RF 3-wire register)
FPGA0_HSSI_PARM1_PI = 1 << 8             # regs.h:904
FPGA0_HSSI_PARM2_ADDR_SHIFT = 23         # regs.h:910
FPGA0_HSSI_PARM2_ADDR_MASK = 0x7F800000  # regs.h:911 (0xff << 23)
FPGA0_HSSI_PARM2_EDGE_READ = 1 << 31     # regs.h:913
REG_FPGA0_XA_LSSI_READBACK = 0x08A0      # regs.h:983
REG_HSPI_XA_READBACK = 0x08B8            # regs.h:986
RF_READBACK_MASK = 0x000FFFFF            # `retval &= 0xfffff` (core.c:899)

# ---- M4 channel-set registers + fields (regs.h, 8188e.c:423-522) ----
REG_BW_OPMODE = 0x0603                   # regs.h:738
BW_OPMODE_20MHZ = 1 << 2                 # regs.h:739
REG_FPGA0_RF_MODE = 0x0800               # regs.h:884
REG_FPGA1_RF_MODE = 0x0900               # regs.h:989
FPGA_RF_MODE = 1 << 0                    # regs.h:885

# RF6052 RF chip register layout — read via SIPI through write_rfreg/read_rfreg
RF6052_REG_MODE_AG = 0x18                # regs.h:1333 (RF channel + BW switch)
MODE_AG_CHANNEL_MASK = 0x3FF             # regs.h:1334 (bits[9:0])
MODE_AG_BW_MASK = (1 << 10) | (1 << 11)  # regs.h:1336
MODE_AG_BW_20MHZ_8723B = (1 << 10) | (1 << 11)  # regs.h:1337 (yes — both bits set for 20 MHz)

# ---- M5 RX path registers (regs.h) -----------------------------------
REG_HIMR0 = 0x00B0                       # regs.h:238 (interrupt mask 0)
REG_HISR0 = 0x00B4                       # regs.h:272 (interrupt status 0)
REG_HIMR1 = 0x00B8                       # regs.h:273 (interrupt mask 1)
REG_RCR = 0x0608                         # regs.h:746 (RX Control Register)
REG_RX_DRVINFO_SZ = 0x060F               # regs.h:787
REG_USB_SPECIAL_OPTION = 0xFE55          # regs.h:1248
USB_SPEC_INT_BULK_SELECT = 1 << 4        # regs.h:1250

# ---- REG_HIMR0 / REG_HIMR1 bits (regs.h:241-295) ---------------------
IMR0_PSTIMEOUT = 1 << 29                 # regs.h:241
IMR0_TBDER = 1 << 26                     # regs.h:244
IMR0_CPWM2 = 1 << 9                      # regs.h:260
IMR0_CPWM = 1 << 8                       # regs.h:262
IMR1_TXERR = 1 << 11                     # regs.h:290
IMR1_RXERR = 1 << 10                     # regs.h:292
IMR1_TXFOVW = 1 << 9                     # regs.h:294
IMR1_RXFOVW = 1 << 8                     # regs.h:295

# 8188e-specific interrupt mask values (core.c:4110-4113)
HIMR0_8188E = IMR0_PSTIMEOUT | IMR0_TBDER | IMR0_CPWM | IMR0_CPWM2
HIMR1_8188E = IMR1_TXERR | IMR1_RXERR | IMR1_TXFOVW | IMR1_RXFOVW

# ---- REG_RCR bits (regs.h:746-782) -----------------------------------
RCR_ACCEPT_AP = 1 << 0                   # regs.h:747 — Accept all unicast pkt
RCR_ACCEPT_PHYS_MATCH = 1 << 1           # regs.h:748
RCR_ACCEPT_MCAST = 1 << 2                # regs.h:749
RCR_ACCEPT_BCAST = 1 << 3                # regs.h:750
RCR_ACCEPT_CRC32 = 1 << 8                # regs.h:758
RCR_ACCEPT_ICV = 1 << 9                  # regs.h:759
RCR_ACCEPT_DATA_FRAME = 1 << 11          # regs.h:760 — Accept ALL data pkts (EAPOL lives here!)
RCR_ACCEPT_CTRL_FRAME = 1 << 12          # regs.h:762 — Accept ALL control pkts (ACKs etc)
RCR_ACCEPT_MGMT_FRAME = 1 << 13          # regs.h:764
RCR_HTC_LOC_CTRL = 1 << 14               # regs.h:766
RCR_APPEND_PHYSTAT = 1 << 28             # regs.h:780
RCR_APPEND_ICV = 1 << 29                 # regs.h:781
RCR_APPEND_MIC = 1 << 30                 # regs.h:782

# Combined RCR for full MONITOR-mode RX.
#
# The kernel's `core.c:4130-4133` value is its STATION-MODE RCR (filter
# to packets addressed to us). When the user runs `iw set monitor` the
# mac80211 `configure_filter` callback toggles in ACCEPT_AP / DATA_FRAME
# / CTRL_FRAME / CRC32 / ICV; we go directly to the monitor superset
# here so EAPOL, ACKs, and frames-for-other-stations all reach our parser.
RCR_MONITOR = (
    RCR_ACCEPT_AP                     # all unicast (not just to us)
    | RCR_ACCEPT_PHYS_MATCH
    | RCR_ACCEPT_MCAST
    | RCR_ACCEPT_BCAST
    | RCR_ACCEPT_CRC32                # accept CRC-failed frames (analysis)
    | RCR_ACCEPT_ICV                  # accept ICV-failed frames
    | RCR_ACCEPT_DATA_FRAME           # **critical** — EAPOL lives here
    | RCR_ACCEPT_CTRL_FRAME           # ACKs, RTS, CTS, etc.
    | RCR_ACCEPT_MGMT_FRAME
    | RCR_HTC_LOC_CTRL
    | RCR_APPEND_PHYSTAT
    | RCR_APPEND_ICV
    | RCR_APPEND_MIC
)

# ---- M5b enable_rf registers (regs.h:671, 1057-1064) -----------------
REG_TXPAUSE = 0x0522                     # regs.h:671
REG_OFDM0_TRX_PATH_ENABLE = 0x0C04       # regs.h:1057
OFDM_RF_PATH_RX_MASK = 0x0F              # regs.h:1058
OFDM_RF_PATH_RX_A = 1 << 0               # regs.h:1059
OFDM_RF_PATH_TX_MASK = 0xF0              # regs.h:1063
OFDM_RF_PATH_TX_A = 1 << 4               # regs.h:1064

# ---- M5c CCK/OFDM block enable + 8188e GPIO (regs.h:141-142, 887-888) -
# Different bits from FPGA_RF_MODE (bit 0) which is the 40/20-MHz toggle.
FPGA_RF_MODE_CCK = 1 << 24               # regs.h:887
FPGA_RF_MODE_OFDM = 1 << 25              # regs.h:888

REG_GPIO_MUXCFG = 0x0040                 # regs.h:141
GPIO_MUXCFG_IO_SEL_ENBT = 1 << 5         # regs.h:142 (BT-coex enable)

# ---- M5 RX descriptor layout (rtl8xxxu.h:135-273) --------------------
# `struct rtl8xxxu_rxdesc16` is **24 bytes** despite the name: 5 u32-bitfield
# words inside the #ifdef __LITTLE_ENDIAN block (20 B) PLUS a 6th `u32 tsfl`
# declared OUTSIDE the endian block at rtl8xxxu.h:267 (4 B). The "16" refers
# to the descriptor format generation (vs the 32-byte `rtl8xxxu_rxdesc24`),
# not the byte size. Eyeballing the struct without scrolling past #endif
# yields a 16- or 20-byte wrong answer — we use the correct 24 here.
RX_PKT_DESC_SZ_8188E = 24                # = 6 × u32 (including u32 tsfl at end)
RX_FRAME_ALIGN_8188E = 128               # roundup(..., 128) (core.c:6301)
PHY_STATS_SZ_8188E = 32                  # REG_RX_DRVINFO_SZ=4 → 4 * 8 = 32 bytes

# Byte offset of `cck_sig_qual_ofdm_pwdb_all` within `struct rtl8723au_phy_stats`
# (rtl8xxxu.h:607). Layout before this field:
#   path_agc[2]    = 2 × sizeof(struct phy_rx_agc_info) = 2 × 1 = 2 bytes
#   ch_corr[2]     = 2 bytes
# → pwdb starts at byte 4. NOT byte 6 — `phy_rx_agc_info` is 1 byte not 2
# (single u8 with `gain:7, trsw:1` bitfields, rtl8xxxu.h:593-599).
PHY_STATS_PWDB_OFFSET = 4

# ---- M6 TX descriptor + queue routing --------------------------------
TX_DESC_SZ_8188E = 32                    # sizeof(struct rtl8xxxu_txdesc32)

# txdw0 (byte 3 of the descriptor) — rtl8xxxu.h:474-481
TXDESC_BROADMULTICAST = 1 << 0           # set when DA is bcast/mcast
TXDESC_LAST_SEGMENT = 1 << 2
TXDESC_FIRST_SEGMENT = 1 << 3
TXDESC_OWN = 1 << 7                      # chip owns this descriptor (hand off)

# txdw1 — TX queue routing (rtl8xxxu.h:494-502)
TXDESC_QUEUE_SHIFT = 8                   # rtl8xxxu.h:494
TXDESC_QUEUE_MGNT = 0x12                 # rtl8xxxu.h:502 — the MGMT queue id

# txdw2 / txdw4 / txdw5 flags used by fill_txdesc_v3 (rtl8xxxu.h:526-584)
TXDESC40_AGG_BREAK = 1 << 16             # rtl8xxxu.h:526 (used by v3 too)
TXDESC_ANTENNA_SELECT_A = 1 << 24        # rtl8xxxu.h:534
TXDESC_ANTENNA_SELECT_B = 1 << 25        # rtl8xxxu.h:535
TXDESC_ANTENNA_SELECT_C = 1 << 29        # rtl8xxxu.h:584
TXDESC32_USE_DRIVER_RATE = 1 << 8        # rtl8xxxu.h:550 (txdw4)
TXDESC32_RETRY_LIMIT_ENABLE = 1 << 17    # rtl8xxxu.h:575 (txdw5)
TXDESC32_RETRY_LIMIT_SHIFT = 18          # rtl8xxxu.h:576 (txdw5)
TXDESC32_RETRY_LIMIT_MGNT = 6            # `rtl8xxxu_fill_txdesc_v3` (core.c:5360)

# ---- REG_TRXDMA_CTRL — TX-queue → DMA-channel routing (regs.h:405-421)
REG_TRXDMA_CTRL = 0x010C                 # regs.h:405
TRXDMA_CTRL_VOQ_SHIFT = 4
TRXDMA_CTRL_VIQ_SHIFT = 6
TRXDMA_CTRL_BEQ_SHIFT = 8
TRXDMA_CTRL_BKQ_SHIFT = 10
TRXDMA_CTRL_MGQ_SHIFT = 12
TRXDMA_CTRL_HIQ_SHIFT = 14
TRXDMA_QUEUE_LOW = 1
TRXDMA_QUEUE_NORMAL = 2
TRXDMA_QUEUE_HIGH = 3

# 802.11 frame control bits we build into the deauth frame.
FC0_TYPE_MGMT = 0x00
FC0_SUBTYPE_DEAUTH = 0xC0                # subtype 0xC, shifted into bits[7:4]
REASON_CODE_CLASS3_FRAME = 0x07          # "class-3 frame from non-associated STA"

# ---- M8 EFUSE registers + magic values (regs.h, rtl8xxxu.h) ---------
REG_9346CR = 0x000A                      # regs.h:60 (EEPROM/EFUSE config)
EEPROM_BOOT = 1 << 4                     # regs.h:61
EEPROM_ENABLE = 1 << 5                   # regs.h:62
REG_EFUSE_CTRL = 0x0030                  # regs.h:121
REG_EFUSE_TEST = 0x0034                  # regs.h:122
REG_EFUSE_ACCESS = 0x00CF                # regs.h:301
EFUSE_ACCESS_ENABLE = 0x69               # regs.h:133
EFUSE_ACCESS_DISABLE = 0x00              # regs.h:134

# REG_SYS_ISO_CTRL bit for EFUSE 1.2V power (regs.h:14)
SYS_ISO_PWC_EV12V = 1 << 15
# REG_SYS_FUNC bit for EFUSE reset (regs.h:29) — same reg as M1, bit 12
SYS_FUNC_ELDR = 1 << 12
# REG_SYS_CLKR bits for EFUSE clock (regs.h:49, 51)
SYS_CLK_ANA8M = 1 << 1
SYS_CLK_LOADER_ENABLE = 1 << 5

# EFUSE map / read tunables (rtl8xxxu.h:87-91)
EFUSE_MAP_LEN = 512
EFUSE_REAL_CONTENT_LEN_8723A = 512
EFUSE_MAX_WORD_UNIT = 4

# ---- M8c TX-power AGC registers (regs.h:940, 1134-1140) -------------
REG_TX_AGC_B_CCK11_A_CCK2_11 = 0x086C    # regs.h:940
REG_TX_AGC_A_RATE18_06 = 0x0E00          # regs.h:1134
REG_TX_AGC_A_RATE54_24 = 0x0E04          # regs.h:1135
REG_TX_AGC_A_CCK1_MCS32 = 0x0E08         # regs.h:1136
REG_TX_AGC_A_MCS03_MCS00 = 0x0E10        # regs.h:1137
REG_TX_AGC_A_MCS07_MCS04 = 0x0E14        # regs.h:1138
REG_TX_AGC_A_MCS11_MCS08 = 0x0E18        # regs.h:1139
REG_TX_AGC_A_MCS15_MCS12 = 0x0E1C        # regs.h:1140
