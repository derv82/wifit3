"""RTL88xxAU family-shared MAC/PHY register addresses + bit constants.

Transcribed verbatim from the Lucid-Duck vendor source (NOT mainline rtw88); these
are the symbols `hal_com_reg.h` / `rtl8812a_spec.h` share across the whole 88xxA
("jaguar") family — the 8821a and 8812a use the same register map for power-on, the
firmware-download mailbox, the LLT, and the EFUSE controller. Per-chip values that
the vendor computes differently (TX page boundary, EEPROM logical offsets, the USB
PID) live in each chip package's ``constants.py``, not here.

[SRC] cites are vendor `file:line`.
"""
from __future__ import annotations


def BIT(n: int) -> int:
    return 1 << n


# --- Vendor control-transfer (rtw88 family) [SRC] include/usb_ops.h:19-31 ---
REALTEK_USB_VENQT_READ = 0xC0     # bmRequestType, device->host
REALTEK_USB_VENQT_WRITE = 0x40    # bmRequestType, host->device
REALTEK_USB_VENQT_CMD_REQ = 0x05  # bRequest
REALTEK_USB_VENQT_CMD_IDX = 0x00  # wIndex
MAX_VENDOR_REQ_CMD_SIZE = 254

# --- USB vendor id (Realtek) — the PID is per-chip ---
USB_VID_REALTEK = 0x0BDA

# --- MAC registers [SRC] include/hal_com_reg.h (shared 8821a/8812a) ---
REG_SYS_ISO_CTRL = 0x0000       # :39
REG_SYS_FUNC_EN = 0x0002        # :40  (+1 = 0x0003, bit2 = 8051 core gate)
REG_APS_FSMCO = 0x0004          # :41  (pwr-seq touches 0x04/0x05/0x06 byte-wise)
REG_SYS_CLKR = 0x0008           # :42
REG_MULTI_FUNC_CTRL = 0x0068    # :84  (WIFI/BT/GPS multi-func; read in read_chip_version)
REG_RF_CTRL = 0x001F            # :53  (path-A RF enable/reset: RF_EN|RF_RSTB|RF_SDMRSTB)
REG_RSV_CTRL = 0x001C           # :52  (8051 reset wrapper)

# --- SYS power-switch bits used by Hal_EfusePowerSwitch (efuse access ON gating) ---
# [SRC] hal_com_reg.h:1165/1181/1216/1218
PWC_EV12V = BIT(15)             # REG_SYS_ISO_CTRL (0x00) 1.2V power (vendor write commented)
FEN_ELDR = BIT(12)              # REG_SYS_FUNC_EN (0x02) eldr reset valid
ANA8M = BIT(1)                  # REG_SYS_CLKR (0x08) 8M ANA clock
LOADER_CLK_EN = BIT(5)          # REG_SYS_CLKR (0x08) loader clock gate
REG_MCUFWDL = 0x0080            # :88  (FW download ctrl; +2 = page idx / 8051 rst hold)
REG_SYS_CFG = 0x00F0            # :102 (no REG_SYS_CFG1/CFG2 in this tree)
REG_CR = 0x0100                 # :117 (MAC DMA / WMAC / SCHEDULE / SEC enable)
REG_LLT_INIT = 0x01E0           # :159
REG_HMETFR = 0x01CC             # :153  (H2C trigger; InitializeFirmwareVars writes 0x0F)
REG_TXDMA_OFFSET_CHK = 0x020C   # :174
REG_AUTO_LLT = 0x0224           # :179

# --- REG_MCUFWDL (0x0080) bits [SRC] hal_com_reg.h:1269-1276 ---
MCUFWDL_EN = BIT(0)
MCUFWDL_RDY = BIT(1)
FWDL_ChkSum_rpt = BIT(2)
MACINI_RDY = BIT(3)
BBINI_RDY = BIT(4)
RFINI_RDY = BIT(5)
WINTINI_RDY = BIT(6)
RAM_DL_SEL = BIT(7)

# --- REG_CR (0x0100) enable bits [SRC] hal_com_reg.h:1345-1355 ---
HCI_TXDMA_EN = BIT(0)
HCI_RXDMA_EN = BIT(1)
TXDMA_EN = BIT(2)
RXDMA_EN = BIT(3)
PROTOCOL_EN = BIT(4)
SCHEDULE_EN = BIT(5)
ENSEC = BIT(9)
CALTMR_EN = BIT(10)
# _InitPowerOn CR-enable composite = 0x063F (shared 8821AU/8812AU)
CR_ENABLE = (HCI_TXDMA_EN | HCI_RXDMA_EN | TXDMA_EN | RXDMA_EN
             | PROTOCOL_EN | SCHEDULE_EN | ENSEC | CALTMR_EN)

# --- LLT [SRC] hal_com_reg.h:1418-1425,1865,1876 ---
_LLT_NO_ACTIVE = 0x0
_LLT_WRITE_ACCESS = 0x1
LAST_ENTRY_OF_TX_PKT_BUFFER_8812 = 255
POLLING_LLT_THRESHOLD = 20


def _LLT_INIT_DATA(x: int) -> int:
    return x & 0xFF


def _LLT_INIT_ADDR(x: int) -> int:
    return (x & 0xFF) << 8


def _LLT_OP(x: int) -> int:
    return (x & 0x3) << 30


def _LLT_OP_VALUE(x: int) -> int:
    return (x >> 30) & 0x3


# --- USB drop-incorrect-bulkout [SRC] hal_com_reg.h:1452 (ENABLE_USB_DROP_INCORRECT_OUT) ---
DROP_DATA_EN = BIT(9)

# --- Firmware download [SRC] include/rtl8812a_hal.h:65-66, include/hal_com.h:158 ---
FW_START_ADDRESS = 0x1000
MAX_DLFW_PAGE_SIZE = 4096
FW_SIZE_8812 = 0x8000      # max RAM code size (88xxA)
FW_HEADER_SIZE = 32        # skipped before download (FirmwareDownload8812:618-622)

# TX descriptor [SRC] include/rtw_xmit.h:215 (default IC branch) — 40 bytes for the
# 8812a/8821a (the 8822b/8821c use 48). TXDESC_OFFSET == TXDESC_SIZE here.
TXDESC_SIZE = 40

# --- EFUSE read [SRC] hal_com_reg.h + rtl8812a_hal.h (JAGUAR) + core/efuse ---
REG_9346CR = 0x000A             # autoload status (bit5 = EEPROM present)
REG_EFUSE_CTRL = 0x0030         # +1 addr[7:0], +2 addr[9:8], +3 bit7 = ready/trigger
REG_EFUSE_ACCESS = 0x00CF       # efuse access protection
EFUSE_ACCESS_ON = 0x69
EFUSE_ACCESS_OFF = 0x00
EFUSE_MAP_LEN_JAGUAR = 512      # logical map length
EFUSE_MAX_SECTION_JAGUAR = 64   # EFUSE_MAP_LEN / 8
EFUSE_MAX_WORD_UNIT = 4         # words per PG section (JAGUAR)
EFUSE_REAL_CONTENT_LEN_JAGUAR = 512   # physical efuse size (non-8814 jaguar)
EEPROM_DEFAULT_CRYSTAL_CAP = 0x20

# Bit shorthands used by inline pokes.
BIT0, BIT1, BIT2, BIT6, BIT7 = BIT(0), BIT(1), BIT(2), BIT(6), BIT(7)
