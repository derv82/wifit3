"""rt2800usb USB + register constants.

Ported from:

    data_dumps/rt2x00-source-v6.18/rt2x00.h        (chipset IDs)
    data_dumps/rt2x00-source-v6.18/rt2x00usb.h     (vendor request codes)
    data_dumps/rt2x00-source-v6.18/rt2800.h        (register addresses)

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
USB_PID_RT5372 = 0x5372  # Panda PAU05                (silicon: RT5390)
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
AUTOWAKEUP_CFG = 0x1010
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

# MCU command tokens (rt2800.h:3033+)
MCU_BOOT_SIGNAL = 0x72

# MAC_SYS_CTRL bit-fields (used in M2b)
MAC_SYS_CTRL_RESET_CSR = 1 << 0
MAC_SYS_CTRL_RESET_BBP = 1 << 1

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
HT_FBK_CFG0 = 0x1500
HT_FBK_CFG1 = 0x1504
LG_FBK_CFG0 = 0x1508
LG_FBK_CFG1 = 0x150C

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

# BBP4 bit-field used by rt2800_bbp4_mac_if_ctrl
BBP4_MAC_IF_CTRL = 0x40

# BBP152 bit-field for RX antenna default (used by init_bbp_53xx,
# deferred since it needs EEPROM antenna-diversity reading).
BBP152_RX_DEFAULT_ANT = 0x80

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

# RFCSR bit-fields used by M2c.
RFCSR30_RF_CALIBRATION = 0x80
RFCSR30_RX_VCM = 0x18         # bits[4:3], value 2 = 0x10
RFCSR30_TX_H20M = 0x02
RFCSR30_RX_H20M = 0x04
RFCSR38_RX_LO1_EN = 0x20
RFCSR39_RX_LO2_EN = 0x80

# RFCSR1 (used by config_channel_rf53xx)
RFCSR1_RF_BLOCK_EN = 0x01
RFCSR1_PLL_PD = 0x02
RFCSR1_RX0_PD = 0x04
RFCSR1_TX0_PD = 0x08
RFCSR1_RX1_PD = 0x10
RFCSR1_TX1_PD = 0x20

# RFCSR3 VCO cal enable (kicked at end of config_channel for RF53xx).
RFCSR3_VCOCAL_EN = 0x80

# RFCSR11 — R field (bits[1:0]) for the synthesizer divider.
RFCSR11_R = 0x03

# RFCSR49/50 TX power (low 6 bits) — referenced by config_channel_rf53xx
# but we leave these alone in our M4 minimal port (no EEPROM TX power yet).
RFCSR49_TX = 0x3F
RFCSR50_TX = 0x3F

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
TXWI_W0_MCS = 0x007F0000
TXWI_W0_BW = 0x00800000
TXWI_W0_PHYMODE = 0xC0000000          # 0 = CCK, 1 = OFDM, 2 = HT/MM, 3 = HT/GF

TXWI_PHYMODE_CCK = 0
TXWI_PHYMODE_OFDM = 1

# TXWI_W1 fields
TXWI_W1_ACK = 0x00000001
TXWI_W1_NSEQ = 0x00000002
TXWI_W1_WIRELESS_CLI_ID = 0x0000FF00
TXWI_W1_MPDU_TOTAL_BYTE_COUNT = 0x0FFF0000
TXWI_W1_PACKETID_QUEUE = 0x30000000
TXWI_W1_PACKETID_ENTRY = 0xC0000000
