"""rt2500usb (Ralink RT2570) register map + USB protocol constants.

Ported verbatim from the kernel sources so the addresses can be trusted
as ground truth:

  * USB request enums / vendor request types   ← rt2x00usb.h
  * CSR / BBP / RF register offsets + bitfields ← rt2500usb.h
  * USB VID:PID device table                    ← rt2500usb.c device table

RT2570 CSRs are **16-bit** (cf. rt2800's 32-bit). BBP and RF are not
directly addressable — they are reached indirectly through the PHY_CSR
busy-poll registers (see transport.py / kernel rt2500usb_bbp_*,
rt2500usb_rf_write).

Bitfield masks are stored as raw ``(mask)`` ints; use the helpers in
``transport`` (get_field16 / set_field16) to extract/insert.
"""
from __future__ import annotations
from wifit3.chips.products import ASUS, Belkin, Buffalo, DLink, Gigabyte, Hercules, Linksys, MSI, NovaTech, Ralink, Sagem, Siemens, Spairon, SureCom, VTech, Zinwell

# ---------------------------------------------------------------------------
# USB device identity (rt2500usb.c device table). The Buffalo/Melco
# "Nintendo Wi-Fi USB Connector" is 0x0411:0x008b. The other IDs are the
# rest of the rt2500usb table — listed so the one driver claims them all.
# ---------------------------------------------------------------------------
USB_VID_MELCO = 0x0411          # Buffalo / Melco (Nintendo Wi-Fi connector)
USB_PID_NINTENDO_WIFI = 0x008b

# (vid, pid, chipset, vendor, product_name): the full rt2500usb device table.
# The 0x050d:0x7051 (Broadcom BCM4320) and 0x0707:0xee13 (Intersil ISL3887) rows
# from the kernel table are dropped: neither is an RT2570 this driver can drive.
RT2500USB_DEVICE_TABLE: list[tuple[int, int, str, str | None, str | None]] = [
    (0x0b05, 0x1706, "RT2570", None, ASUS.WL_167G),
    (0x0b05, 0x1707, "RT2570", None, ASUS.WL_167G),
    (0x050d, 0x7050, "RT2570", None, Belkin.F5D7050),
    (0x13b1, 0x000d, "RT2570", None, Linksys.WUSB54G_V4),
    (0x13b1, 0x0011, "RT2570", None, Linksys.WUSB54GP_V4),
    (0x13b1, 0x001a, "RT2570", None, Linksys.HU200TS),
    (0x14b2, 0x3c02, "RT2570", None, Ralink.C54RUV2),
    (0x2001, 0x3c00, "RT2570", None, DLink.DWL_G122_REV_B1),
    (0x1044, 0x8001, "RT2570", None, Gigabyte.GN_54G),
    (0x1044, 0x8007, "RT2570", None, Gigabyte.GN_WBKG),
    (0x06f8, 0xe000, "RT2570", None, Hercules.HWGUSB2_54),
    (0x0411, 0x005e, "RT2570", None, Buffalo.WLI_U2_KG54_YB),
    (0x0411, 0x0066, "RT2570", None, Buffalo.WLI_U2_KG54),
    (0x0411, 0x0067, "RT2570", None, Buffalo.WLI_U2_KG54_AI),
    (0x0411, 0x008b, "RT2570", None, Buffalo.WI_FI),
    (0x0411, 0x0097, "RT2570", None, Buffalo.WLI_U2_KG54_BB),
    (0x0db0, 0x6861, "RT2570", None, MSI.MS_6861),
    (0x0db0, 0x6865, "RT2570", None, MSI.MS_6865),
    (0x0db0, 0x6869, "RT2570", None, MSI.MS_6869),
    (0x148f, 0x1706, "RT2570", None, None),
    (0x148f, 0x2570, "RT2570", None, None),
    (0x148f, 0x9020, "RT2570", None, None),
    (0x079b, 0x004b, "RT2570", None, Sagem.WIFI_11G),
    (0x0681, 0x3c06, "RT2570", None, Siemens._54G),
    (0x114b, 0x0110, "RT2570", None, Spairon.UB801R),
    (0x0769, 0x11f3, "RT2570", None, SureCom.RT2570),
    (0x0eb0, 0x9020, "RT2570", None, NovaTech.NV_902W),
    (0x0f88, 0x3012, "RT2570", None, VTech.RT2570),
    (0x5a57, 0x0260, "RT2570", None, Zinwell.ZWX_G261),
]

# ---------------------------------------------------------------------------
# USB vendor request protocol (rt2x00usb.h)
#   USB_VENDOR_REQUEST = USB_TYPE_VENDOR | USB_RECIP_DEVICE
# ---------------------------------------------------------------------------
USB_VENDOR_REQUEST_IN = 0xC0    # DIR_IN  | TYPE_VENDOR | RECIP_DEVICE
USB_VENDOR_REQUEST_OUT = 0x40   # DIR_OUT | TYPE_VENDOR | RECIP_DEVICE

# enum rt2x00usb_vendor_request (rt2x00usb.h:48-57)
USB_DEVICE_MODE = 1
USB_SINGLE_WRITE = 2
USB_SINGLE_READ = 3
USB_MULTI_WRITE = 6     # rt2500usb register write  (offset → wIndex)
USB_MULTI_READ = 7      # rt2500usb register read
USB_EEPROM_WRITE = 8
USB_EEPROM_READ = 9     # one-shot whole-EEPROM read (wValue=wIndex=0)

# enum rt2x00usb_mode_offset — wValue for USB_DEVICE_MODE (rt2x00usb.h:64-72).
USB_MODE_RESET = 1
USB_MODE_UNPLUG = 2
USB_MODE_FUNCTION = 3
USB_MODE_TEST = 4

# Timeouts (rt2x00usb.h:30-32), milliseconds.
REGISTER_TIMEOUT = 100
REGISTER_TIMEOUT_FIRMWARE = 1000
EEPROM_TIMEOUT = 2000

# Indirect-register busy-poll budget (rt2x00usb.h). The kernel retries
# REGISTER_USB_BUSY_COUNT times with REGISTER_BUSY_DELAY us between tries.
REGISTER_USB_BUSY_COUNT = 20
REGISTER_BUSY_DELAY = 100       # microseconds

# CSR_CACHE_SIZE: max bytes per multi-byte control transfer (rt2x00usb.h).
CSR_CACHE_SIZE = 64

# ---------------------------------------------------------------------------
# Register layout (rt2500usb.h:42-50)
# ---------------------------------------------------------------------------
CSR_REG_BASE = 0x0400
CSR_REG_SIZE = 0x0100
EEPROM_BASE = 0x0000
EEPROM_SIZE = 0x006e            # 110 bytes — one-shot EEPROM read length
BBP_BASE = 0x0000
BBP_SIZE = 0x0060
RF_BASE = 0x0004
RF_SIZE = 0x0010

NUM_TX_QUEUES = 2

DEFAULT_RSSI_OFFSET = 120       # RSSI <-> dBm conversion (rt2500usb.h:38)

# RT2570 chip revisions (rt2500usb.h:30-32). rt2x00_rev = MAC_CSR0 low nibble.
RT2570_VERSION_B = 2
RT2570_VERSION_C = 3
RT2570_VERSION_D = 4

# Queue frame sizes (rt2x00queue.h:28-29). MAC_CSR8 max-frame uses DATA size.
DATA_FRAME_SIZE = 2432
MGMT_FRAME_SIZE = 256

# enum dev_state (rt2x00reg.h:60-64) — MAC_CSR17 power states.
STATE_DEEP_SLEEP = 0
STATE_SLEEP = 1
STATE_STANDBY = 2
STATE_AWAKE = 3

# enum cipher (rt2x00reg.h:101) + 802.11 header length (rt2x00.h:113).
CIPHER_NONE = 0
IEEE80211_HEADER = 24

# enum antenna (rt2x00reg.h:29-33).
ANTENNA_SW_DIVERSITY = 0
ANTENNA_A = 1
ANTENNA_B = 2
ANTENNA_HW_DIVERSITY = 3

# RF chip ids (rt2500usb.h:20-25) — value of EEPROM_ANTENNA_RF_TYPE.
RF2522 = 0x0000
RF2523 = 0x0001
RF2524 = 0x0002
RF2525 = 0x0003
RF2525E = 0x0005
RF5222 = 0x0010

RF_NAMES: dict[int, str] = {
    RF2522: "RF2522", RF2523: "RF2523", RF2524: "RF2524",
    RF2525: "RF2525", RF2525E: "RF2525E", RF5222: "RF5222",
}

# ---------------------------------------------------------------------------
# MAC Control/Status Registers (rt2500usb.h:62-221). 16-bit each.
# ---------------------------------------------------------------------------
MAC_CSR0 = 0x0400               # ASIC revision number
MAC_CSR1 = 0x0402               # System control
MAC_CSR1_SOFT_RESET = 0x0001
MAC_CSR1_BBP_RESET = 0x0002
MAC_CSR1_HOST_READY = 0x0004
MAC_CSR2 = 0x0404               # STA MAC register 0 (bytes 0,1)
MAC_CSR3 = 0x0406               # STA MAC register 1 (bytes 2,3)
MAC_CSR4 = 0x0408               # STA MAC register 2 (bytes 4,5)
MAC_CSR5 = 0x040a               # BSSID register 0
MAC_CSR6 = 0x040c               # BSSID register 1
MAC_CSR7 = 0x040e               # BSSID register 2
MAC_CSR8 = 0x0410               # Max frame length
MAC_CSR8_MAX_FRAME_UNIT = 0x0fff
MAC_CSR9 = 0x0412               # Timer control
MAC_CSR10 = 0x0414              # Slot time
MAC_CSR11 = 0x0416              # SIFS
MAC_CSR12 = 0x0418              # EIFS
MAC_CSR13 = 0x041a              # Power mode0
MAC_CSR14 = 0x041c              # Power mode1
MAC_CSR15 = 0x041e              # Power saving transition0
MAC_CSR16 = 0x0420              # Power saving transition1
MAC_CSR17 = 0x0422              # Manual power control / status
MAC_CSR17_SET_STATE = 0x0001
MAC_CSR17_BBP_DESIRE_STATE = 0x0006
MAC_CSR17_RF_DESIRE_STATE = 0x0018
MAC_CSR17_BBP_CURR_STATE = 0x0060
MAC_CSR17_RF_CURR_STATE = 0x0180
MAC_CSR17_PUT_TO_SLEEP = 0x0200
MAC_CSR18 = 0x0424              # Wakeup timer
MAC_CSR18_DELAY_AFTER_BEACON = 0x00ff
MAC_CSR18_BEACONS_BEFORE_WAKEUP = 0x7f00
MAC_CSR18_AUTO_WAKE = 0x8000
MAC_CSR19 = 0x0426              # GPIO control (rfkill on VAL0)
MAC_CSR20 = 0x0428             # LED control
MAC_CSR20_ACTIVITY = 0x0001
MAC_CSR20_LINK = 0x0002
MAC_CSR20_ACTIVITY_POLARITY = 0x0004
MAC_CSR21 = 0x042a             # LED on/off period
MAC_CSR21_ON_PERIOD = 0x00ff
MAC_CSR21_OFF_PERIOD = 0xff00
MAC_CSR22 = 0x042c             # Collision window control

# ---------------------------------------------------------------------------
# TX/RX Control/Status Registers (rt2500usb.h:231-372).
# ---------------------------------------------------------------------------
TXRX_CSR0 = 0x0440             # Security control
TXRX_CSR0_ALGORITHM = 0x0007
TXRX_CSR0_IV_OFFSET = 0x01f8
TXRX_CSR0_KEY_ID = 0x1e00
TXRX_CSR1 = 0x0442             # TX configuration
TXRX_CSR1_ACK_TIMEOUT = 0x00ff
TXRX_CSR1_TSF_OFFSET = 0x7f00
TXRX_CSR1_AUTO_SEQUENCE = 0x8000
TXRX_CSR2 = 0x0444             # RX control (the monitor-mode filter reg)
TXRX_CSR2_DISABLE_RX = 0x0001
TXRX_CSR2_DROP_CRC = 0x0002
TXRX_CSR2_DROP_PHYSICAL = 0x0004
TXRX_CSR2_DROP_CONTROL = 0x0008
TXRX_CSR2_DROP_NOT_TO_ME = 0x0010
TXRX_CSR2_DROP_TODS = 0x0020
TXRX_CSR2_DROP_VERSION_ERROR = 0x0040
TXRX_CSR2_DROP_MULTICAST = 0x0200
TXRX_CSR2_DROP_BROADCAST = 0x0400
TXRX_CSR3 = 0x0446             # CCK RX BBP ID
TXRX_CSR4 = 0x0448             # OFDM RX BBP ID
# TXRX_CSR5..8 share the BBP-id layout (rt2500usb.h:282-312).
TXRX_CSR5 = 0x044a             # CCK TX BBP ID0
TXRX_CSR6 = 0x044c             # CCK TX BBP ID1
TXRX_CSR7 = 0x044e            # OFDM TX BBP ID0
TXRX_CSR8 = 0x0450            # OFDM TX BBP ID1
TXRX_CSR_BBP_ID0 = 0x007f
TXRX_CSR_BBP_ID0_VALID = 0x0080
TXRX_CSR_BBP_ID1 = 0x7f00
TXRX_CSR_BBP_ID1_VALID = 0x8000
TXRX_CSR9 = 0x0452            # TX ACK time-out
TXRX_CSR10 = 0x0454           # Auto responder control
TXRX_CSR10_AUTORESPOND_PREAMBLE = 0x0004
TXRX_CSR11 = 0x0456           # Auto responder basic rate
TXRX_CSR12 = 0x0458           # ACK/CTS time
TXRX_CSR13 = 0x045a
TXRX_CSR14 = 0x045c
TXRX_CSR15 = 0x045e
TXRX_CSR16 = 0x0460
TXRX_CSR17 = 0x0462
TXRX_CSR18 = 0x0464           # Synchronization control
TXRX_CSR18_OFFSET = 0x000f
TXRX_CSR18_INTERVAL = 0xfff0
TXRX_CSR19 = 0x0466           # Synchronization control
TXRX_CSR19_TSF_COUNT = 0x0001
TXRX_CSR19_TSF_SYNC = 0x0006
TXRX_CSR19_TBCN = 0x0008
TXRX_CSR19_BEACON_GEN = 0x0010
TXRX_CSR20 = 0x0468           # TX BEACON offset time control
TXRX_CSR20_OFFSET = 0x1fff
TXRX_CSR20_BCN_EXPECT_WINDOW = 0xe000
TXRX_CSR21 = 0x046a

# Shared-key table base (rt2500usb.h:389; KEY_ENTRY(idx) = SEC_CSR0 + idx*16).
SEC_CSR0 = 0x0480

# ---------------------------------------------------------------------------
# PHY control registers (rt2500usb.h:465-551). BBP/RF indirect access.
# ---------------------------------------------------------------------------
PHY_CSR0 = 0x04c0             # RF switching timing control
PHY_CSR1 = 0x04c2             # TX PA configuration
PHY_CSR2 = 0x04c4             # TX MAC configuration
PHY_CSR2_LNA = 0x0002
PHY_CSR2_LNA_MODE = 0x3000
PHY_CSR3 = 0x04c6             # RX MAC configuration
PHY_CSR4 = 0x04c8             # Interface configuration
PHY_CSR4_LOW_RF_LE = 0x0001
PHY_CSR5 = 0x04ca             # BBP pre-TX CCK
PHY_CSR5_CCK = 0x0003
PHY_CSR5_CCK_FLIP = 0x0004
PHY_CSR6 = 0x04cc             # BBP pre-TX OFDM
PHY_CSR6_OFDM = 0x0003
PHY_CSR6_OFDM_FLIP = 0x0004
PHY_CSR7 = 0x04ce             # BBP access register 0
PHY_CSR7_DATA = 0x00ff
PHY_CSR7_REG_ID = 0x7f00
PHY_CSR7_READ_CONTROL = 0x8000  # 0: write, 1: read
PHY_CSR8 = 0x04d0             # BBP access register 1 (busy flag)
PHY_CSR8_BUSY = 0x0001
PHY_CSR9 = 0x04d2             # RF access register (low 16 bits of value)
PHY_CSR9_RF_VALUE = 0xffff
PHY_CSR10 = 0x04d4            # RF access register (high bits + control)
PHY_CSR10_RF_VALUE = 0x00ff
PHY_CSR10_RF_NUMBER_OF_BITS = 0x1f00
PHY_CSR10_RF_IF_SELECT = 0x2000
PHY_CSR10_RF_PLL_LD = 0x4000
PHY_CSR10_RF_BUSY = 0x8000

# ---------------------------------------------------------------------------
# Statistics registers (rt2500usb.h:557-594).
# ---------------------------------------------------------------------------
STA_CSR0 = 0x04e0             # FCS error count (cleared on read)
STA_CSR0_FCS_ERROR = 0xffff
STA_CSR1 = 0x04e2             # PLCP error count
STA_CSR2 = 0x04e4             # LONG error count
STA_CSR3 = 0x04e6             # CCA false alarm
STA_CSR3_FALSE_CCA_ERROR = 0xffff
STA_CSR4 = 0x04e8             # RX FIFO overflow
STA_CSR5 = 0x04ea             # Beacon sent counter

# ---------------------------------------------------------------------------
# BBP registers (rt2500usb.h:604-611). 8-bit wordsize, indirect.
# ---------------------------------------------------------------------------
BBP_R2_TX_ANTENNA = 0x03
BBP_R2_TX_IQ_FLIP = 0x04
BBP_R14_RX_ANTENNA = 0x03
BBP_R14_RX_IQ_FLIP = 0x04

# RF register fields (rt2500usb.h:620-626).
RF1_TUNER = 0x00020000
RF3_TUNER = 0x00000100
RF3_TXPOWER = 0x00003e00

# ---------------------------------------------------------------------------
# EEPROM contents (rt2500usb.h:635-743). Word offsets (16-bit words);
# byte offset into a one-shot EEPROM read = word * 2.
# ---------------------------------------------------------------------------
EEPROM_MAC_ADDR_0 = 0x0002      # MAC bytes 0,1  → byte offset 4
EEPROM_MAC_ADDR_1 = 0x0003      # MAC bytes 2,3
EEPROM_MAC_ADDR_2 = 0x0004      # MAC bytes 4,5
EEPROM_ANTENNA = 0x000b
EEPROM_ANTENNA_NUM = 0x0003
EEPROM_ANTENNA_TX_DEFAULT = 0x000c
EEPROM_ANTENNA_RX_DEFAULT = 0x0030
EEPROM_ANTENNA_LED_MODE = 0x01c0
EEPROM_ANTENNA_DYN_TXAGC = 0x0200
EEPROM_ANTENNA_HARDWARE_RADIO = 0x0400
EEPROM_ANTENNA_RF_TYPE = 0xf800
EEPROM_NIC = 0x000c
EEPROM_NIC_CARDBUS_ACCEL = 0x0001
EEPROM_NIC_DYN_BBP_TUNE = 0x0002
EEPROM_NIC_CCK_TX_POWER = 0x000c
EEPROM_GEOGRAPHY = 0x000d
EEPROM_GEOGRAPHY_GEO = 0x0f00
EEPROM_BBP_START = 0x000e
EEPROM_BBP_SIZE = 16
EEPROM_BBP_VALUE = 0x00ff
EEPROM_BBP_REG_ID = 0xff00
EEPROM_TXPOWER_START = 0x001e
EEPROM_TXPOWER_SIZE = 7

# BBP link-tuning seeds (rt2500usb.h:701-737). reset_tuner reads the *_LOW /
# VGCUPPER bytes and writes them to BBP R24/R25/R61/R17 — the AGC/VGC seed,
# re-applied on every channel tune. The kernel fills blank (0xffff) words with
# the defaults below in rt2500usb_init_eeprom (validate); a real EEPROM (this
# unit) carries calibrated values, so those branches don't fire here.
EEPROM_BBPTUNE = 0x0030
EEPROM_BBPTUNE_THRESHOLD = 0x00ff
EEPROM_BBPTUNE_R24 = 0x0031
EEPROM_BBPTUNE_R24_LOW = 0x00ff
EEPROM_BBPTUNE_R24_HIGH = 0xff00
EEPROM_BBPTUNE_R25 = 0x0032
EEPROM_BBPTUNE_R25_LOW = 0x00ff
EEPROM_BBPTUNE_R25_HIGH = 0xff00
EEPROM_BBPTUNE_R61 = 0x0033
EEPROM_BBPTUNE_R61_LOW = 0x00ff
EEPROM_BBPTUNE_R61_HIGH = 0xff00
EEPROM_BBPTUNE_VGC = 0x0034
EEPROM_BBPTUNE_VGCUPPER = 0x00ff
EEPROM_BBPTUNE_VGCLOWER = 0xff00
EEPROM_BBPTUNE_R17 = 0x0035
EEPROM_BBPTUNE_R17_LOW = 0x00ff
EEPROM_BBPTUNE_R17_HIGH = 0xff00
# Blank-EEPROM defaults (rt2500usb.c:1398-1420, 1381) for the bytes reset_tuner
# consumes. VGCUPPER's blank default is a constant 0x40, so reset_tuner never
# needs the live BBP[17] read that only the (absent) periodic tuner's VGCLOWER
# would require.
EEPROM_BBPTUNE_R24_LOW_DEFAULT = 0x40
EEPROM_BBPTUNE_R25_LOW_DEFAULT = 0x40
EEPROM_BBPTUNE_R61_LOW_DEFAULT = 0x60
EEPROM_BBPTUNE_VGCUPPER_DEFAULT = 0x40

EEPROM_CALIBRATE_OFFSET = 0x0036
EEPROM_CALIBRATE_OFFSET_RSSI = 0x00ff

# ---------------------------------------------------------------------------
# DMA descriptor sizes (rt2500usb.h:748-749).
# ---------------------------------------------------------------------------
TXD_DESC_SIZE = 5 * 4           # TX descriptor: 5 x __le32
RXD_DESC_SIZE = 4 * 4           # RX descriptor: 4 x __le32

# TX descriptor fields (rt2500usb.h:758-784). The TXD is prepended to the
# frame in the bulk-OUT URB (rt2500usb_write_tx_desc).
TXD_W0_PACKET_ID = 0x0000000f
TXD_W0_RETRY_LIMIT = 0x000000f0
TXD_W0_MORE_FRAG = 0x00000100
TXD_W0_ACK = 0x00000200
TXD_W0_TIMESTAMP = 0x00000400
TXD_W0_OFDM = 0x00000800
TXD_W0_NEW_SEQ = 0x00001000
TXD_W0_IFS = 0x00006000
TXD_W0_DATABYTE_COUNT = 0x0fff0000
TXD_W0_CIPHER = 0x20000000
TXD_W0_KEY_ID = 0xc0000000
TXD_W1_IV_OFFSET = 0x0000003f
TXD_W1_AIFS = 0x000000c0
TXD_W1_CWMIN = 0x00000f00
TXD_W1_CWMAX = 0x0000f000
TXD_W2_PLCP_SIGNAL = 0x000000ff
TXD_W2_PLCP_SERVICE = 0x0000ff00
TXD_W2_PLCP_LENGTH_LOW = 0x00ff0000
TXD_W2_PLCP_LENGTH_HIGH = 0xff000000

# RX descriptor fields (rt2500usb.h:803-818). The RXD trails the frame
# data in the bulk-IN URB (rt2500usb.c:1222-1225).
RXD_W0_UNICAST_TO_ME = 0x00000002
RXD_W0_MULTICAST = 0x00000004
RXD_W0_BROADCAST = 0x00000008
RXD_W0_MY_BSS = 0x00000010
RXD_W0_CRC_ERROR = 0x00000020
RXD_W0_OFDM = 0x00000040
RXD_W0_PHYSICAL_ERROR = 0x00000080
RXD_W0_CIPHER = 0x00000100
RXD_W0_CIPHER_ERROR = 0x00000200
RXD_W0_DATABYTE_COUNT = 0x0fff0000
RXD_W1_RSSI = 0x000000ff
RXD_W1_SIGNAL = 0x0000ff00

# TX power clamp (rt2500usb.h:834-836).
MIN_TXPOWER = 0
MAX_TXPOWER = 31
DEFAULT_TXPOWER = 24
