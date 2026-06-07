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

REG_HMETFR = 0x01CC          # H2C trigger, written by InitializeFirmwareVars

# --- MAC config [SRC] hal_com_reg.h, Hal8188EPhyCfg.h --------------------
REG_MAX_AGGR_NUM = 0x04CA

# --- BB config [SRC] hal_com_reg.h, rtl8188e_phycfg.c, hal_com.c ----------
REG_RF_CTRL = 0x001F
RF_EN = BIT(0)
RF_RSTB = BIT(1)
RF_SDMRSTB = BIT(2)
RF_CTRL_INIT = RF_EN | RF_RSTB | RF_SDMRSTB           # 0x07
# REG_SYS_FUNC_EN BB-enable: BIT13 (FEN_DIO_RF) | BIT0 | BIT1
SYS_FUNC_BB_ENABLE = BIT(13) | BIT(0) | BIT(1)        # 0x2003
FEN_BBRSTB = BIT(0)
FEN_BB_GLB_RSTn = BIT(1)
FEN_USBA = BIT(2)
FEN_USBD = BIT(4)
FEN_BB_USB = FEN_USBA | FEN_USBD | FEN_BB_GLB_RSTn | FEN_BBRSTB  # 0x17
REG_AFE_XTAL_CTRL = 0x0024
XTAL_CAP_MASK = 0x007FF800   # REG_AFE_XTAL_CTRL[22:11] = cap | cap<<6
DEFAULT_CRYSTAL_CAP = 0x20   # EEPROM_Default_CrystalCap (efuse 0xB9 on this card)
# PHY_REG delay pseudo-addresses (settling, no register write)
BB_DELAY_ADDRS = range(0xF9, 0xFF)

# --- RF config [SRC] Hal8188EPhyReg.h, rtl8188e_rf6052.c, phy_RFWrite -----
# Path-A BB register-definition offsets (phy_InitBBRFRegisterDefinition).
RF_INTFS_A = 0x0870          # rFPGA0_XAB_RFInterfaceSW (RFENV control)
RF_INTFO_A = 0x0860          # rFPGA0_XA_RFInterfaceOE  (RFENV output)
RF_INTFE_A = 0x0860          # rFPGA0_XA_RFInterfaceOE  (RFENV enable)
RF_HSSI_PARA2_A = 0x0824     # rFPGA0_XA_HSSIParameter2 (3-wire addr/data len)
RF_LSSI_WRITE_A = 0x0840     # rFPGA0_XA_LSSIParameter  (3-wire RF write)
bRFSI_RFENV = 0x10
b3WireAddressLength = 0x400
b3WireDataLength = 0x800
RFREGOFFSETMASK = 0xFFFFF
# RF data-row delay pseudo-addresses [SRC] odm_config_rf_reg_8188e
RF_DELAY_ADDRS = (0xFFE, 0xFD, 0xFC, 0xFB, 0xFA, 0xF9)
