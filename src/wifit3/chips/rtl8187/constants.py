"""RTL8187L USB register map + flag bits.

Ported from the kernel headers:

    data_dumps/rtl818x-source-v6.18/rtl818x.h       (struct rtl818x_csr + flags)
    data_dumps/rtl818x-source-v6.18/rtl8187/rtl8187.h (USB request constants)

The kernel maps the CSR struct at virtual base ``0xFF00`` (see
``rtl8187_probe``: ``priv->map = (struct rtl818x_csr *)0xFF00``), so the
on-the-wire ``wValue`` of every vendor control transfer is
``0xFF00 + struct_offset``. We pre-fold the 0xFF00 base into every
constant here so callers never have to do the addition themselves.

Page-select magic registers (those above ``0xFFE0``) need ``wIndex`` set
non-zero — see :mod:`.transport` and the kernel ``rtl818x_iowrite8_idx``
helpers for the exact semantics.
"""
from __future__ import annotations

# ----------------------------------------------------------------------
# USB device identity
# ----------------------------------------------------------------------
USB_VID_REALTEK = 0x0BDA
USB_PID_RTL8187 = 0x8187  # AWUS036H + many other 8187L dongles

# Endpoints on the 8187L (verified against AWUS036H).
USB_EP_BULK_IN = 0x81
USB_EP_BULK_OUT = 0x02

# ----------------------------------------------------------------------
# Vendor control transfer
# ----------------------------------------------------------------------
# [SRC] rtl8187.h:27-30
RTL8187_REQT_READ = 0xC0   # bmRequestType for vendor IN
RTL8187_REQT_WRITE = 0x40  # bmRequestType for vendor OUT
RTL8187_REQ_GET_REG = 0x05
RTL8187_REQ_SET_REG = 0x05

RTL8187_MAX_RX = 0x9C4  # 2500 bytes — max bulk-IN URB size used by kernel

# ----------------------------------------------------------------------
# CSR base address — kernel does ``priv->map = (void *)0xFF00`` and then
# addresses every register as ``&priv->map->FIELD``. So the wValue we
# put on the wire is always ``0xFF00 + struct_offset``.
# ----------------------------------------------------------------------
CSR_BASE = 0xFF00

# ----------------------------------------------------------------------
# Register addresses (full wValue including the CSR_BASE offset).
# Field-name + offset comments come from struct rtl818x_csr in rtl818x.h.
# ----------------------------------------------------------------------
REG_MAC0 = 0xFF00 + 0x00          # u8 MAC[6]
REG_MAC4 = 0xFF00 + 0x04
REG_MAR = 0xFF00 + 0x08           # __le32 MAR[2] (multicast filter)
REG_RX_FIFO_COUNT = 0xFF00 + 0x10
REG_TX_FIFO_COUNT = 0xFF00 + 0x12
REG_BQREQ = 0xFF00 + 0x13
REG_TSFT = 0xFF00 + 0x18          # __le32 TSFT[2]
REG_TLPDA = 0xFF00 + 0x20
REG_TNPDA = 0xFF00 + 0x24
REG_THPDA = 0xFF00 + 0x28
REG_BRSR = 0xFF00 + 0x2C          # Basic Rate Set Register
REG_BSSID = 0xFF00 + 0x2E
REG_RESP_RATE = 0xFF00 + 0x34
REG_EIFS = 0xFF00 + 0x35
REG_CMD = 0xFF00 + 0x37
REG_INT_MASK = 0xFF00 + 0x3C
REG_INT_STATUS = 0xFF00 + 0x3E
REG_TX_CONF = 0xFF00 + 0x40       # 32-bit; HWVER bits in [27:25]
REG_RX_CONF = 0xFF00 + 0x44       # 32-bit
REG_INT_TIMEOUT = 0xFF00 + 0x48
REG_TBDA = 0xFF00 + 0x4C
REG_EEPROM_CMD = 0xFF00 + 0x50    # u8; bit-banged 93cx6 SPI lives here
REG_CONFIG0 = 0xFF00 + 0x51
REG_CONFIG1 = 0xFF00 + 0x52
REG_CONFIG2 = 0xFF00 + 0x53
REG_ANAPARAM = 0xFF00 + 0x54
REG_MSR = 0xFF00 + 0x58
REG_CONFIG3 = 0xFF00 + 0x59
REG_CONFIG4 = 0xFF00 + 0x5A
REG_TESTR = 0xFF00 + 0x5B
REG_PGSELECT = 0xFF00 + 0x5E
REG_SECURITY = 0xFF00 + 0x5F
REG_ANAPARAM2 = 0xFF00 + 0x60
REG_IMR = 0xFF00 + 0x6C           # __le32 — 8187se interrupt mask
REG_BEACON_INTERVAL = 0xFF00 + 0x70
REG_ATIM_WND = 0xFF00 + 0x72
REG_BEACON_INTERVAL_TIME = 0xFF00 + 0x74
REG_ATIMTR_INTERVAL = 0xFF00 + 0x76
REG_PHY_DELAY = 0xFF00 + 0x78
REG_CARRIER_SENSE_COUNTER = 0xFF00 + 0x79
REG_PHY = 0xFF00 + 0x7C           # u8[4]
REG_RFPINSOUTPUT = 0xFF00 + 0x80  # __le16; drives RF SPI bit-bang
REG_RFPINSENABLE = 0xFF00 + 0x82
REG_RFPINSSELECT = 0xFF00 + 0x84
REG_RFPINSINPUT = 0xFF00 + 0x86
REG_RF_PARA = 0xFF00 + 0x88
REG_RF_TIMING = 0xFF00 + 0x8C
REG_GP_ENABLE = 0xFF00 + 0x90
REG_GPIO0 = 0xFF00 + 0x91
REG_GPIO1 = 0xFF00 + 0x92
REG_HSSI_PARA = 0xFF00 + 0x94
REG_TX_AGC_CTL = 0xFF00 + 0x9C
REG_TX_GAIN_CCK = 0xFF00 + 0x9D
REG_TX_GAIN_OFDM = 0xFF00 + 0x9E
REG_TX_ANTENNA = 0xFF00 + 0x9F
REG_WPA_CONF = 0xFF00 + 0xB0
REG_SIFS = 0xFF00 + 0xB4
REG_DIFS = 0xFF00 + 0xB5
REG_SLOT = 0xFF00 + 0xB6
REG_CW_CONF = 0xFF00 + 0xBC
REG_CW_VAL = 0xFF00 + 0xBD
REG_RATE_FALLBACK = 0xFF00 + 0xBE
REG_ACM_CONTROL = 0xFF00 + 0xBF
REG_CONFIG5 = 0xFF00 + 0xD8
REG_TX_DMA_POLLING = 0xFF00 + 0xD9
REG_PHY_PR = 0xFF00 + 0xDA
REG_CWR = 0xFF00 + 0xDC
REG_RETRY_CTR = 0xFF00 + 0xDE
REG_INT_MIG = 0xFF00 + 0xE2
REG_RDSAR = 0xFF00 + 0xE4
REG_TID_AC_MAP = 0xFF00 + 0xE8
REG_ANAPARAM3A = 0xFF00 + 0xEE
REG_AC_VO_PARAM = 0xFF00 + 0xF0
REG_AC_VI_PARAM = 0xFF00 + 0xF4
REG_FEMR = 0xFF00 + 0xF4           # union with AC_VI_PARAM
REG_TALLY_CNT = 0xFF00 + 0xFA
REG_TALLY_SEL = 0xFF00 + 0xFC

# Page-select magic registers (above 0xFFE0) — kernel uses wIndex=1/2/3
# to address these. They map to internal asic_rev / wlan-MAC config that
# isn't exposed via the rtl818x_csr struct. Used during init_hw and
# 8187B-specific paths only.
REG_MAGIC_ASIC_REV = 0xFFFE       # page 1: low 2 bits = asic_rev
REG_MAGIC_FFE1 = 0xFFE1           # 8187B hw_rev probe
REG_MAGIC_FE18 = 0xFE18           # init_hw soft-reset toggle (writes 0x10/0x11/0x00)
REG_MAGIC_FE53 = 0xFE53           # init_hw host_usb_init OR-with-0x80
REG_MAGIC_FFF4 = 0xFFF4           # init_hw EEPROM_CONFIG sentinel write (0xFFFF)
REG_MAGIC_FFFF = 0xFFFF           # init_hw page-1 0x60 write

# ----------------------------------------------------------------------
# CMD register bits  [SRC] rtl818x.h:79-81
# ----------------------------------------------------------------------
CMD_TX_ENABLE = 1 << 2
CMD_RX_ENABLE = 1 << 3
CMD_RESET = 1 << 4

# ----------------------------------------------------------------------
# MSR (Media Status) values  [SRC] rtl818x.h:186-190
# ----------------------------------------------------------------------
MSR_NO_LINK = 0 << 2
MSR_ADHOC = 1 << 2
MSR_INFRA = 2 << 2
MSR_MASTER = 3 << 2
MSR_ENEDCA = 4 << 2

# ----------------------------------------------------------------------
# EEPROM 93cx6 bit-banging  [SRC] rtl818x.h:172-179
# ----------------------------------------------------------------------
EEPROM_CMD_READ = 1 << 0
EEPROM_CMD_WRITE = 1 << 1
EEPROM_CMD_CK = 1 << 2
EEPROM_CMD_CS = 1 << 3
EEPROM_CMD_NORMAL = 0 << 6
EEPROM_CMD_LOAD = 1 << 6
EEPROM_CMD_PROGRAM = 2 << 6
EEPROM_CMD_CONFIG = 3 << 6

# ----------------------------------------------------------------------
# CONFIG2 bits  [SRC] rtl818x.h:183
# ----------------------------------------------------------------------
CONFIG2_ANTENNA_DIV = 1 << 6

# ----------------------------------------------------------------------
# CONFIG3 bits  [SRC] rtl818x.h:192-193
# ----------------------------------------------------------------------
CONFIG3_ANAPARAM_WRITE = 1 << 6
CONFIG3_GNT_SELECT = 1 << 7

# ----------------------------------------------------------------------
# CONFIG4 bits  [SRC] rtl818x.h:195-196
# ----------------------------------------------------------------------
CONFIG4_POWEROFF = 1 << 6
CONFIG4_VCOOFF = 1 << 7

# ----------------------------------------------------------------------
# RX_CONF bits  [SRC] rtl818x.h:154-168
# ----------------------------------------------------------------------
RX_CONF_MONITOR = 1 << 0
RX_CONF_NICMAC = 1 << 1
RX_CONF_MULTICAST = 1 << 2
RX_CONF_BROADCAST = 1 << 3
RX_CONF_FCS = 1 << 5
RX_CONF_DATA = 1 << 18
RX_CONF_CTRL = 1 << 19
RX_CONF_MGMT = 1 << 20
RX_CONF_ADDR3 = 1 << 21
RX_CONF_PM = 1 << 22
RX_CONF_BSSID = 1 << 23
RX_CONF_RX_AUTORESETPHY = 1 << 28
RX_CONF_CSDM1 = 1 << 29
RX_CONF_CSDM2 = 1 << 30
RX_CONF_ONLYERLPKT = 1 << 31

# ----------------------------------------------------------------------
# TX_CONF bits  [SRC] rtl818x.h:136-152
# ----------------------------------------------------------------------
TX_CONF_LOOPBACK_MAC = 1 << 17
TX_CONF_LOOPBACK_CONT = 3 << 17
TX_CONF_NO_ICV = 1 << 19
TX_CONF_DISCW = 1 << 20
TX_CONF_SAT_HWPLCP = 1 << 24
# Hardware version (HWVER) lives in TX_CONF[27:25] and is the canonical
# way to discriminate RTL8187vB / RTL8187vD / early-RTL8187B-masquerading-
# as-8187L. The driver checks it in rtl8187_probe after EEPROM load.
TX_CONF_HWVER_MASK = 7 << 25
TX_CONF_R8180_ABCD = 2 << 25
TX_CONF_R8180_F = 3 << 25
TX_CONF_R8185_ABC = 4 << 25
TX_CONF_R8185_D = 5 << 25
TX_CONF_R8187vD = 5 << 25
TX_CONF_R8187vD_B = 6 << 25       # "Some RTL8187B devices have a USB ID of 0x8187"
TX_CONF_RTL8187SE = 6 << 25
TX_CONF_DISREQQSIZE = 1 << 28
TX_CONF_PROBE_DTS = 1 << 29
TX_CONF_HW_SEQNUM = 1 << 30
TX_CONF_CW_MIN = 1 << 31

# Human-readable HWVER chip-name map. Mirrors the switch/case in
# rtl8187_probe lines 1541-1556.  Anything outside this map (and outside
# the 8187B path) falls through to "RTL8187vB (default)".
HWVER_CHIP_NAMES = {
    TX_CONF_R8187vD: "RTL8187vD",
    TX_CONF_R8187vD_B: "RTL8187BvB(early)",
}
HWVER_DEFAULT_NAME = "RTL8187vB"

# ----------------------------------------------------------------------
# EEPROM byte offsets within the 93cx6  [SRC] rtl8187.h:20-25
# ----------------------------------------------------------------------
EEPROM_TXPWR_BASE = 0x05
EEPROM_MAC_ADDR = 0x07
EEPROM_TXPWR_CHAN_1 = 0x16        # 3 channels
EEPROM_TXPWR_CHAN_6 = 0x1B        # 2 channels
EEPROM_TXPWR_CHAN_4 = 0x3D        # 2 channels
EEPROM_SELECT_GPIO = 0x3B

# ----------------------------------------------------------------------
# RX descriptor flags  [SRC] rtl818x.h:383-400
# ----------------------------------------------------------------------
RX_DESC_FLAG_ICV_ERR = 1 << 12
RX_DESC_FLAG_CRC32_ERR = 1 << 13
RX_DESC_FLAG_PM = 1 << 14
RX_DESC_FLAG_RX_ERR = 1 << 15
RX_DESC_FLAG_BCAST = 1 << 16
RX_DESC_FLAG_PAM = 1 << 17
RX_DESC_FLAG_MCAST = 1 << 18
RX_DESC_FLAG_QOS = 1 << 19
RX_DESC_FLAG_TRSW = 1 << 24
RX_DESC_FLAG_SPLCP = 1 << 25
RX_DESC_FLAG_FOF = 1 << 26
RX_DESC_FLAG_DMA_FAIL = 1 << 27
RX_DESC_FLAG_LS = 1 << 28
RX_DESC_FLAG_FS = 1 << 29
RX_DESC_FLAG_EOR = 1 << 30
RX_DESC_FLAG_OWN = 1 << 31

# ----------------------------------------------------------------------
# TX descriptor flags  [SRC] rtl818x.h:369-381
# ----------------------------------------------------------------------
TX_DESC_FLAG_NO_ENC = 1 << 15
TX_DESC_FLAG_TX_OK = 1 << 15
TX_DESC_FLAG_SPLCP = 1 << 16
TX_DESC_FLAG_MOREFRAG = 1 << 17
TX_DESC_FLAG_CTS = 1 << 18
TX_DESC_FLAG_RTS = 1 << 23
TX_DESC_FLAG_LS = 1 << 28
TX_DESC_FLAG_FS = 1 << 29
TX_DESC_FLAG_DMA = 1 << 30
TX_DESC_FLAG_OWN = 1 << 31

# ----------------------------------------------------------------------
# rtl8187_rx_hdr (16 bytes, trailing — appended after the 802.11 frame
# in every bulk-IN URB on 8187L).  [SRC] rtl8187.h:44-51
#
#   __le32 flags;
#   u8 noise;
#   u8 signal;
#   u8 agc;
#   u8 reserved;
#   __le64 mac_time;
# ----------------------------------------------------------------------
RX_HDR_SIZE_8187L = 16

# ----------------------------------------------------------------------
# ANAPARAM magic values  [SRC] rtl8225.h:15-18
# Used by rtl8187_set_anaparam(rfon=True/False) to drive the analogue
# baseband state machine. Values are little-endian u32s written to
# ANAPARAM (0xFF54) and ANAPARAM2 (0xFF60).
# ----------------------------------------------------------------------
ANAPARAM_ON = 0xA0000A59
ANAPARAM2_ON = 0x860C7312
ANAPARAM_OFF = 0xA00BEB59
ANAPARAM2_OFF = 0x840DEC11

# ----------------------------------------------------------------------
# Retry count  [SRC] rtl8187.h:37
# ----------------------------------------------------------------------
RETRY_COUNT = 7
