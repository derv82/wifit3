"""Register addresses, bitfields and magic constants for the Ralink RT3070.

Every symbol here is transcribed verbatim from the mainline kernel headers in
``data_dumps/rt2x00-source-v6.18/`` — never typed from memory [[feedback_constants_from_source]].
``[SRC]`` cites the header + line so a future reader can re-verify.

Field masks follow the kernel ``FIELD32`` convention: the mask marks the bit
span and the value is shifted to the mask's lowest set bit. ``set_field`` /
``get_field`` mirror ``rt2x00_set_field32`` / ``rt2x00_get_field32``.
"""
from __future__ import annotations

# --- USB vendor request codes [SRC rt2x00usb.h:50-56] ----------------------
USB_DEVICE_MODE = 1
USB_SINGLE_WRITE = 2
USB_SINGLE_READ = 3
USB_MULTI_WRITE = 6
USB_MULTI_READ = 7
USB_EEPROM_READ = 9

# --- USB_DEVICE_MODE sub-modes [SRC rt2x00usb.h:65-72] ---------------------
USB_MODE_RESET = 1
USB_MODE_FIRMWARE = 8
USB_MODE_AUTORUN = 17

# --- bmRequestType [SRC rt2x00usb.h:42-44; USB_TYPE_VENDOR|RECIP_DEVICE] ----
USB_VENDOR_REQUEST_IN = 0xC0   # USB_DIR_IN  | USB_TYPE_VENDOR | USB_RECIP_DEVICE
USB_VENDOR_REQUEST_OUT = 0x40  # USB_DIR_OUT | USB_TYPE_VENDOR | USB_RECIP_DEVICE

# --- transport tunables [SRC rt2x00usb.h:30-37, rt2x00.h:1041-1043] ---------
CSR_CACHE_SIZE = 64               # max bytes per vendor-request chunk
REGISTER_TIMEOUT = 100            # ms
REGISTER_TIMEOUT_FIRMWARE = 1000  # ms
REGISTER_BUSY_COUNT = 100         # CSR-ready / PBF-ready poll attempts
REGISTER_USB_BUSY_COUNT = 20      # indirect-register (BBP/RFCSR/MCU/EFUSE) poll attempts

# --- EEPROM / RF sizes [SRC rt2800.h:100,104] ------------------------------
EEPROM_SIZE = 0x0200              # bytes (256 u16 words)
RF_SIZE = 0x0010

# --- chip identity [SRC rt2800.h:722-724] ----------------------------------
MAC_CSR0 = 0x1000
MAC_CSR0_REVISION = 0x0000FFFF
MAC_CSR0_CHIPSET = 0xFFFF0000

# RT chipset ids (== the MAC_CSR0 chipset field) [SRC rt2x00.h:149-154]
RT2860 = 0x2860
RT2872 = 0x2872
RT3070 = 0x3070
RT3071 = 0x3071
RT3090 = 0x3090

# chipset revisions [SRC rt2800.h:81-83]
REV_RT3070E = 0x0200
REV_RT3070F = 0x0201
REV_RT3071E = 0x0211

# RF chip ids [SRC rt2800.h:53-63] (EEPROM NIC_CONF0 RF_TYPE field)
RF3020 = 0x0005
RF3021 = 0x0007
RF3022 = 0x0008
RF3070 = 0x3070

# --- MAC system control [SRC rt2800.h:729-734] -----------------------------
MAC_SYS_CTRL = 0x1004
MAC_SYS_CTRL_RESET_CSR = 0x00000001
MAC_SYS_CTRL_RESET_BBP = 0x00000002
MAC_SYS_CTRL_ENABLE_TX = 0x00000004
MAC_SYS_CTRL_ENABLE_RX = 0x00000008

# --- PBF system control [SRC rt2800.h:568-570] -----------------------------
PBF_SYS_CTRL = 0x0400
PBF_SYS_CTRL_READY = 0x00000080

# --- WPDMA global config [SRC rt2800.h:346-352] ----------------------------
WPDMA_GLO_CFG = 0x0208
WPDMA_GLO_CFG_ENABLE_TX_DMA = 0x00000001
WPDMA_GLO_CFG_TX_DMA_BUSY = 0x00000002
WPDMA_GLO_CFG_ENABLE_RX_DMA = 0x00000004
WPDMA_GLO_CFG_RX_DMA_BUSY = 0x00000008
WPDMA_GLO_CFG_TX_WRITEBACK_DONE = 0x00000040

# --- USB DMA config [SRC rt2800.h:541-549] ---------------------------------
USB_DMA_CFG = 0x02A0
USB_DMA_CFG_RX_BULK_AGG_TIMEOUT = 0x000000FF
USB_DMA_CFG_RX_BULK_AGG_LIMIT = 0x0000FF00
USB_DMA_CFG_PHY_CLEAR = 0x00010000
USB_DMA_CFG_RX_BULK_AGG_EN = 0x00200000
USB_DMA_CFG_RX_BULK_EN = 0x00400000
USB_DMA_CFG_TX_BULK_EN = 0x00800000

# --- EFUSE [SRC rt2800.h:655-679] ------------------------------------------
EFUSE_CTRL = 0x0580
EFUSE_CTRL_ADDRESS_IN = 0x03FE0000     # NB: a u16-WORD index, not a byte offset
EFUSE_CTRL_MODE = 0x000000C0
EFUSE_CTRL_KICK = 0x40000000
EFUSE_CTRL_PRESENT = 0x80000000
EFUSE_DATA0 = 0x0590
EFUSE_DATA1 = 0x0594
EFUSE_DATA2 = 0x0598
EFUSE_DATA3 = 0x059C

# --- GPIO control (rfkill direction) [SRC rt2800.h:442,453] ----------------
GPIO_CTRL = 0x0228
GPIO_CTRL_DIR2 = 0x00000400

# --- firmware / MCU mailbox [SRC rt2800usb.h:25, rt2800.h:2112-2143,575] ----
FIRMWARE_IMAGE_BASE = 0x3000
AUTOWAKEUP_CFG = 0x1208
HOST_CMD_CSR = 0x0404
HOST_CMD_CSR_HOST_COMMAND = 0x000000FF
H2M_MAILBOX_CSR = 0x7010
H2M_MAILBOX_CSR_ARG0 = 0x000000FF
H2M_MAILBOX_CSR_ARG1 = 0x0000FF00
H2M_MAILBOX_CSR_CMD_TOKEN = 0x00FF0000
H2M_MAILBOX_CSR_OWNER = 0xFF000000
H2M_MAILBOX_CID = 0x7014
H2M_MAILBOX_STATUS = 0x701C
H2M_INT_SRC = 0x7024
H2M_BBP_AGENT = 0x7028

# MCU command opcodes [SRC rt2800.h:3023-3033]
MCU_SLEEP = 0x30
MCU_WAKEUP = 0x31
MCU_RADIO_OFF = 0x35
MCU_CURRENT = 0x36
MCU_LED = 0x50
MCU_BOOT_SIGNAL = 0x72

# --- indirect BBP register access [SRC rt2800.h:808-814] -------------------
BBP_CSR_CFG = 0x101C
BBP_CSR_CFG_VALUE = 0x000000FF
BBP_CSR_CFG_REGNUM = 0x0000FF00
BBP_CSR_CFG_READ_CONTROL = 0x00010000
BBP_CSR_CFG_BUSY = 0x00020000
BBP_CSR_CFG_BBP_RW_MODE = 0x00080000

# --- indirect RF (RFCSR) register access [SRC rt2800.h:628-632] ------------
RF_CSR_CFG = 0x0500
RF_CSR_CFG_DATA = 0x000000FF
RF_CSR_CFG_REGNUM = 0x00003F00
RF_CSR_CFG_WRITE = 0x00010000
RF_CSR_CFG_BUSY = 0x00020000

# --- EEPROM word map [SRC rt2800lib.c:308-347 rt2800_eeprom_map] -----------
EEPROM_CHIP_ID = 0x0000
EEPROM_VERSION = 0x0001
EEPROM_MAC_ADDR_0 = 0x0002
EEPROM_MAC_ADDR_1 = 0x0003
EEPROM_MAC_ADDR_2 = 0x0004
EEPROM_NIC_CONF0 = 0x001A
EEPROM_NIC_CONF1 = 0x001B
EEPROM_FREQ = 0x001D
EEPROM_LED_AG_CONF = 0x001E
EEPROM_LED_ACT_CONF = 0x001F
EEPROM_LED_POLARITY = 0x0020
EEPROM_NIC_CONF2 = 0x0021
EEPROM_LNA = 0x0022
EEPROM_RSSI_BG = 0x0023
EEPROM_RSSI_BG2 = 0x0024
EEPROM_RSSI_A = 0x0025
EEPROM_RSSI_A2 = 0x0026

# EEPROM bitfields [SRC rt2800.h:2681-2683,2727] (FIELD16)
EEPROM_NIC_CONF0_RXPATH = 0x000F
EEPROM_NIC_CONF0_TXPATH = 0x00F0
EEPROM_NIC_CONF0_RF_TYPE = 0x0F00
EEPROM_FREQ_OFFSET = 0x00FF

# DATA_FRAME_SIZE [SRC rt2x00queue.h:28] — RX bulk-agg-limit math in enable_radio
DATA_FRAME_SIZE = 2432


def _shift(mask: int) -> int:
    """Bit offset of a FIELD mask's lowest set bit (kernel ``rt2x00_field*``)."""
    return (mask & -mask).bit_length() - 1


def set_field(reg: int, mask: int, value: int) -> int:
    """Replace the ``mask`` bits of ``reg`` with ``value`` shifted into place.

    Mirrors ``rt2x00_set_field32``; result masked to 32 bits.
    """
    shift = _shift(mask)
    return ((reg & ~mask) | ((value << shift) & mask)) & 0xFFFFFFFF


def get_field(reg: int, mask: int) -> int:
    """Extract the ``mask`` bits of ``reg`` (mirrors ``rt2x00_get_field32``)."""
    return (reg & mask) >> _shift(mask)
