"""rt2800usb USB + register constants.

Ported from:

    driver_sources/rt2x00-source-v6.18/rt2x00.h        (chipset IDs)
    driver_sources/rt2x00-source-v6.18/rt2x00usb.h     (vendor request codes)
    driver_sources/rt2x00-source-v6.18/rt2800.h        (register addresses)

This module starts small (just what M1 needs to identify the chip) and
grows as later milestones land — same shape as rtl8187's constants.py.
"""
from __future__ import annotations

# ----------------------------------------------------------------------
# USB device identity (rt2800usb VID is always 0x148F for Ralink-branded
# variants; OEM rebrands have their own VIDs but we don't claim those
# yet — easy to extend SUPPORTED_IDS later).
# ----------------------------------------------------------------------
USB_VID_RALINK = 0x148F
USB_PID_RT3572 = 0x3572  # ALFA AWUS051NH v2          (silicon: RT3572)
USB_PID_RT5372 = 0x5372  # Panda PAU05/PAU06          (silicon: RT5392)
USB_PID_RT5572 = 0x5572  # Panda PAU09 N600           (silicon: RT5592)

# ----------------------------------------------------------------------
# Vendor control transfer.  [SRC] rt2x00usb.h:42-44, 49-58
# ----------------------------------------------------------------------
USB_VENDOR_REQUEST_IN = 0xC0   # USB_TYPE_VENDOR | USB_RECIP_DEVICE | DIR_IN
USB_VENDOR_REQUEST_OUT = 0x40  # USB_TYPE_VENDOR | USB_RECIP_DEVICE | DIR_OUT

# enum rt2x00usb_vendor_request
USB_DEVICE_MODE = 1     # set USB device mode (used for resets, FW boot signal)
USB_SINGLE_WRITE = 2    # 1-byte register write
USB_SINGLE_READ = 3
USB_MULTI_WRITE = 6     # 4-byte (or longer) register write at wValue=addr
USB_MULTI_READ = 7
USB_EEPROM_WRITE = 8
USB_EEPROM_READ = 9
USB_LED_CONTROL = 10
USB_RX_CONTROL = 12

# enum rt2x00usb_mode_offset — values for USB_DEVICE_MODE's wValue.
# [SRC] rt2x00usb.h:64-71
USB_MODE_RESET = 1
USB_MODE_UNPLUG = 2
USB_MODE_TEST = 4
USB_MODE_FIRMWARE = 8   # used by rt2800usb_write_firmware to kick FW exec
USB_MODE_AUTORUN = 17   # 0x11 — rt2800usb_autorun_detect probe (IN read of fw_mode)

# Vendor-request timeouts. [SRC] rt2x00usb.h:30-31
REGISTER_TIMEOUT_MS = 100
REGISTER_TIMEOUT_FIRMWARE_MS = 1000

# ----------------------------------------------------------------------
# rt2800 silicon IDs returned by MAC_CSR0[31:16].  [SRC] rt2x00.h:149-165
#
# IMPORTANT: these are SILICON IDs, NOT USB PIDs. The marketing name of
# the dongle (RT5572) is often different from the chip ID readback:
#   USB PID 0x5372 → silicon ID 0x5390 (RT5390 family covers 5370/5372)
#   USB PID 0x5572 → silicon ID 0x5592 (RT5572's silicon is RT5592)
#   USB PID 0x3572 → silicon ID 0x3572 (matches)
# ----------------------------------------------------------------------
RT_RT2860 = 0x2860
RT_RT2872 = 0x2872  # WSOC
RT_RT3070 = 0x3070
RT_RT3071 = 0x3071
RT_RT3090 = 0x3090
RT_RT3290 = 0x3290
RT_RT3352 = 0x3352  # WSOC
RT_RT3390 = 0x3390
RT_RT3572 = 0x3572
RT_RT3593 = 0x3593
RT_RT3883 = 0x3883
RT_RT5350 = 0x5350  # WSOC 2.4G
RT_RT5390 = 0x5390  # 2.4G — covers RT5370/RT5372
RT_RT5392 = 0x5392
RT_RT5592 = 0x5592  # dual-band — covers RT5572
RT_RT6352 = 0x6352  # WSOC 2.4G

# Human-readable chip names for log messages.  Falls back to the raw hex.
RT_NAMES = {
    RT_RT2860: "RT2860",
    RT_RT2872: "RT2872",
    RT_RT3070: "RT3070",
    RT_RT3071: "RT3071",
    RT_RT3090: "RT3090",
    RT_RT3290: "RT3290",
    RT_RT3352: "RT3352",
    RT_RT3390: "RT3390",
    RT_RT3572: "RT3572",
    RT_RT3593: "RT3593",
    RT_RT3883: "RT3883",
    RT_RT5350: "RT5350",
    RT_RT5390: "RT5390",  # = RT5370 / RT5372 chip-family
    RT_RT5392: "RT5392",
    RT_RT5592: "RT5592",  # = RT5572 chip-family
    RT_RT6352: "RT6352",
}

# Chips currently driven by wifit3. Anything outside this set is read but
# rejected with a clear "M-future" message at connect() time.
#
# RT5392 is included alongside RT5390 because the RT5372 chip family
# ships with both silicon revisions (the user's Panda PAU05 reports
# 0x5392 rev 0x0223, not 0x5390 as you might guess from the marketing
# name). Kernel `rt2800_probe_rt` treats them both as valid and the
# RF init dispatches to rt2800_init_bbp_5390 for RT5390 and
# rt2800_init_bbp_5392 for RT5392 — same family, different RF init.
RT_SUPPORTED = (RT_RT3572, RT_RT5390, RT_RT5392, RT_RT5592)

# ----------------------------------------------------------------------
# Register addresses (subset for M1).  [SRC] rt2800.h
# ----------------------------------------------------------------------
WLAN_FUN_CTRL = 0x0080
WLAN_FUN_CTRL_WLAN_EN = 1 << 0
WLAN_FUN_CTRL_WLAN_CLK_EN = 1 << 1
WLAN_FUN_CTRL_WLAN_RESET = 1 << 2
WLAN_FUN_CTRL_WLAN_RESET_RF = 1 << 3
WLAN_FUN_CTRL_GPIO0_OUT_OE_N = 0xFF << 24

PBF_SYS_CTRL = 0x0400
PBF_SYS_CTRL_READY = 1 << 7         # hw signals "PBF is ready" after FW boot
PBF_SYS_CTRL_HOST_RAM_WRITE = 1 << 16

USB_DMA_CFG = 0x02A0
RX_FILTER_CFG = 0x1400
# RX_FILTER_CFG DROP_* fields [SRC] rt2800.h:1757-1773 + mac80211 FIF_* flags.
RX_FILTER_CFG_DROP_CRC_ERROR = 0x00000001
RX_FILTER_CFG_DROP_PHY_ERROR = 0x00000002
RX_FILTER_CFG_DROP_NOT_TO_ME = 0x00000004
RX_FILTER_CFG_DROP_NOT_MY_BSSD = 0x00000008
RX_FILTER_CFG_DROP_VER_ERROR = 0x00000010
RX_FILTER_CFG_DROP_MULTICAST = 0x00000020
RX_FILTER_CFG_DROP_BROADCAST = 0x00000040
RX_FILTER_CFG_DROP_DUPLICATE = 0x00000080
RX_FILTER_CFG_DROP_CF_END_ACK = 0x00000100
RX_FILTER_CFG_DROP_CF_END = 0x00000200
RX_FILTER_CFG_DROP_ACK = 0x00000400
RX_FILTER_CFG_DROP_CTS = 0x00000800
RX_FILTER_CFG_DROP_RTS = 0x00001000
RX_FILTER_CFG_DROP_PSPOLL = 0x00002000
RX_FILTER_CFG_DROP_BA = 0x00004000
RX_FILTER_CFG_DROP_BAR = 0x00008000
RX_FILTER_CFG_DROP_CNTL = 0x00010000
FIF_ALLMULTI = 0x00000002    # mac80211 filter flags (subset the driver reads)
FIF_FCSFAIL = 0x00000004
FIF_PLCPFAIL = 0x00000008
FIF_CONTROL = 0x00000020
FIF_PSPOLL = 0x00000080
USB_DMA_CFG_RX_BULK_AGG_TIMEOUT_MASK = 0x000000FF
USB_DMA_CFG_RX_BULK_AGG_LIMIT_MASK = 0x0000FF00   # [SRC] rt2800.h:543
USB_DMA_CFG_PHY_CLEAR = 1 << 16
USB_DMA_CFG_RX_BULK_AGG_EN = 1 << 21
USB_DMA_CFG_RX_BULK_EN = 1 << 22
USB_DMA_CFG_TX_BULK_EN = 1 << 23
# RX bulk-agg limit = (rx_queue_limit * DATA_FRAME_SIZE / 1024) - 3, masked to the
# 8-bit field. rt2800usb RX queue depth = 128, DATA_FRAME_SIZE = 2432. [SRC]
# rt2800usb.c:310 (rt2800usb_enable_radio), :722 (queue_init), rt2x00queue.h:28.
RX_QUEUE_LIMIT = 128
DATA_FRAME_SIZE = 2432

MAC_CSR0 = 0x1000                   # [31:16] = CHIPSET, [15:0] = REVISION
MAC_CSR0_REVISION_MASK = 0x0000FFFF
MAC_CSR0_CHIPSET_MASK = 0xFFFF0000
MAC_CSR0_CHIPSET_SHIFT = 16

MAC_ADDR_DW0 = 0x1008               # bytes 0..3 of permanent MAC
MAC_ADDR_DW1 = 0x100C               # bytes 4..5 of permanent MAC

# Used by M2 (FW upload, MAC init) — declared here so transport can build
# helpers around them without a forward import.
H2M_MAILBOX_CSR = 0x7010
H2M_MAILBOX_CID = 0x7014
H2M_INT_SRC = 0x7024
H2M_MAILBOX_STATUS = 0x701C
H2M_BBP_AGENT = 0x7028
FIRMWARE_IMAGE_BASE = 0x3000        # FW chunk destination in chip RAM
AUTOWAKEUP_CFG = 0x1208             # [SRC] rt2800.h:1044 (0x1010 = MAC_BSSID_DW0, not this)
AUTOWAKEUP_CFG_AUTO_LEAD_TIME = 0x000000FF     # [SRC] rt2800.h:1045
AUTOWAKEUP_CFG_TBCN_BEFORE_WAKE = 0x00007F00   # [SRC] rt2800.h:1046
AUTOWAKEUP_CFG_AUTOWAKE = 0x00008000           # [SRC] rt2800.h:1047
WPDMA_GLO_CFG = 0x0208
HOST_CMD_CSR = 0x0404
MAC_SYS_CTRL = 0x1004

# WPDMA_GLO_CFG bit-fields  [SRC] rt2800.h:347-352
WPDMA_GLO_CFG_ENABLE_TX_DMA = 1 << 0
WPDMA_GLO_CFG_TX_DMA_BUSY = 1 << 1
WPDMA_GLO_CFG_ENABLE_RX_DMA = 1 << 2
WPDMA_GLO_CFG_RX_DMA_BUSY = 1 << 3
WPDMA_GLO_CFG_WP_DMA_BURST_SIZE = 0x3 << 4
WPDMA_GLO_CFG_TX_WRITEBACK_DONE = 1 << 6

# H2M_MAILBOX_CSR bit-fields  [SRC] rt2800.h:2113-2116
H2M_MAILBOX_CSR_ARG0 = 0x000000FF
H2M_MAILBOX_CSR_ARG1 = 0x0000FF00
H2M_MAILBOX_CSR_CMD_TOKEN = 0x00FF0000
H2M_MAILBOX_CSR_OWNER = 0xFF000000

# HOST_CMD_CSR bit-field
HOST_CMD_CSR_HOST_COMMAND = 0x000000FF

# MCU command tokens (rt2800.h:3024-3033)
MCU_WAKEUP = 0x31        # chip → STATE_AWAKE
MCU_LED = 0x50           # radio/assoc LED mode (rt2800_brightness_set, USB path)
MCU_LED_AG_CONF = 0x52       # per-band LED config from EEPROM (enable_radio tail)
MCU_LED_ACT_CONF = 0x53
MCU_LED_LED_POLARITY = 0x54
MCU_CURRENT = 0x36       # RT3070/RT3071/RT3572 USB-only — called by enable_radio
                         # between init_rfcsr and MAC_SYS_CTRL enable.
MCU_BOOT_SIGNAL = 0x72

# EEPROM_FREQ LED config fields — the kernel's led_mcu_reg (rt2800.h:2727-2729)
EEPROM_FREQ_LED_MODE = 0x7F00      # bits[14:8]
EEPROM_FREQ_LED_POLARITY = 0x1000  # bit 12

# MAC_SYS_CTRL bit-fields (used in M2b)
MAC_SYS_CTRL_RESET_CSR = 1 << 0
MAC_SYS_CTRL_RESET_BBP = 1 << 1
MAC_SYS_CTRL_ENABLE_TX = 1 << 2
MAC_SYS_CTRL_ENABLE_RX = 1 << 3

# Busy-loop budgets (kernel uses REGISTER_BUSY_COUNT = 100, sleep 1ms).
REGISTER_BUSY_COUNT = 100

# ----------------------------------------------------------------------
# Register addresses used by M2b-2 (rt2800_init_registers).  All from
# rt2800.h.
# ----------------------------------------------------------------------
US_CYC_CNT = 0x02A4
PBF_CFG = 0x0408
PBF_MAX_PCNT = 0x040C
AMPDU_BA_WINSIZE = 0x1040
CH_TIME_CFG = 0x110C
INT_TIMER_CFG = 0x1128
MAX_LEN_CFG = 0x1018
LED_CFG = 0x102C
XIFS_TIME_CFG = 0x1100
BKOFF_SLOT_CFG = 0x1104
BCN_TIME_CFG = 0x1114
PWR_PIN_CFG = 0x1204
TX_SW_CFG0 = 0x1330
TX_SW_CFG1 = 0x1334
TX_SW_CFG2 = 0x1338
TXOP_CTRL_CFG = 0x1340
TX_RTS_CFG = 0x1344
TX_TIMEOUT_CFG = 0x1348
TX_RTY_CFG = 0x134C
TX_RTY_CFG_SHORT_RTY_LIMIT = 0x000000FF   # [SRC] rt2800.h:1365 (mac80211 default 7)
TX_RTY_CFG_LONG_RTY_LIMIT = 0x0000FF00    # [SRC] rt2800.h:1366 (mac80211 default 4)
TX_LINK_CFG = 0x1350
CCK_PROT_CFG = 0x1364
OFDM_PROT_CFG = 0x1368
MM20_PROT_CFG = 0x136C
MM40_PROT_CFG = 0x1370
GF20_PROT_CFG = 0x1374
GF40_PROT_CFG = 0x1378
EXP_ACK_TIME = 0x1380
AUTO_RSP_CFG = 0x1404
LEGACY_BASIC_RATE = 0x1408
HT_BASIC_RATE = 0x140C
TXOP_HLDR_ET = 0x1608
RX_STA_CNT0 = 0x1700
RX_STA_CNT1 = 0x1704
RX_STA_CNT2 = 0x1708
TX_STA_CNT0 = 0x170C
TX_STA_CNT1 = 0x1710
TX_STA_CNT2 = 0x1714
TX_STA_FIFO = 0x1718    # per-frame TX status FIFO (read-to-pop, VALID bit = entry present)
MAC_STATUS_CFG = 0x1200 # bit 0 = BBP/RF busy on TX, bit 1 = busy on RX
# HT/LG rate-fallback config. [SRC] rt2800.h:1397-1436. NOTE: 0x1500-0x150C are
# TX_SEC_CNT0 / RX_SEC_CNT0 / CCMP_FC_MUTE (security regs), NOT the FBK block —
# the rate-fallback table belongs at 0x1354-0x1360.
HT_FBK_CFG0 = 0x1354
HT_FBK_CFG1 = 0x1358
LG_FBK_CFG0 = 0x135c
LG_FBK_CFG1 = 0x1360

# MAC table bases (used for the 256-entry WCID/IVEIV clears).
MAC_IVEIV_TABLE_BASE = 0x6000           # 256 × 8B  = 2048 bytes
SHARED_KEY_MODE_BASE = 0x7000           # 256 × 4B  = 1024 bytes (32 entries × 4B)

# Misc constants used by init_registers.
AGGREGATION_SIZE = 3840                 # rt2x00queue.h
USB_MAX_PSDU = 3                        # USB-specific max_psdu from drv_data

# ----------------------------------------------------------------------
# WPDMA_GLO_CFG extra bit-fields (M2b-2 uses these on top of the basic
# ones already defined above for M2a's disable_wpdma).
# ----------------------------------------------------------------------
WPDMA_GLO_CFG_BIG_ENDIAN = 1 << 7
WPDMA_GLO_CFG_RX_HDR_SCATTER = 0xFF << 8
WPDMA_GLO_CFG_HDR_SEG_LEN = 0xFFFF << 16
# (WPDMA_GLO_CFG_WP_DMA_BURST_SIZE already defined above as 0x3 << 4)

# ----------------------------------------------------------------------
# BBP register access — indirect through BBP_CSR_CFG.  [SRC] rt2800.h:808-814
# ----------------------------------------------------------------------
BBP_CSR_CFG = 0x101C
BBP_CSR_CFG_VALUE = 0x000000FF
BBP_CSR_CFG_REGNUM = 0x0000FF00
BBP_CSR_CFG_READ_CONTROL = 0x00010000
BBP_CSR_CFG_BUSY = 0x00020000
BBP_CSR_CFG_BBP_RW_MODE = 0x00080000

# BBP3_HT40_MINUS — bit 5. Cleared in HT20 monitor mode at end of
# config_channel. [SRC] rt2800.h:2234
BBP3_HT40_MINUS = 0x20

# BBP1/BBP3 antenna-chain selects — set by rt2800_config_ant. [SRC] rt2800.h:2226-2233
BBP1_TX_POWER_CTRL = 0x03    # bits[1:0] — the -6/-12 dBm TX-power backoff (config_txpower)
BBP1_TX_ANTENNA = 0x18       # bits[4:3] — TX chain select
BBP3_RX_ADC = 0x03           # bits[1:0]
BBP3_RX_ANTENNA = 0x18       # bits[4:3] — RX chain select

# BBP4 bit-fields. MAC_IF_CTRL set by rt2800_bbp4_mac_if_ctrl;
# BANDWIDTH used by RX filter calibration. [SRC] rt2800.h:2241-2242
BBP4_MAC_IF_CTRL = 0x40
BBP4_BANDWIDTH = 0x18           # bits[4:3]

# Channel-activity counters cleared at the end of every channel tune
# (kernel reads-without-using, which side-effect-clears them).
# [SRC] rt2800.h:1012-1022
CH_IDLE_STA = 0x1130
CH_BUSY_STA = 0x1134
CH_BUSY_STA_SEC = 0x1138

# BBP27_RX_CHAIN_SEL — bits[6:5]. Used by bbp_write_with_rx_chain to
# walk a write across each RX path's per-chain register. [SRC] rt2800.h:2246
BBP27_RX_CHAIN_SEL = 0x60

# BBP152 bit-field for RX antenna default (used by init_bbp_53xx,
# deferred since it needs EEPROM antenna-diversity reading).
BBP152_RX_DEFAULT_ANT = 0x80

# RT5592-only BBP fields. [SRC] rt2800.h:2272, 2301
BBP105_MLD = 0x04           # bit 2 — set when rx_chain_num == 2
BBP254_BIT7 = 0x80          # set by init_bbp_5592 only on REV_RT5592C+

# Chip revision constants used by RT5592 rev-gating.  [SRC] rt2800.h:90
REV_RT5592C = 0x0221

# RT5592 TX power bounds — applied to RFCSR49/50.TX_POWER ceiling.
# [SRC] rt2800lib.c:3299-3300
POWER_BOUND = 0x27          # 2.4 GHz
POWER_BOUND_5G = 0x2b       # 5 GHz

# ----------------------------------------------------------------------
# RF_CSR_CFG indirect access (M2c).  [SRC] rt2800.h:626-632
# Different bit layout from BBP_CSR_CFG — DATA in low byte, REGNUM in
# bits[13:8] (only 6 bits since RF has 64 registers), WRITE in bit 16,
# BUSY in bit 17.
# ----------------------------------------------------------------------
RF_CSR_CFG = 0x0500
RF_CSR_CFG_DATA = 0x000000FF
RF_CSR_CFG_REGNUM = 0x00003F00
RF_CSR_CFG_WRITE = 0x00010000
RF_CSR_CFG_BUSY = 0x00020000

# OPT_14_CSR (LED open-drain enable, called at end of RF init).
OPT_14_CSR = 0x0114
OPT_14_CSR_BIT0 = 0x00000001

# LDO_CFG0 — used by init_rfcsr_3572 (BGSEL + LDO_CORE_VLEVEL dance).
# [SRC] rt2800.h:684-691
LDO_CFG0 = 0x05D4
LDO_CFG0_BGSEL = 0x03000000             # bits[25:24]
LDO_CFG0_LDO_CORE_VLEVEL = 0x1C000000   # bits[28:26]

# GPIO_CTRL — used by config_channel_rf3052 (band-switch GPIO #7).
# [SRC] rt2800.h:450-462
GPIO_CTRL = 0x0228
GPIO_CTRL_DIR2 = 0x00000400   # rfkill-switch pin direction, set in rt2800_probe_hw
GPIO_CTRL_VAL7 = 0x00000080
GPIO_CTRL_DIR7 = 0x00008000

# MAC_DEBUG_INDEX — RT5592 xtal-clock detection. The XTAL bit indicates
# 40 MHz crystal (1) vs 20 MHz crystal (0); this selects which of the two
# RF channel tables (xtal20 vs xtal40) the driver consults. RF5592 is the
# only chip in the rt2800 family that surfaces this via a register —
# other chips do it through EEPROM_NIC_CONF2 instead.
# [SRC] rt2800.h:709-710, rt2800lib.c:11844-11852
MAC_DEBUG_INDEX = 0x05E8
MAC_DEBUG_INDEX_XTAL = 0x80000000

# EEPROM_NIC_CONF1.ANT_DIVERSITY — bits[12:11]. 3 = aux antenna,
# anything else = main antenna. Used by init_bbp_5592 to pick
# BBP152.RX_DEFAULT_ANT. [SRC] rt2800.h:2717
EEPROM_NIC_CONF1_ANT_DIVERSITY_MASK = 0x1800
EEPROM_NIC_CONF1_ANT_DIVERSITY_SHIFT = 11

# RFCSR bit-fields used by M2c.
RFCSR30_RF_CALIBRATION = 0x80
RFCSR30_RX_VCM = 0x18         # bits[4:3], value 2 = 0x10
RFCSR30_TX_H20M = 0x02
RFCSR30_RX_H20M = 0x04
RFCSR38_RX_LO1_EN = 0x20
RFCSR39_RX_LO2_EN = 0x80

# RFCSR1 (used by config_channel_rf53xx + config_channel_rf3052)
RFCSR1_RF_BLOCK_EN = 0x01
RFCSR1_PLL_PD = 0x02
RFCSR1_RX0_PD = 0x04
RFCSR1_TX0_PD = 0x08
RFCSR1_RX1_PD = 0x10
RFCSR1_TX1_PD = 0x20
RFCSR1_RX2_PD = 0x40           # [SRC] rt2800.h:2317 — needed for 2T2R RT3572
RFCSR1_TX2_PD = 0x80           # [SRC] rt2800.h:2318

# RFCSR3 VCO cal enable (kicked at end of config_channel for RF53xx).
RFCSR3_VCOCAL_EN = 0x80

# RFCSR5_R1 — bits[3:2] of RFCSR5. Used by config_channel_rf3052
# (1 for 2.4 GHz, 2 for 5 GHz). [SRC] rt2800.h:2354
RFCSR5_R1 = 0x0C

# RFCSR6 fields used by RF3052 RT3572 init + channel tune.
# [SRC] rt2800.h:2359-2361
RFCSR6_R1 = 0x03                # bits[1:0] — synthesizer R1
RFCSR6_TXDIV = 0x0C             # bits[3:2] — TX divider (2 for 2.4G, 1 for 5G)
RFCSR6_R2 = 0x40                # bit 6 — set by init_rfcsr_3572

# RFCSR7 — bit 0 RF_TUNING is the channel-tune kick (both bands).
# BIT2/BIT3/BIT4/BITS67 are used by RF3052's 5 GHz path to override
# RFCSR7 (RMW); 2.4 GHz writes RFCSR7=0xD8 outright.
# [SRC] rt2800.h:2368-2374
RFCSR7_RF_TUNING = 0x01
RFCSR7_BIT2 = 0x04
RFCSR7_BIT3 = 0x08
RFCSR7_BIT4 = 0x10
RFCSR7_BITS67 = 0xC0

# RFCSR9 field packing for RF5592 synthesizer (5-field rf_channel
# {N,K,mod,R} → split into RFCSR8/9/11). [SRC] rt2800.h:2379-2382
RFCSR9_K = 0x0F                 # bits[3:0] — K
RFCSR9_N = 0x10                 # bit 4 — high bit of N (low 8 bits go in RFCSR8)
RFCSR9_MOD = 0x80               # bit 7 — high bit of (mod - 8)

# RFCSR11 — R field (bits[1:0]) for the synthesizer divider.
RFCSR11_R = 0x03
# RFCSR11_MOD — bits[7:6], low two bits of (mod - 8) on RF5592.
# [SRC] rt2800.h:2389
RFCSR11_MOD = 0xC0

# RFCSR12/13 — TX_POWER + DR0 fields (used by config_channel_rf3052).
# [SRC] rt2800.h:2398-2405
RFCSR12_TX_POWER = 0x1F         # bits[4:0]
RFCSR12_DR0 = 0xE0              # bits[7:5]
RFCSR13_TX_POWER = 0x1F
RFCSR13_DR0 = 0xE0

# RFCSR16_TXMIXER_GAIN — bits[2:0]. Used by config_channel_rf3052
# (EEPROM-derived; default 0 means leave at chip default).
# [SRC] rt2800.h:2416
RFCSR16_TXMIXER_GAIN = 0x07

# RFCSR17 — used by normal_mode_setup_3xxx.  [SRC] rt2800.h:2423-2425
RFCSR17_TXMIXER_GAIN = 0x07     # bits[2:0]
RFCSR17_TX_LO1_EN = 0x08        # bit 3 — cleared by normal_mode_setup_3xxx
RFCSR17_R = 0x20                # bit 5

# RFCSR22 — baseband-loopback toggle used by RX filter calibration.
# [SRC] rt2800.h:2449
RFCSR22_BASEBAND_LOOPBACK = 0x01

# RFCSR23_FREQ_OFFSET — bits[6:0]. RF3052 writes freq_offset directly to
# this register (no MCU command needed, unlike RF53xx).
# [SRC] rt2800.h:2455
RFCSR23_FREQ_OFFSET = 0x7F

# RFCSR31_RX_H20M — bit 5. Used by RX filter calibration's BW40 path.
# [SRC] rt2800.h:2501
RFCSR31_RX_H20M = 0x20

# RFCSR49/50 TX power (low 6 bits) — referenced by config_channel_rf53xx
# but we leave these alone in our M4 minimal port (no EEPROM TX power yet).
RFCSR49_TX = 0x3F
RFCSR50_TX = 0x3F

# RFCSR49/50_EP — bits[7:6]. Set by config_channel_rf55xx only when
# is_type_ep is true (we keep is_type_ep=False, matching the kernel's
# constant default since type_ep isn't surfaced through EEPROM yet).
# [SRC] rt2800.h:2544, 2555
RFCSR49_EP = 0xC0
RFCSR50_EP = 0xC0

# RFCSR17 freq-offset code field — used by freq_cal_mode1 to clamp
# the freq trim. [SRC] rt2800.h:2426
RFCSR17_CODE = 0x7F
FREQ_OFFSET_BOUND = 0x5F        # rt2800lib.c:2445

# ----------------------------------------------------------------------
# RX descriptor sizes (M3).  [SRC] rt2800usb.h:61 + rt2800.h
#
# RX URB layout (rt2800usb_fill_rxdone at rt2800usb.c:481-518):
#     [RXINFO (4B)] [RXWI (16/24B)] [hdr] [L2 pad] [payload] [pad] [RXD (4B)] [USB pad]
#                   |<------------ rx_pkt_len ---------------->|
#
# RXWI size:
#   * 4 words (16 B) for RT3572/RT5390/RT5392 — most rt2800usb chips
#   * 6 words (24 B) for RT5592 — RT5572 hw
#
# RXD trails after the payload (its own 4 B word with CRC_ERROR etc).
# ----------------------------------------------------------------------
RXINFO_DESC_SIZE = 4
RXWI_DESC_SIZE_4WORDS = 16
RXWI_DESC_SIZE_6WORDS = 24
RXD_DESC_SIZE = 4

# RXINFO_W0 bit-fields
RXINFO_W0_USB_DMA_RX_PKT_LEN = 0x0000FFFF

# RXWI_W0 — MPDU byte count
RXWI_W0_MPDU_TOTAL_BYTE_COUNT = 0x0FFF0000

# RXWI_W1 — rate / PHY mode
RXWI_W1_MCS = 0x007F0000
RXWI_W1_BW = 0x00800000
RXWI_W1_SHORT_GI = 0x01000000
RXWI_W1_PHYMODE = 0xC0000000

# RXWI_W2 — per-path RSSI (bytes 0/1/2 are signed)
RXWI_W2_RSSI0 = 0x000000FF
RXWI_W2_RSSI1 = 0x0000FF00
RXWI_W2_RSSI2 = 0x00FF0000

# RXD_W0 trailing flags
RXD_W0_CRC_ERROR = 0x00000100
RXD_W0_L2PAD = 0x00004000      # [SRC] rt2800usb.h:91 — 2B hdr/body align pad

# ----------------------------------------------------------------------
# USB endpoint addresses (re-stated here for the rx/tx modules).
# Per the kernel rt2800usb_probe + endpoint enumeration:
#   bulk-IN  data : 0x84
#   bulk-OUT data : 0x01..0x06 (EDCA QSEL prio queues; 0x05 = EDCA Q0)
# ----------------------------------------------------------------------
# Already defined above as USB_EP_BULK_IN / USB_EP_BULK_OUT.

# ----------------------------------------------------------------------
# Default RX/TX bulk endpoint addresses.  Probed at runtime by
# rx.probe_endpoints (M3) — these are fallback values for tests.
#
# Kernel TX EP-to-queue mapping (rt2800usb.c):
#   0x01 = AC_BK,  0x02 = AC_BE,  0x03 = AC_VI,  0x04 = AC_VO,
#   0x05 = HCCA,   0x06 = MGMT
#
# For broadcast mgmt-style inject (deauth) we use the MGMT endpoint
# (0x06).  Real driver picks per-queue based on AC; our injects all go
# out the MGMT queue since they're spoofed/no-ACK.
# ----------------------------------------------------------------------
USB_EP_BULK_IN = 0x84      # RXdata
USB_EP_BULK_OUT_MGMT = 0x06
USB_EP_BULK_OUT_AC_BE = 0x02
# Kernel rt2x00usb_assign_endpoint maps queues to bulk-OUT EPs in
# descriptor order: 1st bulk-OUT (= 0x01) is AC_VO. Mgmt frames in
# mac80211 default to the highest-priority queue (AC_VO), so kernel
# inject (aireplay-ng deauths) all go through EP 0x01. Verified from
# driver_captures/captures_rt2800usb_rt3572/capture-1.pcap frame 43087.
# [SRC] rt2x00usb.c:579-595
USB_EP_BULK_OUT_AC_VO = 0x01

# ----------------------------------------------------------------------
# TX_BAND_CFG + TX_PIN_CFG (used by config_channel_rf3052 + rf53xx).
# Shared between RT5392 and RT3572 — the dispatcher in chan.py writes
# the same register, but RT3572 sets bit A for 5 GHz routing on top of
# the existing BG bit. [SRC] rt2800.h:1241-1280
# ----------------------------------------------------------------------
TX_BAND_CFG_REG = 0x132C
TX_BAND_CFG_HT40_MINUS = 0x00000001
TX_BAND_CFG_A = 0x00000002        # 1 = 5 GHz routing
TX_BAND_CFG_BG_BIT = 0x00000004   # 1 = 2.4 GHz routing (already used by chan.py)

# Per-rate TX power config — chip firmware reads these for each frame's
# modulation rate to determine final TX power. Each register holds 8 ×
# 4-bit power values across rates. Kernel populates these from EEPROM
# TXPOWER_BYRATE in rt2800_config_txpower_rt28xx (rt2800lib.c:5407+).
# With unburned EEPROM these stay at chip reset value (~0) and gate
# emit to near-zero RF regardless of RFCSR12/13.TX_POWER.
# [SRC] rt2800.h:1115-1235
TX_PWR_CFG_0 = 0x1314    # CCK 1/2/5.5/11 Mbps + OFDM 6/9/12/18 Mbps
TX_PWR_CFG_1 = 0x1318    # OFDM 24/36/48/54 + MCS 0..3
TX_PWR_CFG_2 = 0x131C    # MCS 4..11
TX_PWR_CFG_3 = 0x1320    # MCS 12..15 + STBC
TX_PWR_CFG_4 = 0x1324    # STBC extras

TX_PIN_CFG_REG = 0x1328
TX_PIN_CFG_PA_PE_A0_EN_BIT = 0x00000001    # 5 GHz primary PA (RF3052+)
TX_PIN_CFG_PA_PE_G0_EN_BIT = 0x00000002
TX_PIN_CFG_PA_PE_A1_EN = 0x00000004        # 2T2R 5 GHz secondary PA
TX_PIN_CFG_PA_PE_G1_EN = 0x00000008        # 2T2R 2.4 GHz secondary PA
TX_PIN_CFG_LNA_PE_A0_EN_BIT = 0x00000100
TX_PIN_CFG_LNA_PE_G0_EN_BIT = 0x00000200
TX_PIN_CFG_LNA_PE_A1_EN = 0x00000400       # 2T2R 5 GHz secondary LNA
TX_PIN_CFG_LNA_PE_G1_EN = 0x00000800       # 2T2R 2.4 GHz secondary LNA
TX_PIN_CFG_RFTR_EN_BIT = 0x00010000
TX_PIN_CFG_TRSW_EN_BIT = 0x00040000

# Default for inject_frame.
USB_EP_BULK_OUT = USB_EP_BULK_OUT_MGMT

# ----------------------------------------------------------------------
# TX descriptor sizes + bit-fields (M5).  [SRC] rt2800.h + rt2800usb.h
# ----------------------------------------------------------------------
TXINFO_DESC_SIZE = 4
TXWI_DESC_SIZE_4WORDS = 16
TXWI_DESC_SIZE_5WORDS = 20    # RT5592

# TXINFO_W0 fields
TXINFO_W0_USB_DMA_TX_PKT_LEN = 0x0000FFFF
TXINFO_W0_WIV = 0x01000000
TXINFO_W0_QSEL = 0x06000000           # 2 bits — 2 = EDCA, 0 = MGMT
TXINFO_W0_SW_USE_LAST_ROUND = 0x08000000
TXINFO_W0_USB_DMA_NEXT_VALID = 0x40000000
TXINFO_W0_USB_DMA_TX_BURST = 0x80000000

TXINFO_QSEL_EDCA = 2
TXINFO_QSEL_MGMT = 0

# TXWI_W0 fields
TXWI_W0_FRAG = 0x00000001
TXWI_W0_TX_OP = 0x00000300            # 0=HT_TXOP_RTS, 1=PIFS, 2=SIFS, 3=NONE
TXWI_W0_MCS = 0x007F0000
TXWI_W0_BW = 0x00800000
TXWI_W0_PHYMODE = 0xC0000000          # 0 = CCK, 1 = OFDM, 2 = HT/MM, 3 = HT/GF

# Kernel uses HT_TXOP_NONE (3) for mgmt frames — chip skips RTS/CTS
# handshake. With our default 0 (HT_TXOP_RTS), chip tries to acquire
# TXOP via RTS first; for spoofed-srcMAC mgmt frames the RTS handshake
# fails silently and the actual data frame never goes on air, even
# though TX_STA_FIFO reports TX_SUCCESS=1 for the queue dequeue.
# Diagnosed 2026-05-22 by comparing kernel pcap aireplay-ng deauth TXWI
# against ours. [SRC] rt2x00reg.h:78 (HT_TXOP_NONE=3)
TXWI_TX_OP_NONE = 3

TXWI_PHYMODE_CCK = 0
TXWI_PHYMODE_OFDM = 1

# TXWI_W1 fields
TXWI_W1_ACK = 0x00000001
TXWI_W1_NSEQ = 0x00000002
TXWI_W1_WIRELESS_CLI_ID = 0x0000FF00
TXWI_W1_MPDU_TOTAL_BYTE_COUNT = 0x0FFF0000
TXWI_W1_PACKETID_QUEUE = 0x30000000
TXWI_W1_PACKETID_ENTRY = 0xC0000000
