"""RTL8922AU (rtw89 8922A over USB) register-access constants, ported from rtw89-7.2.
Values are pasted verbatim from the vendor source; each carries its file:line.
"""

# USB vendor control transfer (rtw89_usb_vendorreq). [SRC] usb.h:10-12
RTW89_USB_VENQT = 0x05           # bRequest for register access
RTW89_USB_VENQT_READ = 0xc0      # bmRequestType: vendor + device-to-host (IN)
RTW89_USB_VENQT_WRITE = 0x40     # bmRequestType: vendor + host-to-device (OUT)

# A register address rides the setup packet split as:
#   wValue = addr & 0xFFFF, wIndex = (addr >> 16) & 0xFF   [SRC] usb.c:31-32
_ADDR_VALUE_MASK = 0xFFFF
_ADDR_INDEX_SHIFT = 16
_ADDR_INDEX_MASK = 0xFF

RTW89_USB_VENDORREQ_ATTEMPTS = 10    # retry budget per op. [SRC] usb.c:34
RTW89_USB_VENDORREQ_TIMEOUT_MS = 500  # [SRC] usb.c:47
RTW89_USB_MAX_IO_ERROR = 4           # continual_io_error cap. [SRC] usb.c:70

# CMAC register window. A read here can return R32_DEAD until its clock is on, so
# rtw89_usb_read_cmac re-enables the clock and re-reads. [SRC] mac.h:586-589, reg.h.
R_AX_CMAC_REG_START = 0xC000     # [SRC] reg.h:2141
R_AX_CMAC_REG_END = 0xFFFF       # [SRC] reg.h:3769
RTW89_R32_DEAD = 0xDEADBEEF      # [SRC] core.h:208
MAC_REG_POOL_COUNT = 10          # read_cmac retry budget. [SRC] mac.h:586
R_AX_CK_EN = 0xC004              # [SRC] reg.h:2157
B_AX_CMAC_ALLCKEN = 0xFFFFFFFF   # GENMASK(31, 0). [SRC] reg.h:2159

# Chip power-on / info registers the USB probe touches first. [SRC] reg.h.
R_BE_PAD_CTRL2 = 0x00C4          # USB pad control (rtw89_usb_switch_mode_be). reg.h:4251
R_AX_SYS_CFG1 = 0x00F0           # reg.h:195
R_BE_SYS_CHIPINFO = 0x00FC       # HW id / chip info. reg.h:4314
R_BE_WLAN_XTAL_SI_CTRL = 0x0270  # crystal SI control. reg.h:4670

# rtw89_usb_switch_mode_be: USB2/3 mode switch on R_BE_PAD_CTRL2. [SRC] usb.c:1143-1170, reg.h.
_LIBUSB_SPEED_SUPER = 4          # libusb/pyusb Device.speed for SuperSpeed (USB 3)
USB_SWITCH_DELAY = 0xF           # reg.h:178
B_BE_MATCH_CNT = 0xFF00          # GENMASK(15, 8). reg.h:4263
B_BE_RSM_EN_V1 = 1 << 16         # reg.h:4262
B_BE_NO_PDN_CHIPOFF_V1 = 1 << 17  # reg.h:4261
B_BE_USB3_FORCE = 1 << 21        # reg.h:4260
B_BE_USB2_FORCE = 1 << 22        # reg.h:4259
B_BE_FORCE_U3_CK = 1 << 23       # reg.h:4258
B_BE_FORCE_U2_CK = 1 << 24       # reg.h:4257
B_BE_FORCE_CLK_U2 = 1 << 25      # reg.h:4256
B_BE_USB_AUTO_INSTALL_MASK = 1 << 28  # reg.h:4255
B_BE_USB3_LANE_MODE = 1 << 29    # reg.h:4254
B_BE_USB3_GEN_MODE = 1 << 30     # reg.h:4253
B_BE_USB23_SW_MODE = 1 << 31     # reg.h:4252

# rtw89_read_chip_ver: chip version / hw id. [SRC] core.c:7091, reg.h/mac.h.
B_AX_CHIP_VER_MASK = 0xF000      # GENMASK(15, 12). reg.h:196
B_BE_HW_ID_MASK = 0xFF           # GENMASK(7, 0). reg.h:4319
CHIP_CAV = 0                     # first enum rtw89_cv. core.h:365

# XTAL_SI indirect write extras (rtw89_mac_write_xtal_si). [SRC] mac_be.c:413-441, reg.h/mac.h.
# The BE field positions match the AX ones, so the AX masks above cover the shared fields.
B_AX_WL_XTAL_SI_BITMASK_MASK = 0x00FF0000  # GENMASK(23, 16). reg.h:276
XTAL_SI_NORMAL_WRITE = 0x00      # reg.h:274
XTAL_SI_PLL = 0xE0               # mac.h:1694
XTAL_SI_PLL_1 = 0xE1             # mac.h:1695
XTAL_SI_ANAPAR_WL = 0x90         # mac.h:1681
XTAL_SI_WL_RFC_S0 = 0x80         # mac.h:1675
XTAL_SI_WL_RFC_S1 = 0x81         # mac.h:1678
XTAL_SI_XREF_RF1 = 0x2D          # mac.h:1664
XTAL_SI_XREF_RF2 = 0x2E          # mac.h:1665
XTAL_SI_SRAM_CTRL = 0xA1         # mac.h:1690
XTAL_SI_SRAM_DIS = 1 << 1        # BIT(1). mac.h:1691

# MAC power-on: boot-mode handoff (rtw89_mac_power_switch_boot_mode). [SRC] mac.c:1480-1495, reg.h.
R_AX_GPIO_MUXCFG = 0x0040        # reg.h:81
B_AX_BOOT_MODE = 1 << 19         # reg.h:82
R_AX_SYS_PW_CTRL = 0x0004        # reg.h:21
B_AX_APFN_ONMAC = 1 << 8         # reg.h:35
R_AX_SYS_STATUS1 = 0x00F4        # reg.h:198
B_AX_AUTO_WLPON = 1 << 10        # reg.h:200
R_AX_RSV_CTRL = 0x001C           # reg.h:47
B_AX_R_DIS_PRST = 1 << 6         # reg.h:48

# reset_pwr_state (rtw89_mac_reset_pwr_state_be). [SRC] mac_be.c:474-601, reg.h.
R_BE_SYSON_FSM_MON = 0x00A0      # reg.h:4215
WLAN_FSM_MASK = 0xFFFFFF         # reg.h:4226
WLAN_FSM_SET = 0x4000000         # reg.h:4227
WLAN_FSM_STATE_MASK = 0x1FF      # reg.h:4228
WLAN_FSM_IDLE = 0                # reg.h:4229
R_BE_IC_PWR_STATE = 0x03F0       # reg.h:4688
B_BE_WLMAC_PWR_STE_MASK = 0x300  # GENMASK(9, 8). reg.h:4691
MAC_AX_MAC_OFF = 0               # reg.h:330
MAC_AX_MAC_ON = 1                # mac.h
MAC_AX_MAC_LPS = 2               # mac.h
R_BE_HCI_OPT_CTRL = 0x0074       # reg.h:4082
B_BE_HAXIDMA_IO_EN = 1 << 24     # reg.h:4087
B_BE_HAXIDMA_IO_ST = 1 << 27     # reg.h:4085
B_BE_HAXIDMA_BACKUP_RESTORE_ST = 1 << 26  # reg.h:4086
B_BE_HCI_WLAN_IO_EN = 1 << 28    # reg.h
B_BE_HCI_WLAN_IO_ST = 1 << 31    # reg.h
R_BE_SYS_PW_CTRL = 0x0004        # reg.h
B_BE_EN_WLON = 1 << 16           # reg.h
B_BE_APFM_SWLPS = 1 << 10        # reg.h
B_BE_APFM_OFFMAC = 1 << 9        # reg.h
B_BE_SOP_EASWR = 1 << 30         # reg.h:3830
B_BE_XTAL_OFF_A_DIE = 1 << 22    # reg.h:3838
R_BE_WLLPS_CTRL = 0x0090         # reg.h
B_BE_FORCE_LEAVE_LPS = 1 << 3    # reg.h

# 8922A power-on sequence (rtw8922a_pwr_on_func). [SRC] rtw8922a.c:475-634, reg.h.
B_BE_AFSM_WLSUS_EN = 1 << 11
B_BE_AFSM_PCIE_SUS_EN = 1 << 12
B_BE_DIS_WLBT_PDNSUSEN_SOPC = 1 << 18
B_BE_DIS_WLBT_LPSEN_LOPC = 1 << 1
B_BE_APDM_HPDN = 1 << 15
B_BE_RDY_SYSPWR = 1 << 17
R_BE_WLRESUME_CTRL = 0x0094
B_BE_LPSROP_CMAC0 = 1 << 12
B_BE_LPSROP_CMAC1 = 1 << 13
B_BE_APFN_ONMAC = 1 << 8
R_BE_AFE_ON_CTRL1 = 0x0244
B_BE_REG_CK_MON_CK960M_EN = 1 << 28
R_BE_ANAPAR_POW_MAC = 0x0016
B_BE_POW_PC_LDO_PORT0 = 1 << 2
B_BE_POW_PC_LDO_PORT1 = 1 << 3
R_BE_FEN_RST_ENABLE = 0x0084
B_BE_R_SYM_ISO_ADDA_P02PP = 1 << 20
B_BE_R_SYM_ISO_ADDA_P12PP = 1 << 21
B_BE_FEN_BB_IP_RSTN = 1 << 1
B_BE_FEN_BBPLAT_RSTB = 1 << 0
R_BE_PLATFORM_ENABLE = 0x0088
B_BE_PLATFORM_EN = 1 << 0
R_BE_SYS_ADIE_PAD_PWR_CTRL = 0x0018
B_BE_SYM_PADPDN_WL_RFC1_1P3 = 1 << 6
B_BE_SYM_PADPDN_WL_RFC0_1P3 = 1 << 5
R_BE_PMC_DBG_CTRL2 = 0x00CC
B_BE_SYSON_DIS_PMCR_BE_WRMSK = 1 << 2
R_BE_SYS_ISO_CTRL = 0x0000
B_BE_ISO_EB2CORE = 1 << 8
B_BE_PWC_EV2EF_B = 1 << 15
B_BE_PWC_EV2EF_S = 1 << 14
R_BE_DMAC_FUNC_EN = 0x8400
B_BE_MAC_FUNC_EN = 1 << 30
B_BE_DMAC_FUNC_EN = 1 << 29
B_BE_MPDU_PROC_EN = 1 << 28
B_BE_WD_RLS_EN = 1 << 27
B_BE_DLE_WDE_EN = 1 << 26
B_BE_TXPKT_CTRL_EN = 1 << 25
B_BE_STA_SCH_EN = 1 << 24
B_BE_DLE_PLE_EN = 1 << 23
B_BE_PKT_BUF_EN = 1 << 22
B_BE_DMAC_TBL_EN = 1 << 21
B_BE_PKT_IN_EN = 1 << 20
B_BE_DLE_CPUIO_EN = 1 << 19
B_BE_DISPATCHER_EN = 1 << 18
B_BE_BBRPT_EN = 1 << 17
B_BE_MAC_SEC_EN = 1 << 16
B_BE_H_AXIDMA_EN = 1 << 14
B_BE_DMAC_MLO_EN = 1 << 11
B_BE_PLRLS_EN = 1 << 10
B_BE_P_AXIDMA_EN = 1 << 9
B_BE_DLE_DATACPUIO_EN = 1 << 8
B_BE_LTR_CTL_EN = 1 << 7
R_BE_CMAC_SHARE_FUNC_EN = 0xE000
B_BE_CMAC_SHARE_EN = 1 << 30
B_BE_RESPBA_EN = 1 << 2
B_BE_ADDRSRCH_EN = 1 << 1
B_BE_BTCOEX_EN = 1 << 0
R_BE_CMAC_FUNC_EN = 0x10000
B_BE_CMAC_EN = 1 << 30
B_BE_CMAC_TXEN = 1 << 29
B_BE_CMAC_RXEN = 1 << 28
B_BE_SIGB_EN = 1 << 6
B_BE_PHYINTF_EN = 1 << 5
B_BE_CMAC_DMA_EN = 1 << 4
B_BE_PTCLTOP_EN = 1 << 3
B_BE_SCHEDULER_EN = 1 << 2
B_BE_TMAC_EN = 1 << 1
B_BE_RMAC_EN = 1 << 0
B_BE_TXTIME_EN = 1 << 8
B_BE_RESP_PKTCTL_EN = 1 << 7

# read_poll_timeout budget for the power-on polls (kernel timeout_us/sleep_us ~= 3000). [SRC] mac_be.c.
PWR_POLL_ATTEMPTS = 3000

# Post-pwr-on tail of power_switch(on=True): efuse reads, scoreboard notify. [SRC] mac.c:1557-1568.
# Physical efuse dump + state convert. [SRC] efuse_be.c, reg.h.
R_BE_WL_BT_PWR_CTRL = 0x0068     # reg.h:4032
B_BE_BT_DISN_EN = 1 << 16        # reg.h
B_BE_WHOLE_SYS_PWR_STE_MASK = 0x03FF0000  # GENMASK(25, 16). reg.h
MAC_AX_SYS_ACT = 0x220           # reg.h:4690
R_BE_EFUSE_CTRL = 0x0030         # reg.h:3996
B_BE_EF_ADDR_MASK = 0xFFFF       # GENMASK(15, 0). reg.h
B_BE_EF_RDY = 1 << 29            # reg.h:3998
R_BE_EFUSE_CTRL_1_V1 = 0x0034    # reg.h
R_BE_EFUSE_CTRL_2_V1 = 0x00A4    # reg.h
B_BE_EF_BURST = 1 << 19          # reg.h
# efuse chip-version read (rtw89_efuse_read_ecv_be). [SRC] efuse_be.c:516-540, efuse.h.
EF_FV_OFSET_BE_V1 = 0x17CA       # efuse.h:15
EF_CV_MASK = 0xF0                # GENMASK(7, 4). efuse.h
EF_CV_INV = 15                   # efuse.h
# efuse secure-boot selector (rtw89_efuse_read_fw_secure_be). [SRC] efuse_be.c:468-514, efuse.h.
EFUSE_SEC_BE_START = 0x1580      # efuse.h
EFUSE_SEC_BE_SIZE = 4            # efuse.h
EFUSE_SB_CRYP_SEL_ADDR = 0x1582  # efuse.h
EFUSE_SB_CRYP_SEL_DEFAULT = 0xFFFF  # efuse.h
# BT-coex scoreboard notify (rtw89_mac_update_scoreboard). [SRC] mac.c:1506-1519, rtw8922a.c:3300.
R_BE_SCOREBOARD = 0x00AC         # reg.h
MAC_AX_NOTIFY_TP_MAJOR = 0x81    # mac.h
MAC_AX_NOTIFY_PWR_MAJOR = 0x80   # reg.h:162

# RF-kill GPIO9 polling (rtw89_rfkill_polling_init). [SRC] rtw8922a.c:330-337, reg.h:4023-4030,4667-4668.
R_BE_GPIO8_15_FUNC_SEL = 0x02D4          # reg.h:4667
B_BE_PINMUX_GPIO9_FUNC_SEL_MASK = 0xF0   # GENMASK(7, 4). reg.h:4668
RFKILL_PINMUX_GPIO9_DATA = 0xF           # rtw8922a.c:333
R_BE_GPIO_EXT_CTRL = 0x0060              # reg.h:4023
B_BE_GPIO_MOD_9 = 1 << 25                # reg.h:4025
B_BE_GPIO_IO_SEL_9 = 1 << 17            # reg.h:4027
B_BE_GPIO_IN_9 = 1 << 1                  # reg.h:4030

# DMAC pre-init (rtw89_mac_partial_init -> dmac_pre_init). [SRC] mac.c:4258-4279, mac_be.c:369-410.
R_BE_HCI_FUNC_EN = 0x7880        # reg.h:4861; 8922a hci_func_en_addr (rtw8922a.c:3274)
B_BE_HCI_TXDMA_EN = 1 << 0       # reg.h; same bit as B_AX_HCI_TXDMA_EN
B_BE_HCI_RXDMA_EN = 1 << 1       # reg.h; same bit as B_AX_HCI_RXDMA_EN
R_BE_HAXI_INIT_CFG1 = 0xB000     # reg.h:6283
B_BE_DMA_MODE_MASK = 0x0700      # GENMASK(10, 8). reg.h
S_BE_DMA_MOD_USB = 0x4           # reg.h
B_BE_STOP_AXI_MST = 1 << 7       # reg.h
B_BE_TXDMA_EN = 1 << 4           # reg.h
B_BE_RXDMA_EN = 1 << 5           # reg.h
R_BE_HAXI_DMA_STOP1 = 0xB010     # reg.h:6305
B_BE_TX_STOP1_MASK = 0x7FFF      # B_BE_STOP_CH0..CH14. reg.h:6307-6321
R_BE_DMAC_TABLE_CTRL = 0x8420    # reg.h:4945
B_BE_DMAC_ADDR_MODE = 1 << 12    # reg.h

# DLE init (rtw89_mac_dle_init, QTA_DLFW mode). [SRC] mac.c:2274-2343, mac_be.c:216-312, reg.h.
R_BE_DMAC_CLK_EN = 0x8404        # reg.h
B_BE_DLE_WDE_CLK_EN = 1 << 26    # reg.h
B_BE_DLE_PLE_CLK_EN = 1 << 23    # reg.h
R_BE_WDE_PKTBUF_CFG = 0x8C08     # reg.h
R_BE_PLE_PKTBUF_CFG = 0x9008     # reg.h
B_BE_WDE_PAGE_SEL_MASK = 0x3            # GENMASK(1, 0). reg.h
B_BE_WDE_START_BOUND_MASK = 0x7F00      # GENMASK(14, 8). reg.h
B_BE_WDE_FREE_PAGE_NUM_MASK = 0x1FFF0000  # GENMASK(28, 16). reg.h
B_BE_PLE_PAGE_SEL_MASK = 0x3            # GENMASK(1, 0). reg.h
B_BE_PLE_START_BOUND_MASK = 0x7F00      # GENMASK(14, 8). reg.h
B_BE_PLE_FREE_PAGE_NUM_MASK = 0x1FFF0000  # GENMASK(28, 16). reg.h
S_AX_WDE_PAGE_SEL_64 = 0         # mac.h:605
S_AX_PLE_PAGE_SEL_128 = 1        # mac.h:614
DLE_BOUND_UNIT = 8 * 1024        # mac.h
R_BE_WDE_QTA0_CFG = 0x8C40       # reg.h:5560; QTAn_CFG = QTA0 + n*4
R_BE_PLE_QTA0_CFG = 0x9040       # reg.h:5681
B_BE_QTA_MIN_SIZE_MASK = 0xFFF          # GENMASK(11, 0), uniform across QTAn. reg.h
B_BE_QTA_MAX_SIZE_MASK = 0x0FFF0000     # GENMASK(27, 16), uniform across QTAn. reg.h
R_AX_WDE_INI_STATUS = 0x8D00     # reg.h
R_AX_PLE_INI_STATUS = 0x9100     # reg.h
WDE_MGN_INI_RDY = 0x3            # B_AX_WDE_Q_MGN_INI_RDY | B_AX_WDE_BUF_MGN_INI_RDY. reg.h:1387-1388
PLE_MGN_INI_RDY = 0x3            # B_AX_PLE_Q_MGN_INI_RDY | B_AX_PLE_BUF_MGN_INI_RDY. reg.h:1595-1596
# 8922A dle_mem[QTA_DLFW] config. [SRC] rtw8922a.c:191-195, mac.c:1729/1762/1797/1833.
# rtw89_dle_size = (pge_size, lnk_pge_num, unlnk_pge_num, srt_ofst).
WDE_SIZE3_LNK_PGE_NUM = 0        # wde_size3_v1. mac.c:1729
WDE_SIZE3_SRT_OFST = 0
PLE_SIZE3_LNK_PGE_NUM = 2928     # ple_size3_v1. mac.c:1762
PLE_SIZE3_SRT_OFST = 212992
# rtw89_wde_quota = (hif, wcpu, pkt_in, cpu_io); wde_qt4 is all zero. mac.c:1797.
# rtw89_ple_quota fields; ple_qt9 (min == max for DLFW). mac.c:1833.
PLE_QT9 = (0, 0, 32, 256, 0, 0, 0, 0, 0, 0, 1, 0, 0)   # ..h2d; 8922A stops before snrpt(13)
# ext_wde_min_qt_wcpu = SCC wde_qt0_v1.wcpu (qta_mode defaults to SCC). mac.c:1792, core.c:6990.
EXT_WDE_MIN_QT_WCPU = 6

# HCI flow control init (rtw89_mac_hfc_init, reset+h2c-only path). [SRC] mac.c:1194-1246, mac_be.c.
R_BE_HCI_FC_CTRL = 0xB700        # reg.h
B_BE_HCI_FC_EN = 1 << 0          # reg.h
B_BE_HCI_FC_CH12_EN = 1 << 3     # reg.h
R_BE_CH_PAGE_CTRL = 0xB704       # reg.h
B_BE_PREC_PAGE_CH12_V1_MASK = 0x003F0000  # GENMASK(21, 16). reg.h:6385
# hfc_reset_param takes dle_info.qta_mode, which dle_init's ext_mode lookup left at SCC, so the
# h2c precedence is the USB SCC config hfc_prec_cfg_c5.h2c_prec, not DLFW's c2. [SRC] mac.c:1906,
# rtw8922a.c:111-112 (usb2 SCC), mac.c:1720 (hfc_prec_cfg_c5).
HFC_H2C_PREC = 32

# Firmware-download preconfig (rtw89_mac_fwdl_preconfig_be). [SRC] mac_be.c:625-629, reg.h.
R_BE_FW_AUTO_CAL_DELAY = 0x0188          # reg.h
B_BE_WCPU_FW_DELAY_COUNT_VALID = 1 << 15  # reg.h
B_BE_WCPU_FW_DELAY_COUNT_MASK = 0x7FFF    # GENMASK(14, 0). reg.h

# WCPU disable + firmware-download enable (rtw89_mac_disable_cpu_be, fwdl_enable_wcpu_be,
# set_cpu_en, wcpu_on). [SRC] mac_be.c:603-707, reg.h.
B_BE_WCPU_EN = 1 << 1            # reg.h
B_BE_HOLD_AFTER_RESET = 1 << 11  # reg.h
R_BE_WCPU_FW_CTRL = 0x01E0       # reg.h
B_BE_RUN_ENV_MASK = 0xC0000000   # GENMASK(31, 30). reg.h
B_BE_WLANCPU_FWDL_EN = 1 << 9    # reg.h
B_BE_BBMCU0_FWDL_EN = 1 << 11    # reg.h
B_BE_WDT_PLT_RST_EN = 1 << 17    # reg.h
B_BE_WCPU_ROM_CUT_GET = 1 << 8   # reg.h
R_BE_DCPU_PLATFORM_ENABLE = 0x0888  # reg.h
B_BE_DCPU_PLATFORM_EN = 1 << 0   # reg.h
R_BE_UDM0 = 0x01F0               # reg.h
R_BE_UDM1 = 0x01F4               # reg.h
R_BE_UDM2 = 0x01F8               # reg.h
R_BE_HALT_H2C_CTRL = 0x0160      # reg.h
R_BE_HALT_C2H_CTRL = 0x0164      # reg.h
R_BE_HALT_H2C = 0x0168           # reg.h
R_BE_HALT_C2H = 0x016C           # reg.h
R_BE_BOOT_DBG = 0x78F0           # reg.h
R_BE_HISR0 = 0x01A4              # reg.h
B_BE_HALT_C2H_INT = 1 << 21      # reg.h
R_BE_SYS_CLK_CTRL = 0x0008       # reg.h
B_BE_CPU_CLK_EN = 1 << 14        # reg.h
R_BE_SYS_CFG5 = 0x0170           # reg.h
B_BE_WDT_WAKE_PCIE_EN = 1 << 10  # reg.h
B_BE_WDT_WAKE_USB_EN = 1 << 9    # reg.h
R_BE_SECURE_BOOT_MALLOC_INFO = 0x0184  # reg.h
R_BE_GPIO_MUXCFG = 0x0040        # reg.h; same address as R_AX_GPIO_MUXCFG
B_BE_BOOT_MODE = 1 << 19         # reg.h; same bit as B_AX_BOOT_MODE
R_BE_BOOT_REASON = 0x01E6        # reg.h
B_BE_BOOT_REASON_MASK = 0x7      # GENMASK(2, 0). reg.h

# Firmware-download suit: secure-boot malloc + H2C/DLFW path-ready poll. [SRC] fw.c:1963-1971,
# mac_be.c:757-766, reg.h.
SECURE_BOOT_MALLOC_VALUE = 0x20248000  # 8922A NORMAL/WOWLAN. fw.c:1965
B_BE_H2C_PATH_RDY = 1 << 1       # reg.h:4544
B_BE_DLFW_PATH_RDY = 1 << 0      # reg.h:4545
R_AX_HALT_H2C_CTRL = 0x0160      # reg.h; same address as R_BE_HALT_H2C_CTRL
R_AX_HALT_C2H_CTRL = 0x0164      # reg.h; same address as R_BE_HALT_C2H_CTRL
B_BE_WCPU_FWDL_STATUS_MASK = 0x3C000000  # GENMASK(29, 26). reg.h
RTW89_FWDL_WCPU_FW_INIT_RDY = 7  # fw.h:18

# Firmware file + H2C/fwdl packet build. [SRC] fw.c, fw.h, usb.c, txrx.h, rtw8922a.c/rtw8922au.c.
FW_ASSET = "rtw8922a_fw-4.bin"   # multi-firmware file the capture loaded (fw_format 4)
RTW89_MFW_SIG = 0xFF             # fw.h:4293
RTW89_FW_NORMAL = 1             # enum rtw89_fw_type. fw.h
FWDL_SECTION_PER_PKT_LEN = 2020  # fw.h:287 (AX part_size; BE reads it from the header)
H2C_DESC_SIZE = 24               # sizeof(rtw89_rxdesc_short_v2). rtw8922a.c:3275
H2C_HEADER_LEN = 8               # fw.h:4599
RTW89_USB_MOD512_PADDING = 4     # usb.h:18
BULKOUT_ID_H2C = 2               # bulkout_id[RTW89_DMA_H2C]. rtw8922au.c:27
# TX descriptor dword0 (rtw89_build_txwd_fwcmd0_v2). [SRC] core.c:1892, txrx.h:490-495.
BE_RXD_RPKT_LEN_MASK = 0x3FFF    # GENMASK(13, 0)
BE_RXD_RPKT_TYPE_SHIFT = 24      # GENMASK(29, 24)
RTW89_CORE_RX_TYPE_H2C = 13      # core.h:402
RTW89_CORE_RX_TYPE_FWDL = 14     # core.h:403
# H2C fwcmd header (rtw89_h2c_pkt_set_hdr_fwdl). [SRC] fw.c:1649-1666, fw.h:4599-4667.
H2C_HDR_CAT_MAC = 0x1            # fw.h:4617
H2C_CL_MAC_FWDL = 0x3           # fw.h:4666
H2C_HDR_CLASS_SHIFT = 2          # H2C_HDR_CLASS = GENMASK(7, 2). fw.h:4601
H2C_HDR_TOTAL_LEN_MASK = 0x3FFF  # GENMASK(13, 0). fw.h:4605
# v1 firmware header fields. [SRC] fw.h:682-687.
FW_HDR_V1_W5_HDR_SIZE_SHIFT = 16     # GENMASK(31, 16)
FW_HDR_V1_W6_SEC_NUM_SHIFT = 8       # GENMASK(15, 8)
FW_HDR_V1_W6_SEC_NUM_MASK = 0xFF00
FW_HDR_V1_W6_DSP_CHKSUM = 1 << 24
FW_HDR_V1_W7_PART_SIZE_MASK = 0xFFFF  # GENMASK(15, 0)
FW_HDR_V1_W7_DYN_HDR = 1 << 16
# v1 section header fields. [SRC] fw.h:697-706.
FWSECTION_HDR_V1_W1_SEC_SIZE_MASK = 0xFFFFFF     # GENMASK(23, 0)
FWSECTION_HDR_V1_W1_SECTIONTYPE_SHIFT = 24       # GENMASK(27, 24)
FWSECTION_HDR_V1_W1_CHECKSUM = 1 << 28
FWSECTION_HDR_V1_W2_MSSC_MASK = 0xFF             # GENMASK(7, 0)
# Security-section / MSS pool. [SRC] fw.c:41,71-362, fw.h:286-287.
FWDL_SECURITY_SECTION_TYPE = 9
FWDL_SECTION_CHKSUM_LEN = 8
FWDL_SECURITY_SIGLEN = 512
FWDL_SECURITY_CHKSUM_LEN = 8
FWDL_MSS_POOL_DEFKEYSETS_SIZE = 8
FORMATTED_MSSC = 0xFF
MSS_POOL_HDR_LEN = 32            # sizeof(rtw89_fw_mss_pool_hdr) without rmp_tbl[]. fw.h
MSS_SIGNATURE = b"\x4d\x53\x53\x4b\x50\x4f\x4f\x4c"  # "MSSKPOOL". fw.c:41

# Logical efuse + phycap dump (rtw89_parse_efuse_map_be / rtw89_parse_phycap_map_be).
# [SRC] efuse_be.c:341-433, reg.h, rtw8922a.c.
R_BE_SYS_WL_EFUSE_CTRL = 0x000A  # reg.h:3866
B_BE_AUTOLOAD_SUS = 1 << 5       # reg.h:3873
PHYSICAL_EFUSE_SIZE = 0x1300     # chip->physical_efuse_size. rtw8922a.c:3242
PHYCAP_ADDR = 0x1700             # chip->phycap_addr. rtw8922a.c:3248
PHYCAP_SIZE = 0x38               # chip->phycap_size. rtw8922a.c:3249
R_BE_EFUSE_USB_MACADDR = 0x4078  # rtw8922a_read_efuse_usb reads the MAC here. rtw8922a.c:856
ETH_ALEN = 6

# Register H2C/C2H firmware mailbox (rtw89_fw_msg_reg). [SRC] fw.c:8229-8335, reg.h, fw.h.
R_BE_H2CREG_DATA0 = 0x7140       # reg.h:4848; DATAn = DATA0 + n*4
R_BE_C2HREG_DATA0 = 0x7150       # reg.h:4852
R_BE_H2CREG_CTRL = 0x7160        # reg.h:4856
B_BE_H2CREG_TRIGGER = 1 << 0     # reg.h:4857
R_BE_C2HREG_CTRL = 0x7164        # reg.h:4858
R_BE_MAILBOX_COUNTER = 0x01F5    # R_BE_UDM1 + 1. reg.h:4566
B_MAILBOX_H2C_CNT_MASK = 0x0F    # UDM1 H2C_DEQ_CNT >> 8. reg.h:4569
B_MAILBOX_C2H_CNT_MASK = 0xF0    # UDM1 C2H_ENQ_CNT >> 8. reg.h:4568
RTW89_H2CREG_MAX = 4             # fw.h:117
RTW89_C2HREG_MAX = 4             # fw.h:118
RTW89_H2CREG_HDR_LEN = 2         # fw.h:120
RTW89_C2HREG_HDR_LEN = 2         # fw.h:119
RTW89_H2CREG_HDR_FUNC_MASK = 0x7F      # GENMASK(6, 0). fw.h:101
RTW89_H2CREG_HDR_LEN_MASK = 0xF00      # GENMASK(11, 8). fw.h:102
RTW89_H2CREG_GET_FEATURE_PART_NUM = 0xFF0000  # GENMASK(23, 16). fw.h:115
RTW89_C2HREG_HDR_FUNC_MASK = 0x7F      # GENMASK(6, 0). fw.h:25
RTW89_C2HREG_HDR_LEN_MASK = 0xF00      # GENMASK(11, 8). fw.h:27
RTW89_FWCMD_H2CREG_FUNC_GET_FEATURE = 3      # fw.h:149
RTW89_FWCMD_C2HREG_FUNC_PHY_CAP = 3          # fw.h:163
RTW89_FWCMD_C2HREG_FUNC_PHY_CAP_PART1 = 0xC  # fw.h:166

# XTAL_SI indirect register access. [SRC] mac.c:7179-7233, reg.h/mac.h.
R_AX_WLAN_XTAL_SI_CTRL = 0x0270  # reg.h:268 (same address as the BE name)
B_AX_WL_XTAL_SI_ADDR_MASK = 0x000000FF     # GENMASK(7, 0). reg.h:278
B_AX_WL_XTAL_SI_DATA_MASK = 0x0000FF00     # GENMASK(15, 8). reg.h:277
B_AX_WL_XTAL_SI_MODE_MASK = 0x03000000     # GENMASK(25, 24). reg.h:273
B_AX_WL_XTAL_SI_CMD_POLL = 1 << 31         # BIT(31). reg.h:269
XTAL_SI_NORMAL_READ = 0x01       # reg.h:275
XTAL_SI_CV = 0x41                # mac.h:1666
XTAL_SI_ACV_MASK = 0x0F          # GENMASK(3, 0). mac.h:1667
XTAL_SI_CHIP_ID_L = 0xFD         # mac.h:1696
XTAL_SI_CHIP_ID_H = 0xFE         # mac.h:1697
XTAL_SI_POLL_ATTEMPTS = 1000     # read_poll_timeout(50us, 50ms). [SRC] mac.c:7221
