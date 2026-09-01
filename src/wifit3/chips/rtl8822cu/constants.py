"""RTL8822CU USB identity and read-only chip registers.

The 2357:0137 adapter reports chip id 0x13 and a 2T2R radio. It is not an
MT7612U and must not use the MT76 firmware or register tables.
"""

USB_IDS_RTL8822CU = (
    # Realtek demoboard defaults, with the vendor's own descriptions
    # [SRC os_dep/linux/usb_intf.c:296-300].
    (0x0BDA, 0xC82C, "RTL8822CU", None, None),   # Default ID for USB multi-function
    (0x0BDA, 0xC82E, "RTL8822CU", None, None),   # Default ID for USB multi-function
    (0x0BDA, 0xC812, "RTL8822CU", None, None),   # Default ID for USB Single-function, WiFi only
    (0x0BDA, 0xD820, "RTL8822CU", None, None),   # 21D USB multi-fuction
    (0x0BDA, 0xD82B, "RTL8822CU", None, None),   # 21D USB Single-fuction, WiFi only
    # (0x13B1, 0x0043, "RTL8822CU", None, None), # Alpha [SRC usb_intf.c:302]: 88x2bu claims it too
    #   (rtl8822bu and rtl8822bu_dkms already declare it as the Linksys WUSB6400M).
    # Stays active: device/manager.py resolve_driver() routes this shared id by USB descriptor
    # (bulk-IN 0x85 means Mediatek), searching THIS package's SUPPORTED_IDS for the RTL answer.
    (0x2357, 0x0137, "RTL8822CU", "FAST / TP-Link", "USB 802.11ac Adapter"),
    (0x2001, 0x3329, "RTL8822CU", "D-Link", "AC13U"),
)

# When None, rfe_type is read from the chip's EFUSE (normal, recommended).
# When set, OVERRIDES the EFUSE rfe_type. ONLY SET FOR A DEVICE WITH AN UNBURNED EFUSE.
RFE_TYPE = None

REG_SYS_CFG1 = 0x00F0
REG_SYS_CFG2 = 0x00FC
REG_SYS_STATUS1 = 0x00F4
REG_WL_BT_PWR_CTRL = 0x0068
REG_SYS_EEPROM_CTRL = 0x000A
REG_ANAPARLDO_POW_MAC = 0x0029
REG_LDO_EFUSE_CTRL = 0x0034
REG_EFUSE_CTRL = 0x0030
REG_MACID = 0x0610
REG_RCR = 0x0608
REG_USB_HRPWM = 0xFE58
REG_USB_USBSTAT = 0xFE11

# init_usb_cfg_88xx fields [SRC halmac_bit2.h:26729,29744-29778]
BIT_DROP_DATA_EN = 1 << 9       # REG_TXDMA_OFFSET_CHK
BIT_DMA_MODE = 1 << 1           # REG_RXDMA_MODE
BIT_SHIFT_BURST_SIZE = 4
BIT_SHIFT_BURST_CNT = 2
USB_BURST_SIZE_3_0 = 0x0
USB_BURST_SIZE_2_0_HS = 0x1
USB_BURST_SIZE_2_0_FS = 0x2
REG_C2HEVT_MSG_NORMAL = 0x01A0
# enum _C2H_EVT [SRC hal/hal_com_c2h.h:51-81]
C2H_DBG = 0x00
C2H_MAC_HIDDEN_RPT = 0x19
C2H_DEFEATURE_RSVD = 0xFD

# _send_general_info inputs: rf_type is the halmac RF enum, tx/rx antenna are the enum bb_path
# bitmap. PackageType is NOT from EFUSE (its 8822c parse is an empty stub); it comes from the C2H
# MAC hidden report (hal_data->PackageType = package_type [SRC hal/hal_com.c:1482]).
# enum halmac_rf_type [SRC hal/halmac/halmac_type.h:1085,1087,1089,1094]
HALMAC_RF_1T2R = 0x00
HALMAC_RF_2T2R = 0x02
HALMAC_RF_1T1R = 0x04
HALMAC_RF_MAX_TYPE = 0x0F
# enum bb_path [SRC include/cmn_info/rtw_sta_info.h:99-106]
BB_PATH_NON = 0x0
BB_PATH_A = 0x1
BB_PATH_B = 0x2
BB_PATH_AB = 0x3
BB_PATH_AUTO = 0xFF

REG_PMC_DBG_CTRL1 = 0x00A8
REG_TXPKT_EMPTY = 0x041A
REG_LTECOEX_CTRL = 0x1700       # +3 = ready poll (BIT5); low16 = indirect offset
REG_LTECOEX_WDATA = 0x1704
REG_LTECOEX_RDATA = 0x1708
CHIP_ID_RTL8822CU = 0x13

BIT_RF_TYPE_ID = 1 << 27
CHIP_CUT_SHIFT = 12
CHIP_CUT_MASK = 0xF
ROM_VERSION_SHIFT = 28
ROM_VERSION_MASK = 0xF

EFUSE_SIZE = 512
EEPROM_SIZE = 768
EFUSE_PROTECTED_SIZE = 124
EEPROM_ID = 0x8129
EEPROM_MAC_ADDR = 0x157
EEPROM_RFE_OPTION = 0x0CA
EEPROM_XTAL_B9_8822C = 0xB9                  # [SRC include/hal_pg.h:591]
EEPROM_DEFAULT_CRYSTAL_CAP_B9 = 0x3F         # [SRC include/hal_pg.h:903]
EEPROM_XTAL_110_8822C = 0x110                # [SRC include/hal_pg.h:616,617]
EEPROM_XTAL_111_8822C = 0x111
EEPROM_DEFAULT_CRYSTAL_CAP_110_8822C = 0x40  # [SRC include/hal_pg.h:904]
XCAP_VALUE_MASK = 0x7F                       # GET_XCAP_VALUE_*_8822C [SRC rtl8822c_ops.c:364-366]
BIT_AUTOLOAD_SUS = 1 << 5
BIT_EF_READY = 1 << 31
EFUSE_ADDR_MASK = 0x3FF

USB_INTERFACE_CLASS_VENDOR = 0xFF
USB_ENDPOINT_IN = 0x80
USB_ENDPOINT_DIR_MASK = 0x80
USB_ENDPOINT_TYPE_MASK = 0x03
USB_ENDPOINT_TYPE_BULK = 0x02

# rtl8822c_init_misc [SRC halmac_reg_8822c.h:362,629,669,692; halmac_bit_8822c.h:10096,15961,16864]
REG_CAMCMD = 0x0670
REG_RXFLTMAP1 = 0x06A2
REG_FWHW_TXQ_CTRL = 0x0420
BIT_SECCAM_POLLING = 1 << 31
BIT_SECCAM_CLR = 1 << 30
BIT_ACRC32 = 1 << 8
BIT_AICV = 1 << 9
BIT_TCPOFLD_EN = 1 << 25
BIT_MAC_SEC_EN = 1 << 9
BIT_EN_QUEUE_RPT = 1 << 12      # BIT_EN_QUEUE_RPT_8822C(BIT(4))

# --- TX power: hal_spec fields [SRC hal/rtl8822c/rtl8822c_halinit.c:43-70] ---
HAL_SPEC_RFPATH_NUM_2G = 2
HAL_SPEC_RFPATH_NUM_5G = 2
HAL_SPEC_RF_REG_PATH_NUM = 2
HAL_SPEC_RF_REG_TRX_PATH_BMP = 0x33
HAL_SPEC_MAX_TX_CNT = 2
HAL_SPEC_TX_NSS_NUM = 2
HAL_SPEC_TXGI_MAX = 127
HAL_SPEC_TXGI_PDBM = 4                       # txgi units per dB, so one unit is 0.25 dB
HAL_SPEC_PG_TXPWR_SADDR = 0x10
HAL_SPEC_PG_TXGI_DIFF_FACTOR = 2

# PG map geometry and validity sentinels [SRC hal/hal_com_phycfg.c:20-33]
PG_TXPWR_1PATH_BYTE_NUM_2G = 18
PG_TXPWR_BASE_BYTE_NUM_2G = 11
PG_TXPWR_1PATH_BYTE_NUM_5G = 24
PG_TXPWR_BASE_BYTE_NUM_5G = 14
PG_TXPWR_INVALID_BASE = 255
PG_TXPWR_INVALID_DIFF = 8

# enum txpwr_pg_mode [SRC include/hal_com_phycfg.h:36-40]
TXPWR_PG_WITH_PWR_IDX = 0
TXPWR_PG_WITH_TSSI_OFFSET = 1
TXPWR_PG_UNKNOWN = 2

# dm->dis_dpd_rate, the two values config_phydm_parameter_init_8822c can write
# [SRC hal/phydm/rtl8822c/phydm_hal_api8822c.c:2222-2225]
DIS_DPD_RATE_ALL = 0x3FF
DIS_DPD_RATE_NONE = 0x000

# EFUSE logical offsets [SRC include/hal_pg.h:603,609,610,945]
EEPROM_RF_BOARD_OPTION_8822C = 0xC1
EEPROM_TX_PWR_CALIBRATE_RATE_8822C = 0xC8
EEPROM_RF_ANTENNA_OPT_8822C = 0xC9
EEPROM_DEFAULT_BOARD_OPTION = 0x00
BIT_BOARD_OPTION_1TX = 1 << 2                # 0xC1[2] limits the board to 1 Tx/stream [SRC rtl8822c_ops.c:305]
EEPROM_BOARD_OPTION_BT_COMBO = 0x01          # the 0xC1[7:5] value meaning combo [SRC rtl8822c_ops.c:324-325]
# The five 0xC9 values Hal_EfuseParsePathSelection accepts [SRC rtl8822c_ops.c:528-534]
EEPROM_TRX_PATH_BMP_VALID = (0x33, 0x13, 0x23, 0x11, 0x22)

# c2h_mac_hidden_rpt_hdl's 8822C downgrades [SRC hal/hal_com.c:1472-1480]
MAC_HIDDEN_RPT_1ANT_TRX_PATH_BMP = 0x22      # 1T1R path B
MAC_HIDDEN_RPT_HW_STYPE_1TX = 0xE
RF_PATH_MAX = 4                              # [SRC include/hal_pg.h:1007-1008]
