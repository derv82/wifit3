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

# --- security (invalidate_cam_all) [SRC] hal_com_reg.h, rtl8188e_hal_init.c ---
REG_CAMCMD = 0x0670          # RWCAM
CAMCMD_CLEAR_ALL = BIT(31) | BIT(30)   # CAM_POLLING | CAM_CLR = 0xC0000000

# --- MISC11 tail [SRC] hal_com_reg.h, usb_halinit.c -----------------------
REG_BAR_MODE_CTRL = 0x04CC
BAR_MODE_CTRL_DISABLE = 0x0201FFFF   # disable BAR (suggested by Scott)
REG_HWSEQ_CTRL = 0x0423              # HW SEQ CTRL: 0xFF = enable HW seq num for all queues

# --- TX descriptor (rtl8188e_fill_fake_txdesc) [SRC] rtl8188e_xmit.h, hal_com.h ---
TXDESC_SIZE = 32              # old IC (8188E)
OFFSET_SZ = 0
OFFSET_SHT = 16
LSG = BIT(26)                # last segment
FSG = BIT(27)                # first segment
OWN = BIT(31)               # descriptor owned by HW (ready to transmit)
BMC = BIT(24)               # broadcast/multicast (group-addressed addr1)
QSEL_SHT = 8
QSLT_MGNT = 0x12            # management queue

# --- BB byte masks (phy_set_bb_reg) [SRC] Hal8188EPhyReg.h ----------------
bMaskByte0 = 0x000000FF
bMaskByte1 = 0x0000FF00
bMaskByte2 = 0x00FF0000
bMaskByte3 = 0xFF000000

# --- TX power (PHY_SetTxPowerLevel8188E / MISC11) [SRC] Hal8188EPhyReg.h ---
# Path-A txagc registers (4 rates packed per 32-bit reg, one byte each).
rTxAGC_A_Rate18_06 = 0x0E00        # OFDM 6/9/12/18 M
rTxAGC_A_Rate54_24 = 0x0E04        # OFDM 24/36/48/54 M
rTxAGC_A_CCK1_Mcs32 = 0x0E08       # CCK 1M (byte1)
rTxAGC_B_CCK11_A_CCK2_11 = 0x086C  # CCK 2/5.5/11 M (path-A, bytes 1/2/3)
rTxAGC_A_Mcs03_Mcs00 = 0x0E10      # HT MCS0-3
rTxAGC_A_Mcs07_Mcs04 = 0x0E14      # HT MCS4-7
MAX_POWER_INDEX = 0x3F             # [SRC] hal_com_phycfg.h
TXPWR_2M_EXTRA_BIAS = -9           # tx_power_extra_bias(MGN_2M) [SRC] rtl8188e_phycfg.c:1393
DEFAULT_INIT_CHANNEL = 6           # pHalData->current_channel default [SRC] usb_halinit.c:1344
# efuse PG TX-power block start (pg_txpwr_saddr) [SRC] rtl8188e_hal_init.c:2547
EEPROM_TX_PWR_INX_88E = 0x10

# --- MISC01 queue/page setup [SRC] hal_com_reg.h, usb_halinit.c -----------
REG_RQPN = 0x0200
REG_RQPN_NPQ = 0x0214
REG_TRXDMA_CTRL = 0x010C
REG_PBP = 0x0104
REG_EFUSE_ACCESS = 0x00CF
EFUSE_ACCESS_OFF = 0x00
# This card has ONE bulk-OUT endpoint (the coverage audit shows only EP 0x02 OUT),
# so OutEpNumber=1: all TX pages are public and every queue maps to the single EP.
RQPN_VALUE = 0x80A70000          # _PUBQ(0xA7=TX_TOTAL_PAGE) | LD_RQPN; NPQ/HPQ/LPQ=0
TRXDMA_QUEUE_MAP_1EP = 0xFAF0    # all six queue maps -> the single (high) EP
RXFF_BOUNDARY = 0x25FF           # MAX_RX_DMA_BUFFER_SIZE_88E - 1
PBP_PAGE_SIZE = 0x11             # _PSRX(PBP_128) | _PSTX(PBP_128) (Tx/Rx 128 B)

# --- MISC02 "open the MAC" inits [SRC] usb_halinit.c, hal_com_reg.h --------
REG_RX_DRVINFO_SZ = 0x060F
DRVINFO_SZ = 0x04
REG_HIMR_88E = 0x00B0
REG_HISR_88E = 0x00B4
REG_HIMRE_88E = 0x00B8
IMR_88E = 0x24000300       # PSTIMEOUT|TBDER|CPWM|CPWM2
IMR_EX_88E = 0x00000F00    # TXERR|RXERR|TXFOVW|RXFOVW
REG_USB_SPECIAL_OPTION = 0xFE55
INT_BULK_SEL = BIT(4)
MASK_NETTYPE = 0x30000
NT_LINK_AP = 0x2
REG_RCR = 0x0608
RCR_STA_INIT = 0x700060CE   # _InitWMACSetting STA RCR (monitor mode overrides this)

# --- monitor-mode entry [SRC] rtl8188e_hal_init.c hw_var_set_opmode/_monitor, hal_com.c ---
MSR = REG_CR + 2            # 0x0102 Media Status (net type per port)
MSR_NETTYPE_MASK = 0x0C     # HW_PORT0 keeps port1 net-type [3:2]; rewrites [1:0]
MSR_NOLINK = 0x00
REG_RXFLTMAP0 = 0x06A0      # mgmt-frame subtype filter
REG_RXFLTMAP1 = 0x06A2      # ctrl-frame subtype filter
REG_RXFLTMAP2 = 0x06A4      # data-frame subtype filter
RXFLTMAP_ACCEPT_ALL = 0xFFFF
# hw_var_set_monitor RCR: RCR_AAP|APM|AM|AB|APWRMGT|ADF|ACF|AMF|APP_PHYST_RXFF|APPFCS.
# No ACRC32/AICV (the 8188e #if 0 — CRC/ICV frames drop in recvbuf2recvframe).
RCR_MONITOR_VALUE = 0x9000382F
REG_MAR = 0x0620
REG_RRSR = 0x0440
RATE_BITMAP_ALL = 0xFFFFF
RATE_RRSR_CCK_ONLY_1M = 0xFFFF1
REG_SPEC_SIFS = 0x0428
REG_RL = 0x042A
SPEC_SIFS_ADAPTIVE = 0x1010   # _SPEC_SIFS_CCK(0x10)|_SPEC_SIFS_OFDM(0x10)
RL_STA = 0x3030               # _LRL(0x30)|_SRL(0x30)
SIFS_VAL = 0x100A
REG_MAC_SPEC_SIFS = 0x063A
REG_SIFS_CTX = 0x0514
REG_SIFS_TRX = 0x0516
REG_EDCA_BE_PARAM = 0x0508
REG_EDCA_BK_PARAM = 0x050C
REG_EDCA_VI_PARAM = 0x0504
REG_EDCA_VO_PARAM = 0x0500
EDCA_BE = 0x005EA42B
EDCA_BK = 0x0000A44F
EDCA_VI = 0x005EA324
EDCA_VO = 0x002FA226
REG_FWHW_TXQ_CTRL = 0x0420
EN_AMPDU_RTY_NEW = BIT(7)
REG_ACKTO = 0x0640
ACKTO_VAL = 0x40
# USB aggregation (resolved for this card's default agg config; wire-confirmed)
TDECTRL_TXAGG = 0x0000A810    # usb_AggSettingTxUpdate result (BLK_DESC_NUM)
RXDMA_AGG_EN = BIT(2)         # in REG_TRXDMA_CTRL low byte
USB_AGG_EN = BIT(3)          # in REG_USB_SPECIAL_OPTION
REG_RXDMA_AGG_PG_TH = 0xFE5C  # USB-mode RX-agg size/timeout (size, +1=timeout)
RXAGG_USB_SIZE = 0x06
RXAGG_USB_TIMEOUT = 0x10
# beacon params (InitBeaconParameters_8188e)
REG_BCN_CTRL = 0x0550
REG_TBTT_PROHIBIT = 0x0540
TBTT_PROHIBIT_SETUP_TIME = 0x04
TBTT_PROHIBIT_HOLD = 0x064
REG_DRVERLYINT = 0x0558
DRIVER_EARLY_INT_TIME_8188E = 0x05
REG_BCNDMATIM = 0x0559
BCN_DMA_ATIME_INT_TIME_8188E = 0x02
REG_BCNTCFG = 0x0510
BCNTCFG_VAL = 0x660F
BCN_CTRL_INIT = 0x1010
# MISC02 tail
REG_TXDMA_OFFSET_CHK = 0x020C
DROP_DATA_EN = BIT(9)
REG_TX_RPT_CTRL = 0x04EC
REG_TX_RPT_TIME = 0x04F0
TX_RPT_TIME_VAL = 0xCDF0
REG_EARLY_MODE_CONTROL = 0x04D0
REG_MACID_NO_LINK_0 = 0x0484
REG_MACID_NO_LINK_1 = 0x0488
REG_PKT_VO_VI_LIFE_TIME = 0x04C0
REG_PKT_BE_BK_LIFE_TIME = 0x04C2
PKT_LIFE_TIME = 0x0400        # CONFIG_TX_MCAST2UNI (256us units)

# --- TX-buffer boundary + LLT init [SRC] hal_com_reg.h, rtl8188e_hal.h ----
REG_BCNQ_BDNY = 0x0424
REG_MGQ_BDNY = 0x0425
REG_WMAC_LBK_BF_HD = 0x045D
REG_TRXFF_BNDY = 0x0114
REG_TDECTRL = 0x0208
REG_LLT_INIT = 0x01E0
# TX_PAGE_BOUNDARY_88E = (0xAF - BCNQ 8 - WOWLAN 0) + 1; last entry = 175 (non-I-cut).
TX_PAGE_BOUNDARY = 0xA8
LAST_ENTRY_OF_TX_PKT_BUFFER = 175
# REG_LLT_INIT fields
_LLT_WRITE_ACCESS = 0x1
_LLT_NO_ACTIVE = 0x0

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
# BB turn-on block (_BBTurnOnBlock) [SRC] Hal8188EPhyReg.h
rFPGA0_RFMOD = 0x0800        # RF mode & CCK TxSC
bCCKEn = BIT(24)
bOFDMEn = BIT(25)

# channel tune (PHY_SwChnl8188E / PHY_SetBWMode8188E) [SRC] hal_com_reg.h, Hal8188EPhyReg.h
REG_BWOPMODE = 0x0603
BW_OPMODE_20MHZ = BIT(2)
REG_RRSR_RSC = 0x0442        # REG_RRSR + 2 (read for the 40 MHz RSC; untouched at 20 MHz)
rFPGA1_RFMOD = 0x0900        # RF mode & OFDM TxSC
bRFMOD = 0x1                 # rFPGA0/1_RFMOD[0]
RF_CHNL_MASK = 0xFFFFFC00    # RfRegChnlVal channel field [9:0] (keep upper bits)
RF_BW_MASK = 0xFFFFF3FF      # RfRegChnlVal bandwidth field [11:10]
RF_BW_20M = BIT(10) | BIT(11)  # 20 MHz

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

# --- RF serial read (phy_RFSerialRead) [SRC] Hal8188EPhyReg.h, hal_phy_reg.h ---
bMaskDWord = 0xFFFFFFFF
RF_CHNLBW = 0x18                 # RF channel & BW switch register (RF reg 0x18)
RF_HSSI_PARA1_A = 0x0820         # rFPGA0_XA_HSSIParameter1 (RfPiEnable BIT8)
RF_HSSI_PARA1_B = 0x0828         # rFPGA0_XB_HSSIParameter1
RF_HSSI_PARA2_B = 0x082C         # rFPGA0_XB_HSSIParameter2
RF_LSSI_READBACK_A = 0x08A0      # rFPGA0_XA_LSSIReadBack (serial-interface readback)
RF_LSSI_READBACK_B = 0x08A4      # rFPGA0_XB_LSSIReadBack
RF_LSSI_READBACK_PI_A = 0x08B8   # TransceiverA_HSPI_Readback (parallel-interface readback)
RF_LSSI_READBACK_PI_B = 0x08BC   # TransceiverB_HSPI_Readback
bLSSIReadAddress = 0x7F800000    # read-offset field in HSSI parameter2 [23:30]
bLSSIReadEdge = 0x80000000       # LSSI "read" edge signal
bLSSIReadBackData = 0x000FFFFF   # 20-bit RF read-back value
RF_PI_ENABLE = BIT(8)            # rFPGA0_X?_HSSIParameter1[8]

# --- IOL (initial offload) engine [SRC] rtl8188e_hal_init.c, rtl8188e_spec.h ---
SW_OFFLOAD_EN = BIT(7)        # REG_SYS_CFG (0xF0[7])
REG_HMEBOX_E0 = 0x0088        # IOL command/status mailbox
CMD_INIT_LLT = BIT(0)
CMD_READ_EFUSE_MAP = BIT(1)
CMD_EFUSE_PATCH = BIT(2)
CMD_IOCONFIG = BIT(3)

# --- efuse probe read (IOL map readback + PG decode) [SRC] rtl8188e_hal_init.c ---
REG_PKT_BUFF_ACCESS_CTRL = 0x0106
TXPKT_BUF_SELECT = 0x69
DISABLE_TRXPKT_BUF_ACCESS = 0x00
REG_PKTBUF_DBG_ADDR = 0x0140       # REG_PKTBUF_DBG_CTRL
REG_TXPKTBUF_DBG = 0x0143          # REG_PKTBUF_DBG_CTRL + 3
REG_PKTBUF_DBG_DATA_L = 0x0144
REG_PKTBUF_DBG_DATA_H = 0x0148
EFUSE_MAP_LEN_88E = 512
EFUSE_REAL_CONTENT_LEN_88E = 256
EFUSE_MAX_SECTION_88E = 64
EFUSE_MAX_WORD_UNIT = 4
# logical-map offsets [SRC] include/hal_pg.h
EEPROM_XTAL_88E = 0xB9             # crystal cap (AFE trim)
EEPROM_MAC_ADDR_88EU = 0xD7        # 6-byte MAC
