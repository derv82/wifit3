"""RTL8821AU (DKMS/PHYDM) constants — transcribed verbatim from the Lucid-Duck
vendor source, NOT mainline rtw88. Register names follow this tree: the 8821a
uses REG_MCUFWDL (0x0080) for FW-download control, not the 8814a/8822b-era
REG_MCUFW_CTRL — those symbols do not exist here.

[SRC] cites are vendor `file:line`.
"""
from __future__ import annotations


def BIT(n: int) -> int:
    return 1 << n


# --- USB IDs ---
USB_VID_REALTEK = 0x0BDA
USB_PID_AWUS036ACS = 0x0811  # RTL8811AU/8821AU; [SRC] supported-device-IDs

# --- Vendor control-transfer (rtw88 family) [SRC] include/usb_ops.h:19-31 ---
REALTEK_USB_VENQT_READ = 0xC0     # bmRequestType, device->host
REALTEK_USB_VENQT_WRITE = 0x40    # bmRequestType, host->device
REALTEK_USB_VENQT_CMD_REQ = 0x05  # bRequest
REALTEK_USB_VENQT_CMD_IDX = 0x00  # wIndex
MAX_VENDOR_REQ_CMD_SIZE = 254

# --- MAC registers [SRC] include/hal_com_reg.h ---
REG_SYS_ISO_CTRL = 0x0000       # :39
REG_SYS_FUNC_EN = 0x0002        # :40  (+1 = 0x0003, bit2 = 8051 core gate)
REG_APS_FSMCO = 0x0004          # :41  (pwr-seq touches 0x04/0x05/0x06 byte-wise)
REG_SYS_CLKR = 0x0008           # :42
REG_RSV_CTRL = 0x001C           # :52  (8051 reset wrapper)
REG_MCUFWDL = 0x0080            # :88  (FW download ctrl; +2 = page idx / 8051 rst hold)
REG_SYS_CFG = 0x00F0            # :102 (no REG_SYS_CFG1/CFG2 in this tree)
REG_CR = 0x0100                 # :117 (MAC DMA / WMAC / SCHEDULE / SEC enable)
REG_LLT_INIT = 0x01E0           # :159
REG_HMETFR = 0x01CC             # :153  (H2C trigger; InitializeFirmwareVars8812 writes 0x0F)
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
# _InitPowerOn_8812AU CR-enable composite = 0x063F
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


# --- USB drop-incorrect-bulkout [SRC] hal_com_reg.h:1452 (ENABLE_USB_DROP_INCORRECT_OUT on) ---
DROP_DATA_EN = BIT(9)

# --- Firmware download [SRC] include/rtl8812a_hal.h:65-66, include/hal_com.h:158 ---
FW_START_ADDRESS = 0x1000
MAX_DLFW_PAGE_SIZE = 4096
FW_SIZE_8812 = 0x8000      # max RAM code size
FW_HEADER_SIZE = 32        # skipped before download (FirmwareDownload8812:618-622)

# TX page boundary [SRC] include/rtl8812a_hal.h:203-215
#   BCNQ_PAGE_NUM_8821 = 0x08, WOWLAN_PAGE_NUM_8821 = 0x00 (non-WOWLAN)
#   TX_TOTAL_PAGE_NUMBER_8821 = 0xFF - 0x08 - 0x00 = 0xF7
#   TX_PAGE_BOUNDARY_8821 = TX_TOTAL_PAGE_NUMBER_8821 + 1 = 0xF8
BCNQ_PAGE_NUM_8821 = 0x08
WOWLAN_PAGE_NUM_8821 = 0x00
TX_TOTAL_PAGE_NUMBER_8821 = 0xFF - BCNQ_PAGE_NUM_8821 - WOWLAN_PAGE_NUM_8821
TX_PAGE_BOUNDARY_8821 = TX_TOTAL_PAGE_NUMBER_8821 + 1

# TX descriptor [SRC] include/rtw_xmit.h:215 (default IC branch) — 40 bytes for the
# 8812a/8821a (the 8822b/8821c use 48). TXDESC_OFFSET == TXDESC_SIZE here.
TXDESC_SIZE = 40

# Bit shorthands used by inline pokes.
BIT0, BIT1, BIT2, BIT6, BIT7 = BIT(0), BIT(1), BIT(2), BIT(6), BIT(7)
