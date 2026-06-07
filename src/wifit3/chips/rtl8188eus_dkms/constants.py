"""RTL8188EUS (vendor DKMS) register addresses, bit masks, and USB IDs.

Constants are transcribed verbatim from the ``realtek-rtl8188eus`` 5.3.9 source
(``include/hal_com_reg.h``, ``include/rtl8188e_hal.h``, ``include/hal_com.h``),
never typed from memory. Grouped by bring-up stage; each block grows as a
milestone lands.
"""
from __future__ import annotations


def BIT(n: int) -> int:
    return 1 << n


# --- USB identity ----------------------------------------------------------
VID = 0x2357
PID = 0x010C  # TP-Link TL-WN722N v2/v3 [Realtek RTL8188EUS]

# Realtek vendor control request [SRC] include/usb_ops.h:21
REALTEK_VENDOR_REQUEST = 0x05
REQ_TYPE_WRITE = 0x40  # host->device, vendor, device
REQ_TYPE_READ = 0xC0   # device->host, vendor, device

# --- chip version (REG_SYS_CFG) [SRC] hal_com_reg.h ------------------------
REG_SYS_CFG = 0x00F0
VENDOR_ID = BIT(19)
RTL_ID = BIT(23)            # 1: Test chip, 0: MP chip
CHIP_VER_RTL_MASK = 0xF000  # cut version, bits 12..15
CHIP_VER_RTL_SHIFT = 12

# --- power-on / MAC enable [SRC] hal_com_reg.h ----------------------------
REG_SYS_FUNC_EN = 0x0002
REG_RSV_CTRL = 0x001C
REG_CR = 0x0100
# REG_CR enable bits set by _InitPowerOn_8188EU
HCI_TXDMA_EN = BIT(0)
HCI_RXDMA_EN = BIT(1)
TXDMA_EN = BIT(2)
RXDMA_EN = BIT(3)
PROTOCOL_EN = BIT(4)
SCHEDULE_EN = BIT(5)
MACTXEN = BIT(6)
MACRXEN = BIT(7)
ENSEC = BIT(9)
CALTMR_EN = BIT(10)
CR_ENABLE_BITS = (HCI_TXDMA_EN | HCI_RXDMA_EN | TXDMA_EN | RXDMA_EN
                  | PROTOCOL_EN | SCHEDULE_EN | ENSEC | CALTMR_EN)  # 0x063F

# --- firmware download [SRC] rtl8188e_hal.h, hal_com.h, hal_com_reg.h ------
REG_MCUFWDL = 0x0080
FW_8188E_START_ADDRESS = 0x1000
MAX_DLFW_PAGE_SIZE = 4096    # 4 KB per FW page
MAX_REG_BLOCK_SIZE = 196     # MAX_REG_BOLCK_SIZE: 196-byte phase-1 control writes
FW_HEADER_SIZE = 32          # RT_8188E_FIRMWARE_HDR, stripped before upload
FW_SIGNATURE_MASK = 0xFFF0   # IS_FW_HEADER_EXIST_88E: (sig & 0xFFF0) == 0x88E0
FW_SIGNATURE_88E = 0x88E0

# REG_MCUFWDL bit fields [SRC] hal_com_reg.h
MCUFWDL_RDY = BIT(1)
FWDL_ChkSum_rpt = BIT(2)
WINTINI_RDY = BIT(6)
RAM_DL_SEL = BIT(7)
