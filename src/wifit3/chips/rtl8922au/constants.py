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
RTW89_BAND_2G = 0                # enum rtw89_band. core.h
RTW89_BAND_5G = 1
RTW89_BAND_6G = 2
RTW89_CHANNEL_WIDTH_20 = 0       # enum rtw89_bandwidth. core.h

# rtw8922a_set_channel_mac. [SRC] rtw8922a.c:1048, reg.h.
R_BE_WMAC_RFMOD = 0x10010            # reg.h:6606
B_BE_WMAC_RFMOD_MASK = 0x7           # GENMASK(2, 0). reg.h:6609
BE_WMAC_RFMOD_20M = 0                # reg.h:6610
R_BE_TX_SUB_BAND_VALUE = 0x10088     # reg.h:6629
B_BE_PRI20_BITMAP_MASK = 0xFFFF0000  # GENMASK(31, 16). reg.h:6631
BE_PRI20_BITMAP_MAX = 15             # reg.h:6632
R_BE_TXRATE_CHK = 0x10828            # reg.h:7156
B_BE_BAND_MODE = 1 << 4              # BIT(4). reg.h:7162
B_BE_RTS_LIMIT_IN_OFDM6 = 1 << 1     # BIT(1). reg.h:7164
B_BE_CHECK_CCK_EN = 1 << 0           # BIT(0). reg.h:7165
R_BE_PREBKF_CFG_1 = 0x1033C          # reg.h:6804
B_BE_SIFS_MACTXEN_T1_MASK = 0x7F     # GENMASK(6, 0). reg.h:6809
R_BE_MUEDCA_EN = 0x10370             # reg.h:6850
B_BE_SIFS_MACTXEN_TB_T1_MASK = 0x7F0000  # GENMASK(22, 16). reg.h:6854

# rtw8922a_ctrl_sco_cck (set_channel_bb, 2G). [SRC] rtw8922a.c:1149, reg.h.
R_BK_FC0INV = 0x6758                 # reg.h:9947
B_BK_FC0INV = 0x7FFFF                # GENMASK(18, 0). reg.h:9948
R_CCK_FC0INV = 0x675C                # reg.h:9949
B_CCK_FC0INV = 0x7FFFF               # GENMASK(18, 0). reg.h:9950

# efuse rx-gain offset (rtw8922a_efuse_parsing_gain_offset) + set_rx_gain_normal. [SRC]
# rtw8922a.c:778, rtw8922a.h:56-57, reg.h.
EFUSE_RX_GAIN_A_OFST = 0xD4          # struct rtw8922a_efuse.rx_gain_a (in the block-1 map)
EFUSE_RX_GAIN_B_OFST = 0xD9          # struct rtw8922a_efuse.rx_gain_b
# rx_gain struct order -> rtw89_gain_offset enum: [_2g_ofdm, _2g_cck, _5g_low, _5g_mid, _5g_high].
EFUSE_RX_GAIN_ENUM_ORDER = (1, 0, 2, 3, 4)   # core.h:301 enum rtw89_gain_offset
GAIN_OFFSET_2G_CCK = 0               # core.h:302
GAIN_OFFSET_2G_OFDM = 1              # core.h:303
R_MGAIN_BIAS = 0x672C                # reg.h:9942
B_MGAIN_BIAS_BW20 = 0xF              # GENMASK(3, 0). reg.h:9943
B_MGAIN_BIAS_BW40 = 0xF0             # GENMASK(7, 4). reg.h:9944
R_CCK_RPL_OFST = 0x6750              # reg.h:9945
B_CCK_RPL_OFST = 0xFF                # GENMASK(7, 0). reg.h:9946

# rtw8922a_ctrl_ch tail (freq, sco, cck params, chan-idx). [SRC] rtw8922a.c:1490-1526, reg.h, phy.c.
R_FC0 = 0x6B4C                       # reg.h:9959
B_FC0 = 0x1FFF                       # GENMASK(12, 0). reg.h:9961
R_FC0INV_SBW = 0x6B50                # reg.h:9962
B_FC0_INV = 0x7F                     # GENMASK(6, 0). reg.h:9966
R_PCOEFF01 = 0x6684                  # reg.h:9926; R_PCOEFF23..EF follow at +4 each
B_PCOEFF = 0xFFFFFF                  # GENMASK(23, 0). reg.h:9927
R_MAC_PIN_SEL = 0x0734               # reg.h:8851
B_CH_IDX_SEG0 = 0xFF0000             # GENMASK(23, 16). reg.h:8853
RTW89_CH_BASE_IDX_2G = 0             # phy.c:8574
RTW89_CH_BASE_IDX_MASK = 0xF0        # GENMASK(7, 4). phy.c:8580
RTW89_CH_OFFSET_MASK = 0xF           # GENMASK(3, 0). phy.c:8581

# rtw8922a_ctrl_bw. [SRC] rtw8922a.c:1528, reg.h.
B_CHBW_BW = 0x7000                   # GENMASK(14, 12). reg.h:9969
B_CHBW_PRICH = 0xF00                 # GENMASK(11, 8). reg.h:9970
B_SMALLBW = 0xC0000000               # GENMASK(31, 30). reg.h:9963
R_DAC_CLK = 0x625C                   # reg.h:9917
B_DAC_CLK = 0xC0000000               # GENMASK(31, 30). reg.h:9918
R_GAIN_MAP0 = 0xE44C                 # reg.h:10428
B_GAIN_MAP0_EN = 1 << 0              # BIT(0). reg.h:10429
R_GAIN_MAP1 = 0xE54C                 # reg.h:10430
B_GAIN_MAP1_EN = 1 << 0              # BIT(0). reg.h:10431
B_BW40_2XFFT = 1 << 31               # BIT(31). reg.h:9960
# rtw8922a_ctrl_cck_en. [SRC] rtw8922a.c, reg.h.
R_UPD_CLK_ADC = 0x0700               # reg.h:8834
B_ENABLE_CCK = 1 << 5                # BIT(5). reg.h:8838
R_PD_ARBITER_OFF = 0x0C80            # reg.h:8986
B_PD_ARBITER_OFF = 1 << 31           # BIT(31). reg.h:8987

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

# Second power-on's mac_func_en (rtw89_mac_func_en_be, cmac_pwr_en_be, cmac_func_en_be).
# [SRC] mac_be.c:804-969, reg.h.
RTW89_MAC_0 = 0                          # core.h rtw89_mac_idx
RTW89_MAC_1 = 1
RTW89_MAC_BE_BAND_REG_OFFSET = 0x4000    # mac.h:591; reg_by_idx offset for CMAC1 (band 1)
R_BE_AFE_CTRL1 = 0x0024                  # reg.h:3958
B_BE_R_SYM_WLCMAC0_ALL_EN = 0x1F000000   # BIT(24..28). reg.h:3963
B_BE_R_SYM_WLCMAC1_ALL_EN = 0x0000001F   # BIT(0..4). reg.h:3984
B_BE_R_SYM_ISO_CMAC12PP = 1 << 25        # reg.h:4138
B_BE_R_SYM_ISO_CMAC02PP = 1 << 24        # reg.h:4139
B_BE_CMAC1_FEN = 1 << 17                 # reg.h:4144
B_BE_CMAC0_FEN = 1 << 16                 # reg.h:4145
R_BE_CK_EN = 0x10004                     # reg.h:6587
B_BE_CMAC_CKEN = 1 << 30                 # reg.h:6589
B_BE_TXTIME_CKEN = 1 << 8                # reg.h:6592
B_BE_RESP_PKTCTL_CKEN = 1 << 7           # reg.h:6593
B_BE_SIGB_CKEN = 1 << 6                  # reg.h:6594
B_BE_PHYINTF_CKEN = 1 << 5               # reg.h:6595
B_BE_CMAC_DMA_CKEN = 1 << 4              # reg.h:6596
B_BE_PTCLTOP_CKEN = 1 << 3               # reg.h:6597
B_BE_SCHEDULER_CKEN = 1 << 2             # reg.h:6598
B_BE_TMAC_CKEN = 1 << 1                  # reg.h:6599
B_BE_RMAC_CKEN = 1 << 0                  # reg.h:6600
B_BE_CK_EN_SET = (B_BE_CMAC_CKEN | B_BE_PHYINTF_CKEN | B_BE_CMAC_DMA_CKEN
                  | B_BE_PTCLTOP_CKEN | B_BE_SCHEDULER_CKEN | B_BE_TMAC_CKEN
                  | B_BE_RMAC_CKEN | B_BE_TXTIME_CKEN | B_BE_RESP_PKTCTL_CKEN
                  | B_BE_SIGB_CKEN)      # reg.h:6587
B_BE_CMAC_CRPRT = 1 << 31                # reg.h:6557
B_BE_CMAC_FUNC_EN_SET = (B_BE_CMAC_EN | B_BE_CMAC_TXEN | B_BE_CMAC_RXEN
                         | B_BE_PHYINTF_EN | B_BE_CMAC_DMA_EN | B_BE_PTCLTOP_EN
                         | B_BE_SCHEDULER_EN | B_BE_TMAC_EN | B_BE_RMAC_EN
                         | B_BE_CMAC_CRPRT | B_BE_TXTIME_EN | B_BE_RESP_PKTCTL_EN
                         | B_BE_SIGB_EN)  # reg.h:6581

# BB preinit (rtw89_chip_bb_preinit -> rtw8922a_bb_preinit + bbmcu_cr_init).
# [SRC] rtw8922a.c:1753-1818, core.h:7725, phy.h:10,804. dbcc_en is set on BE chips
# (core.c:6992), so bb_preinit runs for PHY_0 and PHY_1.
RTW89_PHY_0 = 0                          # core.h rtw89_phy_idx
RTW89_PHY_1 = 1
R_BE_DMAC_SYS_CR32B = 0x842C             # reg.h:4958
B_BE_DMAC_BB_PHY0_MASK = 0x0000FFFF      # GENMASK(15, 0). reg.h:4960
B_BE_DMAC_BB_PHY1_MASK = 0xFFFF0000      # GENMASK(31, 16). reg.h:4959
B_BE_FEN_BB1_IP_RSTN = 1 << 9            # reg.h:4149
B_BE_FEN_BB1PLAT_RSTB = 1 << 8           # reg.h:4150
B_BE_BOOT_RDY1 = 1 << 10                 # reg.h:4148
B_BE_BOOT_RDY0 = 1 << 2                  # reg.h:4153
R_BE_MEM_PWR_CTRL = 0x00D0               # reg.h:4278
B_BE_MEM_BBMCU0_DS_V1 = 1 << 17          # reg.h:4292
RTW89_BBMCU_ADDR_OFFSET = 0x30000        # phy.h:10
BB_MCU_INIT_REG = (                      # bb_mcu0_init_reg == bb_mcu1_init_reg. rtw8922a.c:1753-1777
    (0x6990, 0x00000000), (0x6994, 0x00000000), (0x6998, 0x00000000),
    (0x6820, 0xFFFFFFFE), (0x6800, 0xC0000FFE), (0x6808, 0x76543210),
    (0x6814, 0xBFBFB000), (0x6818, 0x0478C009), (0x6800, 0xC0000FFF),
    (0x6820, 0xFFFFFFFF),
)

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

# 8922A dle_mem[QTA_DBCC] config for the USB-2 path. [SRC] rtw8922a.c:206-210, mac.c.
S_AX_PLE_PAGE_SEL_256 = 2        # mac.h:615
WDE_SIZE8_LNK_PGE_NUM = 634      # wde_size8_v1={PG_64, 634, 6}. mac.c
WDE_SIZE8_SRT_OFST = 0
PLE_SIZE7_LNK_PGE_NUM = 2027     # ple_size7_v1={PG_256, 2027, 109, 40960}. mac.c
PLE_SIZE7_SRT_OFST = 40960
WDE_QT8_V1 = (608, 6, 0, 20)     # (hif, wcpu, pkt_in, cpu_io). mac.c
PLE_QT14_V1 = (939, 0, 16, 24, 7, 14, 57, 57, 24, 9, 1, 4, 0)          # ple_min_qt. mac.c
PLE_QT15_V1 = (939, 0, 16, 24, 882, 889, 932, 932, 899, 9, 1, 879, 0)  # ple_max_qt. mac.c

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

# Full HFC init (rtw89_mac_hfc_init en=true): per-channel + public + prec config for USB-2 DBCC.
# [SRC] mac.c:972-1246, mac_be.c:162-200, rtw8922a.c hfc tables, reg.h.
R_BE_CH0_PAGE_CTRL = 0xB718      # reg.h:6389; ach_page_ctrl base
R_BE_CH0_PAGE_INFO = 0xB750      # reg.h:6394; ach_page_info base
R_BE_PUB_PAGE_CTRL1 = 0xB790     # reg.h:6402
R_BE_PUB_PAGE_CTRL2 = 0xB794     # reg.h:6406
R_BE_PUB_PAGE_INFO3 = 0xB78C     # reg.h:6398
R_BE_PUB_PAGE_INFO1 = 0xB79C     # reg.h:6409
R_BE_PUB_PAGE_INFO2 = 0xB7A0     # reg.h:6413
R_BE_WP_PAGE_CTRL1 = 0xB7A4      # reg.h:6416
R_BE_WP_PAGE_CTRL2 = 0xB7A8      # reg.h:6424
R_BE_WP_PAGE_INFO1 = 0xB7AC      # reg.h:6427
B_AX_MAX_PG_MASK = 0x1FFF0000    # GENMASK(28, 16). reg.h:1097
B_AX_MIN_PG_MASK = 0x00001FFF    # GENMASK(12, 0). reg.h:1098
B_AX_GRP = 1 << 31               # reg.h:1099
B_AX_PUBPG_G1_MASK = 0x1FFF0000  # GENMASK(28, 16). reg.h:1134
B_AX_PUBPG_G0_MASK = 0x00001FFF  # GENMASK(12, 0). reg.h:1135
B_AX_WP_THRD_MASK = 0x00001FFF   # GENMASK(12, 0). reg.h:1152
B_BE_PREC_PAGE_CH011_V1_MASK = 0x0000003F   # GENMASK(5, 0). reg.h:6387
B_BE_PUBPG_ALL_MASK = 0x00001FFF            # GENMASK(12, 0). reg.h:6407
B_BE_PREC_PAGE_WP_CH811_MASK = 0x01FF0000   # GENMASK(24, 16). reg.h:6417
B_BE_PREC_PAGE_WP_CH07_MASK = 0x000001FF    # GENMASK(8, 0). reg.h:6418
B_BE_HCI_FC_CH12_FULL_COND_MASK = 0x00000C00      # GENMASK(11, 10). reg.h:6376
B_BE_HCI_FC_WP_CH811_FULL_COND_MASK = 0x00000300  # GENMASK(9, 8). reg.h:6377
B_BE_HCI_FC_WP_CH07_FULL_COND_MASK = 0x000000C0   # GENMASK(7, 6). reg.h:6378
B_BE_HCI_FC_WD_FULL_COND_MASK = 0x00000030        # GENMASK(5, 4). reg.h:6379
B_BE_HCI_FC_MODE_MASK = 0x00000006                # GENMASK(2, 1). reg.h:6381
RTW89_HCIFC_STF = 1              # core.h:4991
RTW89_DMA_H2C = 12               # hfc channel loop bound: ACH0..B1HIQ. core.h dma ch enum
# ch8 / pubcfg_p8 / prec_cfg_c6 for USB-2 DBCC. [SRC] rtw8922a.c (chcfg_ch8, pubcfg_p8), mac.c (c6).
HFC_CH_CFG_CH8 = (               # (min, max, grp) per DMA channel ACH0..H2D
    (24, 196, 0), (0, 0, 0), (54, 226, 0), (0, 0, 0), (54, 196, 1), (0, 0, 1),
    (54, 196, 1), (0, 0, 1), (54, 226, 0), (0, 0, 0), (54, 196, 1), (0, 0, 0),
    (0, 0, 0), (0, 0, 0), (0, 0, 0),
)
HFC_PUB_CFG_P8 = (304, 304, 608, 96)             # (grp0, grp1, pub_max, wp_thrd)
HFC_PREC_CFG_C6 = (8, 32, 148, 148, 1, 1, 0, 1)  # (ch011_prec, h2c_prec, wp07_prec, wp811_prec,
#                                                   ch011_full_cond, h2c_full_cond, wp07_fc, wp811_fc)

# STA scheduler init (sta_sch_init_be). [SRC] mac_be.c:971-998, reg.h.
R_BE_SS_CTRL = 0xA310            # reg.h:6224
B_BE_SS_INIT_DONE = 1 << 31      # reg.h:6226
B_BE_WARM_INIT = 1 << 29         # reg.h:6228
B_BE_BAND_TRIG_EN = 1 << 28      # reg.h:6229
B_BE_BAND1_TRIG_EN = 1 << 9      # reg.h:6239
B_BE_SS_EN = 1 << 0              # reg.h:6247

# MPDU processor init (mpdu_proc_init_be). [SRC] mac_be.c:1000-1033, reg.h.
R_BE_MPDU_PROC = 0x9C00          # reg.h:5967
B_BE_APPEND_FCS = 1 << 0         # reg.h:5977
R_BE_CUT_AMSDU_CTRL = 0x9C94     # reg.h:5994
TRXCFG_MPDU_PROC_CUT_CTRL = 0x010E05F0   # reg.h:1836
B_BE_CA_CHK_ADDRCAM_EN = 1 << 29 # reg.h:5997
R_BE_HDR_SHCUT_SETTING = 0x9B00  # reg.h:5955
B_BE_TX_MAC_MPDU_PROC_EN = 1 << 2  # reg.h:5958
B_BE_TX_HW_ACK_POLICY_EN = 1 << 1  # reg.h:5959
B_BE_TX_HW_SEQ_EN = 1 << 0       # reg.h:5960
B_BE_TX_ADDR_MLD_TO_LIK = 1 << 4 # reg.h:5956
R_BE_RX_HDRTRNS = 0x9CC0         # reg.h:6008
TRXCFG_MPDU_PROC_RX_HDR_CONV = 0x00000000  # reg.h:6015
B_BE_HC_ADDR_HIT_EN = 1 << 3     # reg.h:6011
R_BE_DISP_FWD_WLAN_0 = 0x8938    # reg.h:5446
B_BE_FWD_WLAN_CPU_TYPE_0_DATA_MASK = 0x00000003  # GENMASK(1, 0). reg.h:5462
B_BE_FWD_WLAN_CPU_TYPE_0_MNG_MASK = 0x0000000C   # GENMASK(3, 2). reg.h:5461
B_BE_FWD_WLAN_CPU_TYPE_0_CTL_MASK = 0x00000030   # GENMASK(5, 4). reg.h:5460
B_BE_FWD_WLAN_CPU_TYPE_1_MASK = 0x000000C0       # GENMASK(7, 6). reg.h:5459

# Security engine init (sec_eng_init_be). [SRC] mac_be.c:1035-1059, reg.h.
R_BE_SEC_ENG_CTRL = 0x9D00       # reg.h:6023
B_BE_SEC_PRE_ENQUE_TX = 1 << 11  # reg.h:6037
B_BE_CLK_EN_CGCMP = 1 << 10      # reg.h:6038
B_BE_CLK_EN_WAPI = 1 << 9        # reg.h:6039
B_BE_CLK_EN_WEP_TKIP = 1 << 8    # reg.h:6040
B_BE_BMC_MGNT_DEC = 1 << 5       # reg.h:6041
B_BE_UC_MGNT_DEC = 1 << 4        # reg.h:6042
B_BE_MC_DEC = 1 << 3             # reg.h:6043
B_BE_BC_DEC = 1 << 2             # reg.h:6044
B_BE_SEC_RX_DEC = 1 << 1         # reg.h:6045
B_BE_SEC_TX_ENC = 1 << 0         # reg.h:6046
R_BE_SEC_MPDU_PROC = 0x9D04      # reg.h:6048
B_BE_APPEND_ICV = 1 << 1         # reg.h:6056
B_BE_APPEND_MIC = 1 << 0         # reg.h:6057

# TX packet-control MPDU-info init (txpktctrl_init_be). [SRC] mac_be.c:1061-1091, reg.h.
R_BE_TXPKTCTL_MPDUINFO_CFG = 0x9F10      # reg.h:6097
B_BE_MPDUINFO_FEN = 1 << 31              # reg.h:6098
B_BE_MPDUINFO_PKTID_MASK = 0x0FFF0000    # GENMASK(27, 16). reg.h:6099
B_BE_MPDUINFO_B1_BADDR_MASK = 0x0000003F # GENMASK(5, 0). reg.h:6100
MPDU_INFO_B1_OFST = 18                   # reg.h:6101; dle_input is NULL on 8922A (after-8922D only)

# MLO table init (mlo_init_be). [SRC] mac_be.c:1093-1129, reg.h.
R_BE_MLO_INIT_CTL = 0xA114               # reg.h:6189
B_BE_MLO_TABLE_INIT_DONE = 1 << 31       # reg.h:6190
B_BE_MLO_TABLE_REINIT = 1 << 23          # reg.h:6192
B_BE_MLO_HW_CHGLINK_EN = 1 << 10         # reg.h:6238
R_BE_CMAC_SHARE_ACQCHK_CFG_0 = 0x0E010   # reg.h:6443
B_BE_R_MACID_ACQ_CHK_EN = 1 << 0         # reg.h:6448

# CMAC init - scheduler_init_be (8922A, non-D). CMAC-window regs, band-0 base. [SRC] mac_be.c:1186-1245.
R_BE_HE_CTN_CHK_CCA_NAV = 0x103C4        # reg.h:6921
B_BE_HE_CTN_CHK_TX_NAV = 1 << 15         # reg.h:6923
B_BE_HE_CTN_CHK_INTRA_NAV = 1 << 14      # reg.h:6924
B_BE_HE_CTN_CHK_BASIC_NAV = 1 << 13      # reg.h:6925
B_BE_HE_CTN_CHK_NO_GNT_WL = 1 << 12      # reg.h:6926
B_BE_HE_CTN_CHK_EDCCA_BITMAP = 1 << 3    # reg.h:6935
B_BE_HE_CTN_CHK_CCA_BITMAP = 1 << 2      # reg.h:6936
B_BE_HE_CTN_CHK_EDCCA_P20 = 1 << 1       # reg.h:6937
B_BE_HE_CTN_CHK_CCA_P20 = 1 << 0         # reg.h:6938
R_BE_HE_SIFS_CHK_CCA_NAV = 0x103B4       # reg.h:6902
B_BE_HE_SIFS_CHK_NO_GNT_WL = 1 << 12     # reg.h:6907
B_BE_HE_SIFS_CHK_EDCCA_BITMAP = 1 << 3   # reg.h:6916
B_BE_HE_SIFS_CHK_EDCCA_P20 = 1 << 1      # reg.h:6918
R_BE_TB_CHK_CCA_NAV = 0x103AC            # reg.h:6883
B_BE_TB_CHK_BASIC_NAV = 1 << 13          # reg.h:6887
B_BE_TB_CHK_NO_GNT_WL = 1 << 12          # reg.h:6888
B_BE_TB_CHK_EDCCA_BITMAP = 1 << 3        # reg.h:6897
R_BE_CCA_CFG_0 = 0x10340                 # reg.h:6811
B_BE_NO_GNT_WL_EN = 1 << 5               # reg.h:6828
R_BE_EDCA_BCNQ_PARAM = 0x10324           # reg.h:6789
B_BE_BCNQ_CW_MASK = 0xFF000000           # GENMASK(31, 24). reg.h:6791
B_BE_BCNQ_AIFS_MASK = 0x00FF0000         # GENMASK(23, 16). reg.h:6792
BCN_IFS_25US = 0x19                      # reg.h:6793
# addr_cam_init_be. [SRC] mac_be.c:1247-1276.
R_BE_ADDR_CAM_CTRL = 0x11434             # reg.h:8180
B_BE_ADDR_CAM_RANGE_MASK = 0x00FF0000    # GENMASK(23, 16). reg.h:8182
ADDR_CAM_SERCH_RANGE = 0x7F              # reg.h:8183
B_BE_ADDR_CAM_CLR = 1 << 8               # reg.h:8187
B_BE_ADDR_CAM_EN = 1 << 0                # reg.h:8190
# rx_fltr_init_be (type filters + RX filter opt + PLCP CRC). [SRC] mac_be.c:1275-1336.
R_BE_MGNT_FLTR = 0x11428                 # reg.h:8172
R_BE_CTRL_FLTR = 0x11424                 # reg.h:8166
R_BE_DATA_FLTR = 0x1142C                 # reg.h:8176
RX_FLTR_FRAME_ACCEPT_BE = 0xFFFF         # reg.h:8170
R_BE_RX_FLTR_OPT = 0x11420               # reg.h:8147
B_BE_UID_FILTER_MASK = 0xFF000000        # GENMASK(31, 24). reg.h:8149
B_BE_A_BC_CAM_MATCH = 1 << 5             # reg.h:8159
B_BE_A_UC_CAM_MATCH = 1 << 4             # reg.h:8160
B_BE_A_MC = 1 << 3                       # reg.h:8161
B_BE_A_BC = 1 << 2                       # reg.h:8162
B_BE_A_A1_MATCH = 1 << 1                 # reg.h:8163
B_BE_SNIFFER_MODE = 1 << 0               # reg.h:8164
R_BE_PLCP_HDR_FLTR = 0x11404             # reg.h:8123
B_BE_HE_SIGB_CRC_CHK = 1 << 6            # reg.h:8128
B_BE_VHT_MU_SIGB_CRC_CHK = 1 << 5        # reg.h:8129
B_BE_VHT_SU_SIGB_CRC_CHK = 1 << 4        # reg.h:8130
B_BE_SIGA_CRC_CHK = 1 << 3               # reg.h:8131
B_BE_LSIG_PARITY_CHK_EN = 1 << 2         # reg.h:8132
B_BE_CCK_SIG_CHK = 1 << 1                # reg.h:8133
B_BE_CCK_CRC_CHK = 1 << 0                # reg.h:8134
# nav_ctrl_init_be (cca_ctrl_init_be is a no-op). [SRC] mac_be.c nav_ctrl_init_be.
R_BE_WMAC_NAV_CTL = 0x11080             # reg.h:7695
B_BE_WMAC_NAV_UPPER_EN = 1 << 26        # reg.h:7697
B_BE_WMAC_PLCP_UP_NAV_EN = 1 << 17      # reg.h:7699
B_BE_WMAC_TF_UP_NAV_EN = 1 << 16        # reg.h:7700
B_BE_WMAC_NAV_UPPER_MASK = 0x0000FF00   # GENMASK(15, 8). reg.h:7701
NAV_25MS = 0xC4                         # reg.h:7702
R_BE_SPECIAL_TX_SETTING = 0x10820       # reg.h:7134
B_BE_BMC_NAV_PROTECT = 1 << 26          # reg.h:7140
R_BE_TRXPTCL_RESP_0 = 0x11004           # reg.h:7652
B_BE_WMAC_MBA_DUR_FORCE = 1 << 16       # reg.h:7665
# spatial_reuse_init_be + tmac_init_be (8922A). [SRC] mac_be.c:1376-1423.
R_BE_RX_SR_CTRL = 0x1144A               # reg.h:8221
B_BE_SR_CTRL_PLCP_EN = 1 << 1           # reg.h:8225
B_BE_SR_EN = 1 << 0                     # reg.h:8226
R_BE_BSSID_SRC_CTRL = 0x1144B           # reg.h:8228
B_BE_PLCP_SRC_EN = 1 << 0               # reg.h:8233
R_BE_TB_PPDU_CTRL = 0x1080C             # reg.h:7108
B_BE_QOSNULL_UPD_MUEDCA_EN = 1 << 3     # reg.h:7114
R_BE_WMTX_TCR_BE_4 = 0x10E2C            # reg.h:7629
B_BE_EHT_HE_PPDU_4XLTF_ZLD_USTIMER_MASK = 0x1F000000  # GENMASK(28, 24). reg.h:7633
B_BE_EHT_HE_PPDU_2XLTF_ZLD_USTIMER_MASK = 0x001F0000  # GENMASK(20, 16). reg.h:7634

# trxptcl_init_be (8922A). rrsr_cfgs.ref_rate = {RESP_1, REF_RATE_SEL, 0}. [SRC] mac_be.c:1425-1502, rtw8922a.c:325.
R_BE_MAC_LOOPBACK = 0x11020             # reg.h:7681
S_BE_MACLBK_PLCP_DLY_DEF = 0x28         # reg.h:7687
B_BE_MACLBK_PLCP_DLY_MASK = 0x0001FF00  # GENMASK(16, 8). reg.h:7686
B_BE_MACLBK_EN = 1 << 0                 # reg.h:7689
WMAC_SPEC_SIFS_CCK = 0xA                # reg.h:3069
B_BE_WMAC_SPEC_SIFS_CCK_MASK = 0x000000FF   # GENMASK(7, 0). reg.h:7668
WMAC_SPEC_SIFS_OFDM_1115E = 0x11        # reg.h:7667
B_BE_WMAC_SPEC_SIFS_OFDM_MASK = 0x0000FF00  # GENMASK(15, 8). reg.h:7666
R_BE_WMAC_ACK_BA_RESP_LEGACY = 0x11200  # reg.h:7855
B_BE_ACK_BA_RESP_LEGACY_CHK_EDCCA = 1 << 1  # reg.h:7872
R_BE_WMAC_ACK_BA_RESP_HE = 0x11204      # reg.h:7877
B_BE_ACK_BA_RESP_HE_CHK_EDCCA = 1 << 1  # reg.h:7894
R_BE_WMAC_ACK_BA_RESP_EHT_LEG_PUNC = 0x11208  # reg.h:7897
B_BE_ACK_BA_EHT_LEG_PUNC_CHK_EDCCA = 1 << 1   # reg.h:7914
R_BE_RXTRIG_TEST_USER_2 = 0x110B0       # reg.h:7705
B_BE_RXTRIG_FCSCHK_EN = 1 << 20         # reg.h:7709
R_BE_TRXPTCL_RESP_1 = 0x11008           # reg.h:7670
B_BE_FTM_RRSR_RATE_EN_MASK = 0x1F000000 # GENMASK(28, 24). reg.h:7673
B_BE_WMAC_RESP_DOPPLEB_BE_EN = 1 << 21  # reg.h:7675
B_BE_WMAC_RESP_DCM_EN = 1 << 20         # reg.h:7676
B_BE_WMAC_RESP_REF_RATE_SEL = 1 << 12   # reg.h:7678
B_BE_WMAC_RESP_REF_RATE_MASK = 0x00000FFF   # GENMASK(11, 0). reg.h:7679
R_BE_PTCL_RRSR1 = 0x10090               # reg.h:6656
B_BE_RRSR_RATE_EN_MASK = 0x00001F00     # GENMASK(12, 8). reg.h:6659
B_BE_RRSR_CCK_MASK = 0x0000000F         # GENMASK(3, 0). reg.h:6661
B_BE_RSC_MASK = 0x000000C0              # GENMASK(7, 6). reg.h:6660
R_BE_PTCL_RRSR0 = 0x1008C               # reg.h:6649
B_BE_RRSR_OFDM_MASK = 0x000000FF        # GENMASK(7, 0). reg.h:6654
B_BE_RRSR_HT_MASK = 0x0000FF00          # GENMASK(15, 8). reg.h:6653
B_BE_RRSR_VHT_MASK = 0x00FF0000         # GENMASK(23, 16). reg.h:6652
B_BE_RRSR_HE_MASK = 0xFF000000          # GENMASK(31, 24). reg.h:6651

# rmac_init_be + rst_bacam_be (8922A). [SRC] mac_be.c:1504-1587, mac.c:2921.
R_BE_RESPBA_CAM_CTRL = 0x1143C          # reg.h:8192
B_BE_BACAM_RST_MASK = 0x3               # GENMASK(1, 0). reg.h:8203
S_BE_BACAM_RST_DONE = 0                 # reg.h:8204
S_BE_BACAM_RST_ALL = 2                  # reg.h:8206
R_BE_DLK_PROTECT_CTL = 0x11402          # reg.h:8112
B_BE_RX_DLK_CCA_TIME_MASK = 0x0000FF00  # GENMASK(15, 8). reg.h:8114
TRXCFG_RMAC_CCA_TO = 32                 # reg.h:8115
B_BE_RX_DLK_DATA_TIME_MASK = 0x000000F0 # GENMASK(7, 4). reg.h:8116
TRXCFG_RMAC_DATA_TO = 15                # reg.h:8117
B_BE_RX_DLK_RST_EN = 1 << 1             # reg.h:8120
B_BE_RX_MPDU_MAX_LEN_MASK = 0x003F0000  # GENMASK(21, 16). reg.h:8151
R_BE_RCR = 0x11400                      # reg.h:8099
B_BE_BUSY_CHKSN = 1 << 15               # reg.h:8101
R_BE_RX_PLCP_EXT_OPTION_1 = 0x11514     # reg.h:8293
B_BE_PLCP_SU_PSDU_LEN_SRC = 1 << 8      # reg.h:8302
PLD_RLS_MAX_PG = 127                    # mac_be.c:1521
RX_MAX_LEN_UNIT = 512                   # mac_be.c:1522
RX_SPEC_MAX_LEN = 11454 + 512           # mac_be.c:1523
RTW89_PLE_PG_256 = 256                  # mac.h:611; dle_info.ple_pg_size for DBCC

# resp_pktctl_init_be + cmac_com_init_be + ptcl_init_be (8922A, USB). [SRC] mac_be.c:1589-1732.
PLE_RSVD_QT2 = (1, 56, 28, 6, 6, 6, 6, 0, 0, 0)  # (mpdu_info_tbl, b0_csi, ...) ple_rsvd_qt2. mac.c
R_BE_RESP_CSI_RESERVED_PAGE = 0x11810   # reg.h:8320
B_BE_CSI_RESERVED_PAGE_NUM_MASK = 0x0FFF0000    # GENMASK(27, 16). reg.h:8322
B_BE_CSI_RESERVED_START_PAGE_MASK = 0x00000FFF  # GENMASK(11, 0). reg.h:8323
R_BE_TX_SUB_BAND_VALUE = 0x10088        # reg.h:6629
B_BE_TXSB_160M_MASK = 0x0000F000        # GENMASK(15, 12). reg.h:6633
B_BE_TXSB_80M_MASK = 0x00000F00         # GENMASK(11, 8). reg.h:6636
B_BE_TXSB_40M_MASK = 0x000000F0         # GENMASK(7, 4). reg.h:6640
B_BE_TXSB_20M_MASK = 0x0000000F         # GENMASK(3, 0). reg.h:6644
S_BE_TXSB_160M_1 = 1                    # reg.h:6635
S_BE_TXSB_80M_2 = 2                     # reg.h:6638
S_BE_TXSB_40M_4 = 4                     # reg.h:6643
S_BE_TXSB_20M_8 = 8                     # reg.h:6645
S_BE_TXSB_160M_0 = 0                    # reg.h:6634; MAC_1 sub-band values
S_BE_TXSB_80M_0 = 0                     # reg.h:6637
S_BE_TXSB_40M_1 = 1                     # reg.h:6642
S_BE_TXSB_20M_2 = 2                     # reg.h:6647
R_BE_PTCL_COMMON_SETTING_0 = 0x10800    # reg.h:7067
B_BE_PTCL_TRIGGER_SS_EN_UL = 1 << 4     # reg.h:7074
B_BE_PTCL_TRIGGER_SS_EN_1 = 1 << 3      # reg.h:7075
B_BE_PTCL_TRIGGER_SS_EN_0 = 1 << 2      # reg.h:7076
B_BE_CMAC_TX_MODE_1 = 1 << 1            # reg.h:7077
B_BE_CMAC_TX_MODE_0 = 1 << 0            # reg.h:7078
R_BE_AMPDU_AGG_LIMIT = 0x10810          # reg.h:7118
B_BE_AMPDU_MAX_TIME_MASK = 0xFF000000   # GENMASK(31, 24). reg.h:7120
AMPDU_MAX_TIME = 0x9E                   # reg.h:7121
# cmac_dma_init_be. [SRC] mac_be.c:1734-1753.
R_BE_RX_CTRL_1 = 0x10C0C                # reg.h:7462
B_BE_RXDMA_TXRPT_QUEUE_ID_SW_MASK = 0x7E000000     # GENMASK(30, 25). reg.h:7464
B_BE_RXDMA_F2PCMDRPT_QUEUE_ID_SW_MASK = 0x00FC0000  # GENMASK(23, 18). reg.h:7465
WLCPU_RXCH2_QID = 0xA                   # reg.h:7469

# dbcc_enable_be -> band1_enable_be (qta is DBCC). [SRC] mac_be.c band1_enable_be, dle_quota_change_be.
R_BE_PTCL_TX_CTN_SEL = 0x108EC          # reg.h:7335
B_BE_PTCL_BUSY = 1 << 7                 # reg.h:7338
R_BE_PLE_BUFMGN_CTL = 0x9010            # reg.h:5590
B_BE_PLE_AVAL_UPD_REQ = 1 << 29         # reg.h:5591
B_BE_PLE_AVAL_UPD_QTAID_MASK = 0x0F000000  # GENMASK(27, 24). reg.h:5592
PLE_QTAID_B0_TXPL = 0                   # mac.h:146
PLE_QTAID_CMAC0_RX = 6                  # mac.h:152
# preload_init_be chip op (always runs for 8922A; reached for MAC_1 in band1_enable). [SRC] mac_be.c:2013-2058.
PRELD_AMSDU_SIZE = 52                   # reg.h:1984
PRELD_NEXT_MIN_SIZE = 255               # reg.h:1985
PRELD_NEXT_WND = 1                      # reg.h:1991
PRELD_B0_ENT_NUM = 10                   # reg.h:1982
PRELD_B1_ENT_NUM = 4                    # reg.h:2054
PRELD_MISCQ_ENT_NUM_8922A = 2           # reg.h:6108
PRELD_B0_ACQ_ENT_NUM_8922A = 8          # reg.h:6111
PRELD_B1_ACQ_ENT_NUM_8922A = 2          # reg.h:6112
R_BE_TXPKTCTL_B0_PRELD_CFG0 = 0x9F48    # reg.h:6104
R_BE_TXPKTCTL_B0_PRELD_CFG1 = 0x9F4C    # reg.h:6115
R_BE_TXPKTCTL_B1_PRELD_CFG0 = 0x9F88    # reg.h:6149
R_BE_TXPKTCTL_B1_PRELD_CFG1 = 0x9F8C    # reg.h:6155
B_BE_B0_PRELD_FEN = 1 << 31             # reg.h:6105
B_BE_B0_PRELD_USEMAXSZ_MASK = 0x03FF0000     # GENMASK(25, 16). reg.h:6106
B_BE_B0_PRELD_CAM_G1ENTNUM_MASK = 0x00001F00 # GENMASK(12, 8). reg.h:6107
B_BE_B0_PRELD_CAM_G0ENTNUM_MASK = 0x0000001F # GENMASK(4, 0). reg.h:6110
B_BE_B0_PRELD_NXT_TXENDWIN_MASK = 0x00000F00 # GENMASK(11, 8). reg.h:6116
B_BE_B0_PRELD_NXT_RSVMINSZ_MASK = 0x000000FF # GENMASK(7, 0). reg.h:6117

# IMR tables (enable_imr_be): (addr, clr, set) read-modify-writes. [SRC] rtw8922a.c:269-323, reg.h.
IMR_DMAC_REGS = (
    (0x08874, 0xFF57FFFF, 0xCC13E579),   # R_BE_DISP_HOST_IMR
    (0x08878, 0x7D7BFF7D, 0x3479387D),   # R_BE_DISP_CPU_IMR
    (0x08870, 0xFFFFDFDF, 0x3F000000),   # R_BE_DISP_OTHER_IMR
    (0x09A20, 0x00000003, 0x00000003),   # R_BE_PKTIN_ERR_IMR
    (0x0A3F0, 0x00000007, 0x00000007),   # R_BE_INTERRUPT_MASK_REG
    (0x0A128, 0xE0000000, 0xE0000000),   # R_BE_MLO_ERR_IDCT_IMR
    (0x09BF4, 0x00000001, 0x00000000),   # R_BE_MPDU_TX_ERR_IMR
    (0x09CF4, 0x00000002, 0x00000000),   # R_BE_MPDU_RX_ERR_IMR
    (0x09D2C, 0x0000001F, 0x0000001F),   # R_BE_SEC_ERROR_IMR
    (0x09888, 0x00001111, 0x00001111),   # R_BE_CPUIO_ERR_IMR
    (0x08C38, 0x3FFF3FFF, 0x3FFF3FFF),   # R_BE_WDE_ERR_IMR
    (0x08CC0, 0x00000100, 0x00000100),   # R_BE_WDE_ERR1_IMR
    (0x09038, 0x3FFF3FFF, 0x3FFF3FFF),   # R_BE_PLE_ERR_IMR
    (0x090C0, 0x07000000, 0x07000000),   # R_BE_PLE_ERRFLAG1_IMR
    (0x09430, 0x00003337, 0x00003327),   # R_BE_WDRLS_ERR_IMR
    (0x09F78, 0x03030F03, 0x00030F03),   # R_BE_TXPKTCTL_B0_ERRFLAG_IMR
    (0x09FB8, 0x03030F03, 0x00030F03),   # R_BE_TXPKTCTL_B1_ERRFLAG_IMR
    (0x09608, 0x00000003, 0x00000001),   # R_BE_BBRPT_COM_ERR_IMR
    (0x09628, 0x00000003, 0x00000003),   # R_BE_BBRPT_CHINFO_ERR_IMR
    (0x09638, 0x00000001, 0x00000001),   # R_BE_BBRPT_DFS_ERR_IMR
    (0x09668, 0x00000001, 0x00000001),   # R_BE_LA_ERRFLAG_IMR
    (0x09688, 0x00000F0F, 0x00000000),   # R_BE_CH_INFO_DBGFLAG_IMR
    (0x0A218, 0x00000001, 0x00000001),   # R_BE_PLRLS_ERR_IMR
    (0x0B0B8, 0x000000FB, 0x000000FB),   # R_BE_HAXI_IDCT_MSK
)
# trx_init tail: err_imr_ctrl_be + set_host_rpr_be + rsp_chk_sig clear. [SRC] mac_be.c, reg.h.
R_BE_DMAC_ERR_IMR = 0x8520              # reg.h:5079
DMAC_ERR_IMR_EN = 0xFFFFFFFF            # GENMASK(31, 0). reg.h:676
B_BE_DMAC_NOTX_ERR_INT_EN = 1 << 21     # reg.h:5080
R_BE_CMAC_ERR_IMR = 0x10160             # reg.h:6678
R_BE_CMAC_ERR_IMR_C1 = 0x14160          # reg.h:6679
CMAC0_ERR_IMR_EN = 0xFFFFFFFF           # GENMASK(31, 0). reg.h:2211
CMAC1_ERR_IMR_EN = 0xFFFFFFFF           # GENMASK(31, 0). reg.h:2212
R_BE_WDRLS_CFG = 0x9408                 # reg.h:5756
B_BE_WDRLS_MODE_MASK = 0x3              # GENMASK(1, 0). reg.h:5760
RTW89_RPR_MODE_STF = 1                  # core.h:5010
R_BE_RLSRPT0_CFG0 = 0x9440              # reg.h:5794
B_BE_RLSRPT0_QID_MASK = 0x3F            # GENMASK(5, 0). reg.h:5798
WDRLS_DEST_QID_STF = 0                  # reg.h:5800
R_BE_RLSRPT0_CFG1 = 0x9444              # reg.h:5802
S_BE_WDRLS_FLTR_TXOK = 1                # reg.h:5810
S_BE_WDRLS_FLTR_RTYLMT = 2             # reg.h:5811
S_BE_WDRLS_FLTR_LIFTIM = 4             # reg.h:5812
S_BE_WDRLS_FLTR_MACID = 8              # reg.h:5813
B_BE_RLSRPT0_FLTR_MAP_MASK = 0x0F000000   # GENMASK(27, 24). reg.h:5809
B_BE_RLSRPT0_TO_MASK = 0x00FF0000         # GENMASK(23, 16). reg.h:5814
B_BE_RLSRPT0_AGGNUM_MASK = 0x000000FF     # GENMASK(7, 0). reg.h:5815
R_BE_RSP_CHK_SIG = 0x11000              # reg.h:7638
B_BE_RSP_STATIC_RTS_CHK_SERV_BW_EN = 1 << 30  # reg.h:7640

IMR_CMAC_REGS = (
    (0x11884, 0x0001FF7B, 0x0001987B),   # R_BE_RESP_IMR
    (0x10C04, 0x7FFFFE00, 0x7FFFFE00),   # R_BE_RX_ERROR_FLAG_IMR
    (0x10C70, 0xFFFFC000, 0xFFFFC000),   # R_BE_TX_ERROR_FLAG_IMR
    (0x10C88, 0x3FC3FC00, 0x3FC3FC00),   # R_BE_RX_ERROR_FLAG_IMR_1
    (0x108C8, 0x7FC07F00, 0x08000000),   # R_BE_PTCL_IMR1
    (0x108C0, 0x80000003, 0x80000003),   # R_BE_PTCL_IMR0
    (0x108B8, 0x00000001, 0x00000000),   # R_BE_PTCL_IMR_2
    (0x103E8, 0x00000001, 0x00000001),   # R_BE_SCHEDULE_ERR_IMR
    (0x128E0, 0x00000001, 0x00000001),   # R_BE_C0_TXPWR_IMR
    (0x110BC, 0x000003FF, 0x000003FF),   # R_BE_TRXPTCL_ERROR_INDICA_MASK
    (0x114F8, 0x000003FF, 0x00000340),   # R_BE_RX_ERR_IMR
    (0x110F8, 0x0000003F, 0x00000000),   # R_BE_PHYINFO_ERR_IMR_V1
)

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
RTW89_FW_ELEMENT_ID_BBMCU0 = 0   # enum rtw89_fw_element_id. fw.h:4328
# General H2C command header (rtw89_h2c_pkt_set_hdr) + notify_dbcc. [SRC] fw.h:4600-4688, fw.c:1624.
FWCMD_TYPE_H2C = 0               # fw.h:4609
H2C_CAT_MAC = 0x1                # fw.h:4617
H2C_CL_MAC_MEDIA_RPT = 0x8       # fw.h:4685
H2C_FUNC_NOTIFY_DBCC = 0x5       # fw.h:4688
H2C_HDR_CAT_MASK = 0x00000003    # GENMASK(1, 0). fw.h:4600
H2C_HDR_CLASS_MASK = 0x000000FC  # GENMASK(7, 2). fw.h:4601
H2C_HDR_FUNC_MASK = 0x0000FF00   # GENMASK(15, 8). fw.h:4602
H2C_HDR_DEL_TYPE_MASK = 0x000F0000  # GENMASK(19, 16). fw.h:4603
H2C_HDR_H2C_SEQ_MASK = 0xFF000000   # GENMASK(31, 24). fw.h:4604
H2C_HDR_REC_ACK = 1 << 14        # fw.h:4606
H2C_HDR_DONE_ACK = 1 << 15       # fw.h:4607
RTW89_H2C_NOTIFY_DBCC_EN = 1 << 0  # fw.h:1868
# mac_init tail: feat_init (init_ba_cam_users x2) + set_ofld_cfg. [SRC] mac.c, fw.c.
H2C_CL_BA_CAM = 0xc              # fw.h:4734
H2C_FUNC_MAC_BA_CAM_INIT = 0x2   # fw.h:4737
RTW89_H2C_BA_CAM_INIT_USERS_MASK = 0x000000FF   # GENMASK(7, 0). fw.h:1958
RTW89_H2C_BA_CAM_INIT_OFFSET_MASK = 0x000FF000  # GENMASK(19, 12). fw.h:1959
RTW89_H2C_BA_CAM_INIT_BAND_SEL = 1 << 24        # fw.h:1960
BACAM_1024BMP_OCC_ENTRY = 4      # mac.c rtw89_mac_feat_init
H2C_CL_MAC_FW_OFLD = 0x9         # fw.h:4691
H2C_FUNC_OFLD_CFG = 0x14         # fw.h:4698
H2C_OFLD_CFG = bytes((0x09, 0x00, 0x00, 0x00, 0x5E, 0x00, 0x00, 0x00))  # fw.c:5311

# mac80211 add-interface H2C burst (rtw89_mac_vif_init). [SRC] fw.h:4670-4706.
H2C_CL_MAC_FR_EXCHG = 0x5             # fw.h:4670
H2C_CL_MAC_ADDR_CAM_UPDATE = 0x6     # fw.h:4681
H2C_FUNC_MAC_ADDR_CAM_UPD = 0x0      # fw.h:4682
H2C_FUNC_MAC_JOININFO = 0x0          # fw.h:4686
H2C_FUNC_MAC_FWROLE_MAINTAIN = 0x4   # fw.h:4687
H2C_FUNC_MAC_DCTLINFO_UD_V2 = 0xc    # fw.h:4675
H2C_FUNC_MAC_CCTLINFO_UD_G7 = 0x11   # fw.h:4678
H2C_FUNC_MAC_MACID_PAUSE_SLEEP = 0x28  # fw.h:4706
RTW89_WIFI_ROLE_MONITOR = 7          # core.h:427

# role_maintain w0 fields. [SRC] fw.h:1813-1819.
ROLE_MAINTAIN_W0_MACID = 0xFF        # GENMASK(7, 0)
ROLE_MAINTAIN_W0_WIFI_ROLE = 0x1E000  # GENMASK(16, 13)
# join_info v1 w0/w1 fields. [SRC] fw.h:1837-1862.
JOININFO_W0_MACID = 0xFF             # GENMASK(7, 0)
JOININFO_W0_OP = 0x100               # BIT(8): dis_conn
JOININFO_W0_WIFI_ROLE = 0x3C000000   # GENMASK(29, 26)
JOININFO_W1_MLO_MODE = 0x1000        # BIT(12): MLSR=1
JOININFO_W1_EMLSR_PADDING = 0x70000  # GENMASK(18, 16)
JOININFO_W1_EMLSR_TRANS_DELAY = 0x380000  # GENMASK(21, 19)
JOININFO_EML_PADDING_DELAY_256US = 4      # IEEE80211_EML_CAP_EML_PADDING_DELAY_256US. fw.c:5108
JOININFO_EMLSR_TRANSITION_DELAY_256US = 5  # IEEE80211_EML_CAP_EMLSR_TRANSITION_DELAY_256US. fw.c:5111
# addr-cam v0 (addrcam_ver 0) fields. [SRC] cam.h:38-116, mac.h:16-17, core.h:473, cam.h:12.
ADDR_CAM_W1_LEN = 0xFF0000           # GENMASK(23, 16)
ADDR_CAM_W2_VALID = 0x1              # BIT(0)
ADDR_CAM_W9_SEC_ENT_MODE = 0x30000   # GENMASK(17, 16)
ADDR_CAM_W12_BSSID_LEN = 0xFF0000    # GENMASK(23, 16)
ADDR_CAM_W13_BSSID_VALID = 0x1       # BIT(0)
ADDR_CAM_W13_BSSID_MASK = 0xFC       # GENMASK(7, 2)
ADDR_CAM_ENT_SHORT_SIZE = 0x20       # mac.h:16
BSSID_CAM_ENT_SIZE = 0x08            # mac.h:17
RTW89_ADDR_CAM_SEC_NORMAL = 2        # core.h:473
RTW89_BSSID_MATCH_ALL = 0x3F         # GENMASK(5, 0). cam.h:12
# USB mac_post_init -> rx_agg_cfg_v3 (8922A). [SRC] usb.c rtw89_usb_rx_agg_cfg_v3, usb.h:32-38.
R_BE_RXAGG_0_V1 = 0x6000         # usb.h:32
B_BE_RXAGG_0_EN = 1 << 31        # usb.h:33
B_BE_RXAGG_0_NUM_TH = 0x00FF0000       # GENMASK(23, 16). usb.h:34
B_BE_RXAGG_0_TIME_32US_TH = 0x0000FF00 # GENMASK(15, 8). usb.h:35
B_BE_RXAGG_0_BUF_SZ_1K = 0x000000FF    # GENMASK(7, 0). usb.h:36
R_BE_RXAGG_1_V1 = 0x6004         # usb.h:38
RTW89_FW_ELEMENT_ALIGN = 16      # fw.h:4325
FW_ELEMENT_HDR_SIZE = 32         # sizeof(rtw89_fw_element_hdr): 24 fixed + 8 union. fw.h:4496
FW_ELEMENT_BBMCU_CV_OFFSET = 24  # rtw89_fw_element_hdr.u.bbmcu.cv. fw.h:4518
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

# Physical->logical efuse parse (rtw89_eeprom_parser_be) for the RF block, and the RF-block
# field offsets read by rtw8922a_read_efuse_rf. [SRC] efuse_be.c:196-305, rtw8922a.c:436,861-873.
SEC_CTRL_EFUSE_SIZE = 4          # chip->sec_ctrl_efuse_size. rtw8922a.c:3241
EFUSE_RF_BLOCK_OFFSET = 0x10000  # efuse_blocks[RF].offset. rtw8922a.c:436
EFUSE_RF_BLOCK_SIZE = 0x240      # efuse_blocks[RF].size. rtw8922a.c:436
EFUSE_BLOCK_ID_MASK = 0xFFFF0000    # GENMASK(31, 16). efuse.h:10
EFUSE_BLOCK_SIZE_MASK = 0x0000FFFF  # GENMASK(15, 0). efuse.h:11
EFUSE_HDR_PAGE_MASK = 0x000E0000    # GENMASK(19, 17). efuse_be.c:196
EFUSE_HDR_OFFSET_MASK = 0x0001FFF0  # GENMASK(16, 4). efuse_be.c:197
EFUSE_HDR_WORD_EN_MASK = 0x0000000F # GENMASK(3, 0). efuse_be.c:199
EFUSE_RFE_TYPE_OFST = 0xCA       # struct rtw8922a_efuse.rfe_type. rtw8922a.h:51
EFUSE_XTAL_K_OFST = 0xB9         # struct rtw8922a_efuse.xtal_k. rtw8922a.h:47

# BB register init (rtw89_phy_init_bb_reg). [SRC] phy.c:1940-1966, phy.h:13-29, core.h:206.
RTW89_FW_ELEMENT_ID_BB_REG = 2   # enum rtw89_fw_element_id. fw.h:4330
RTW89_FW_ELEMENT_ID_BB_GAIN = 3  # fw.h:4331
CR_BASE_BE = 0x20000             # rtw89_phy_gen_be.cr_base. phy_be.c:1899
BYPASS_CR_DATA = 0xBABECAFE      # core.h:206
PHY_HEADLINE_VALID = 0xF         # phy.h:14
PHY_COND_BRANCH_IF = 0x8         # phy.h:24
PHY_COND_BRANCH_ELIF = 0x9       # phy.h:25
PHY_COND_BRANCH_ELSE = 0xA       # phy.h:26
PHY_COND_BRANCH_END = 0xB        # phy.h:27
PHY_COND_CHECK = 0x4             # phy.h:28
PHY_COND_DONT_CARE = 0xFF        # phy.h:29

# rtw8922a_bb_postinit register/bit table. [SRC] rtw8922a.c:1798-1849, reg.h.
R_BE_FEN_RST_ENABLE = 0x0084
B_BE_FEN_BBPLAT_RSTB = 1 << 0    # bbrst_mask[0]
B_BE_FEN_BB1PLAT_RSTB = 1 << 8   # bbrst_mask[1]
B_BE_BOOT_RDY0 = 1 << 2          # mcu_bootrdy_mask[0]
B_BE_BOOT_RDY1 = 1 << 10         # mcu_bootrdy_mask[1]
R_BBCLK = 0x0000
B_CLK_640M = 1 << 2
R_TXSCALE = 0x6284
B_TXFCTR_EN = 1 << 19
R_TXFCTR = 0x627C
B_TXFCTR_THD = 0x000FFC00        # GENMASK(19, 10)
R_SLOPE = 0x6B6C
B_EHT_RATE_TH = 0xF0000000       # GENMASK(31, 28)
B_SLOPE_A = 0x00003FFF           # GENMASK(13, 0)
B_SLOPE_B = 0x0FFFC000           # GENMASK(27, 14)
R_BEDGE = 0x6BFC
B_HE_RATE_TH = 0x78000000        # GENMASK(30, 27)
B_EHT_MCS14 = 1 << 31
R_BEDGE2 = 0x6C00
B_HT_VHT_TH = 0x00000FFF         # GENMASK(11, 0)
B_EHT_MCS15 = 1 << 31
R_BEDGE3 = 0x6C04
B_EHTTB_EN = 1 << 15
B_HEERSU_EN = 1 << 19
B_HEMU_EN = 1 << 21
B_TB_EN = 1 << 23
R_SU_PUNC = 0x6C08
B_SU_PUNC_EN = 1 << 1
R_BEDGE5 = 0x6C10
B_HWGEN_EN = 1 << 25
B_PWROFST_COMP = 1 << 20
R_MAG_AB = 0x6BF8
B_BY_SLOPE = 0xFF000000          # GENMASK(31, 24)
B_MAG_AB = 0x00FFFFFF            # GENMASK(23, 0)
R_MAG_A = 0x6BF4
B_MGA_AEND = 0xFF000000          # GENMASK(31, 24)
R_SC_CORNER = 0x6B70
B_SC_CORNER = 0x000007FF         # GENMASK(10, 0)
R_UDP_COEEF = 0x0CBC
B_UDP_COEEF = 1 << 19

# RF register init (rtw89_phy_init_rf_reg + write_full_rf_v2_a / write_rf).
# [SRC] phy.c:2060-2098, 1183-1206, phy.c write_full_rf_v2_a, rtw8922a.c:3083-3188, fw.c:rf_reg.
RTW89_FW_ELEMENT_ID_RADIO_A = 4  # enum rtw89_fw_element_id. fw.h:4332
RTW89_FW_ELEMENT_ID_RADIO_B = 5  # fw.h:4333
RF_PATH_A = 0
RF_PATH_B = 1
RTW89_RF_ADDR_ADSEL_MASK = 1 << 16  # phy.h:11
RFREG_MASK = 0xFFFFF             # core.h RFREG_MASK
RF_BASE_ADDR = (0xE000, 0xF000)  # chip->rf_base_addr. rtw8922a.c:3188
HWSI_IDLE_ADDR = (0x2C24, 0x2D24)   # write_full_rf_v2_a addr_is_idle[]. phy.c
HWSI_OFST_ADDR = (0x2AE0, 0x2BE0)   # write_full_rf_v2_a addr_ofst[]. phy.c
B_HWSI_BUSY = 1 << 29            # write_full_rf_v2_a poll bit. phy.c
B_HWSI_DATA_ADDR = 0x000000FF    # GENMASK(7, 0). reg.h
B_HWSI_DATA_VAL = 0x0FFFFF00     # GENMASK(27, 8). reg.h
H2C_CAT_OUTSRC = 0x2             # fw.h:4618
H2C_CL_OUTSRC_RF_REG_A = 0x8     # fw.h
H2C_CL_OUTSRC_RF_REG_B = 0x9     # fw.h
RTW89_H2C_RF_PAGE_SIZE = 500     # fw.h
RTW89_H2C_RF_PAGE_NUM = 3        # fw.h

# BT-coex init (rtw89_btc_ntfy_init -> rtw8922a_btc_set_rfe / btc_init_cfg).
# [SRC] coex.c:7746, rtw8922a.c:2727-2833, reg.h, coex.h.
RR_LUTWE = 0xEF                  # reg.h RR_LUTWE (trx-mask enable)
RR_LUTWA = 0x33                 # reg.h RR_LUTWA (group select)
RR_LUTWD0 = 0x3F                # reg.h RR_LUTWD0 (mask value)
B_LUTWEN = 1 << 17              # DEBUG_LUT_RFMODE_MASK. rtw8922a.c:2802
BTC_BT_SS_GROUP = 0x0           # coex.h
BTC_BT_TX_GROUP = 0x2           # coex.h
BTC_BT_RX_GROUP = 0x3           # coex.h
BTC_TRX_MASK_SS = 0x5FF         # rtw8922a.c:2805
BTC_TRX_MASK_RX = 0x5DF         # rtw8922a.c:2808
BTC_TRX_MASK_TX_BTG = 0x55F     # rtw8922a.c:2814 (shared ant, btg path)
BTC_TRX_MASK_TX = 0x5FF         # rtw8922a.c:2816
R_BTC_COEX_WL_REQ_BE = 0xE324   # reg.h
B_BTC_RSP_ACK_HI = 1 << 10      # reg.h
B_BTC_TX_BCN_HI = 1 << 22       # reg.h
B_BTC_TX_TRI_HI = 1 << 17       # reg.h
B_BTC_TX_NULL_HI = 1 << 23      # reg.h
R_BE_BT_BREAK_TABLE = 0xE344    # reg.h
BTC_BREAK_PARAM = 0xF0FFFFFF    # reg.h
R_BTC_ZB_COEX_TBL_0 = 0xE328    # coex.h
R_BTC_ZB_COEX_TBL_1 = 0xE32C    # coex.h
R_BTC_ZB_BREAK_TBL = 0xE350     # coex.h
BTC_ZB_COEX_TBL_VAL = 0xDA5A5A5A  # rtw8922a.c:2827-2830
BTC_ZB_BREAK_TBL_VAL = 0xF0FFFFFF
# scoreboard + WL tx-power coex control. [SRC] coex.c:3016-3060, rtw8922a.c:2836-2867, reg.h.
R_BE_SCOREBOARD = 0x00AC         # reg.h:4247; chip->btc_sb.n[0].get
WL_TX_POWER_NO_BTC_CTRL = 0xFFFFFFFF  # GENMASK(31, 0). coex.c:3016
R_BE_PWR_RATE_CTRL = 0x11A2C     # reg.h:8406
R_BE_PWR_REG_CTRL = 0x11A50      # reg.h:8436
R_BE_PWR_COEX_CTRL = 0x11A54     # reg.h:8439
B_BE_FORCE_PWR_BY_RATE_EN = 1 << 19    # reg.h
B_BE_FORCE_PWR_BY_RATE_VAL = 0x1FF00000  # GENMASK(28, 20). reg.h
B_BE_PWR_BT_EN = 1 << 23         # reg.h
B_BE_PWR_BT_VAL = 0x000001FF     # GENMASK(8, 0). reg.h

# Coex fw H2Cs + _run_coex(NTFY_INIT) cold path. [SRC] coex.c, fw.c, mac.c, rtw8922a.c, fw.h, reg.h.
# Coex version rtw89_btc_ver_defs[2] (RTL8922A, >=0.35.71): fcxmreg7/fcxslots7/fcxbtcrpt8/fcxinit7
# /fcxctrl7/fwlrole8/fcxosi1/drvinfo_type2. [SRC] coex.c:152-159.
H2C_CL_OUTSRC_BTC = 0x10         # BTFC_SET. fw.h:2339
BTF_SET_REPORT_EN = 0            # fw.h:2344
BTF_SET_SLOT_TABLE = 1
BTF_SET_MREG_TABLE = 2
BTF_SET_CX_POLICY = 3
BTF_SET_DRV_INFO = 5
R_BE_BT_PLT = 0x1087C            # reg.h:7219
B_BE_TX_PLT_GNT_WL = 1 << 0      # reg.h:7223
B_BE_RX_PLT_GNT_WL = 1 << 4      # reg.h:7227
B_BE_PLT_EN = 1 << 8             # reg.h:7231
B_MAC_AX_SB_FW_MASK = 0x7F000000    # GENMASK(30, 24). reg.h:159
B_AX_TOGGLE = 1 << 31            # reg.h:157
B_MAC_AX_BTGS1_NOTIFY = 1 << 0   # reg.h:160
MAC_AX_NOTIFY_TP_MAJOR = 0x81    # reg.h:161 (POWERON set)
B_MAC_AX_SB_DRV_MASK = 0x00FFFFFF   # GENMASK(23, 0). reg.h
BTC_WSCB_INIT = 0x00004003       # ACTIVE|ON|BTLOG = BIT0|BIT1|BIT14. coex.c:698-710

# rtw89_phy_dm_init BB inits (pre-RFK). [SRC] phy.c:8236, phy_be.c, rtw8922a.c, reg.h.
MASKDWORD = 0xFFFFFFFF
MAC_BAND1_OFFSET = 0x4000        # rtw89_mac_reg_by_idx band-1 delta (BE). mac.h:591
# bb_sethw / ctrl_mlo / ctrl_afe_dac. rtw8922a.c:2103-2160
R_EN_SND_WO_NDP = 0x047C
R_EN_SND_WO_NDP_C1 = 0x147C
B_EN_SND_WO_NDP = 1 << 1
R_BE_PWR_BOOST = 0x11A40
B_BE_PWR_CTRL_SEL = 1 << 16
R_DBCC = 0x6B48
B_DBCC_EN = 1 << 0
R_DBCC_FA = 0x703C
B_DBCC_FA = 1 << 12
R_AFEDAC0 = 0x2A5C
B_AFEDAC0 = 0xF8000000           # GENMASK(31, 27)
R_AFEDAC1 = 0x2A60
B_AFEDAC1 = 0x00000007           # GENMASK(2, 0)
R_EMLSR = 0x0044
B_EMLSR_PARM = 0x0FFFF000        # GENMASK(27, 12)
# env_monitor: ccx_top + ifs_clm. phy.c:6285-6503
R_CCX = 0x0C00
B_CCX_EN_MSK = 1 << 0
B_CCX_TRIG_OPT_MSK = 1 << 1
B_MEASUREMENT_TRIG_MSK = 1 << 2
B_CCX_EDCCA_OPT_MSK_V1 = 0x000000F0   # GENMASK(7, 4)
R_IFS_COUNTER = 0x0C28
B_IFS_COLLECT_EN = 1 << 12
R_IFS_T = (0x0C2C, 0x0C30, 0x0C34, 0x0C38)   # R_IFS_T1..T4
B_IFS_T_TH_LOW = 0x00007FFF      # GENMASK(14, 0)
B_IFS_T_TH_HIGH = 0xFFFF0000     # GENMASK(31, 16)
B_IFS_T_EN = 1 << 15
IFS_CLM_TH_LOW = (0, 3, 9, 33)
IFS_CLM_TH_HIGH = (2, 8, 32, 128)
# physts. phy.c:7127-7207, phy.h
R_PLCP_HISTOGRAM = 0x0738
B_STS_DIS_TRIG_BY_FAIL = 1 << 3
B_STS_DIS_TRIG_BY_BRK = 1 << 2
R_PHY_STS_BITMAP_START = 0x073C
R_PHY_STS_BITMAP_EHT = 0x0788
RTW89_PHYSTS_BITMAP_NUM = 17
RTW89_RSVD_9 = 9
RTW89_HE_MU = 6
RTW89_VHT_MU = 7
RTW89_TRIG_BASE_PPDU = 10
RTW89_CCK_PKT = 11
RTW89_HT_PKT = 13
RTW89_EHT_PKT = 16
IE01_CMN_OFDM = 1 << 1
IE04_07_EXT_PATH = 0x000000F0    # GENMASK(7, 4)
IE13_DL_MU_DEF = 1 << 13
IE20_DBG_OFDM = 1 << 20
# dig dyn_pd_th. phy.c:7652, rtw8922a.c:339-346
R_SEG0R_PD_V2 = 0x6A74
B_SEG0R_PD_LOWER_BOUND = 0x000007C0   # GENMASK(10, 6)
B_SEG0R_PD_SR_EN = 1 << 30
R_BMODE_PDTH_EN_V2 = 0x6718
B_BMODE_PDTH_LIMIT_EN = 1 << 30
R_BMODE_PDTH_V2 = 0x6708
B_BMODE_PDTH_LOWER_BOUND = 0xFF000000  # GENMASK(31, 24)
# cfo: crystal cap + dcfo. phy.c:5007-5102, phy_be.c:204-209
XTAL_SI_XTAL_SC_XO = 0x05        # mac.h:1651
XTAL_SI_XTAL_SC_XI = 0x04        # mac.h:1649
B_AX_XTAL_SC_MASK = 0x0000007F   # GENMASK(6, 0)
XTAL_SC_MASK = 0xFF
R_DCFO_OPT_BE = 0x6260
B_DCFO_OPT_EN_BE = 1 << 17
R_DCFO_WEIGHT_BE = 0x6244
B_DCFO_WEIGHT_MSK_BE = 0xF0000000    # GENMASK(31, 28)
# edcca + ch_info. rtw8922a.c:382, phy_be.c:1155-1163
R_TX_COLLISION_T2R_ST_BE = 0x0CC8
B_TX_COLLISION_T2R_ST_BE_M = 0x00003F00   # GENMASK(13, 8)
R_CHINFO_SEG = 0x00B4
B_CHINFO_SEG_LEN = 0x00000007    # GENMASK(2, 0)
B_CHINFO_SEG = 0x0001FF80        # GENMASK(16, 7)
R_CHINFO_DATA = 0x00C0
B_CHINFO_DATA_BITMAP = 0x007FFFFF    # GENMASK(22, 0)
R_CHINFO_ELM_SRC = 0x4D84
B_CHINFO_ELM_BITMAP = 0x007FFFFF     # GENMASK(22, 0)
B_CHINFO_SRC = 0xC0000000        # GENMASK(31, 30)
R_CHINFO_TYPE_SCAL = 0x4D88
B_CHINFO_TYPE = 0x00000006       # GENMASK(2, 1)
B_CHINFO_SCAL = 1 << 8
# bb_wrap_init. phy_be.c:508-1146, reg.h
R_BE_PWR_MACID_PATH_BASE = 0x0E500
R_BE_PWR_MACID_LMT_BASE = 0x0ED00
R_BE_PWR_BY_RATE = 0x11E00
R_BE_PWR_BY_RATE_END = 0x12044
R_BE_PWR_RULMT_START = 0x12048
R_BE_PWR_RULMT_END = 0x120E4
R_BE_PWR_RATE_OFST_CTRL = 0x11A30
R_BE_PWR_RATE_OFST_END = 0x11A38
R_BE_PWR_FTM_SS = 0x11B04
B_BE_PWR_BY_RATE_DBW_ON = 0x0C000000     # GENMASK(27, 26)
R_BE_PWR_REF_CTRL = 0x11A20
B_BE_PWR_OFST_LMT_DB = 0x0FF80000        # GENMASK(27, 19)
R_BE_PWR_OFST_LMTBF = 0x11A24
B_BE_PWR_OFST_LMTBF_DB = 0x000001FF      # GENMASK(8, 0)
B_BE_PWR_OFST_BYRATE_DB = 0x000001FF     # GENMASK(8, 0) on R_BE_PWR_RATE_CTRL
R_BE_PWR_OFST_RULMT = 0x11A44
B_BE_PWR_OFST_RULMT_DB = 0x0003FE00      # GENMASK(17, 9)
R_BE_PWR_OFST_SW = 0x11AE8
B_BE_PWR_OFST_SW_DB = 0x0F000000         # GENMASK(27, 24)
R_BE_PWR_FORCE_LMT = 0x11A28
B_BE_PWR_FORCE_LMT_ON = 1 << 6
B_BE_PWR_FORCE_RU_ENON = 1 << 28         # on R_BE_PWR_OFST_RULMT
B_BE_PWR_FORCE_RU_ON = 1 << 18           # on R_BE_PWR_OFST_RULMT
R_BE_PWR_FORCE_MACID = 0x11A48
B_BE_PWR_FORCE_MACID_ALL = 0x000FFE00    # BIT9|GENMASK(17,10)|BIT18|BIT19
B_BE_PWR_FORCE_COEX_ON = 0x38000000      # GENMASK(29, 27) on R_BE_PWR_COEX_CTRL
B_BE_PWR_FORCE_RATE_ON = 1 << 29         # on R_BE_PWR_BOOST
R_BE_PWR_FTM = 0x11B00
PWR_FTM_VAL = 0x00E4E431
R_BE_PWR_LISTEN_PATH = 0x11988
B_BE_PWR_LISTEN_PATH_EN = 0xF0000000     # GENMASK(31, 28)
R_BE_PWR_RSSI_TARGET_LMT = 0x11A84
R_BE_PWR_TH = 0x11A78
PWR_RSSI_TARGET_LMT_VAL = 0x0201FE00
PWR_TH_VAL = 0x00FFEC7E

# RFK hw-init + init_rf_nctl. [SRC] rtw8922a_rfk.c, phy.c:2100-2135, phy_be.c:443, reg.h, mac.h.
HWSI_ADD_ADDR = (0x2ADC, 0x2BDC)     # read_full_rf_v2_a add_reg
B_HWSI_ADD_CTL_MASK = 0x00000007     # GENMASK(2, 0)
B_HWSI_ADD_MASK = 0x00000FF0         # GENMASK(11, 4)
B_HWSI_ADD_RD = 1 << 2
B_HWSI_VAL_RDONE = 1 << 31
B_HWSI_ADD_POLL_MASK = 0x00000003    # GENMASK(1, 0)
RR_POW = 0xA0
RR_POW_SYN_V1 = 0x0000000F           # GENMASK(3, 0)
RR_MODOPT = 0x01
RR_TXG_SEL = 0x000E0000              # GENMASK(19, 17)
R_COEF_SEL = 0x8104
R_COEF_SEL_C1 = 0x8204
B_COEF_SEL_EN = 1 << 31
B_COEF_SEL_IQC_V1 = 0x00000003       # GENMASK(1, 0)
B_COEF_SEL_MDPD_V1 = 0x00000300      # GENMASK(9, 8)
R_CFIR_LUT = 0x8154
R_CFIR_LUT_C1 = 0x8254
B_CFIR_LUT_G3 = 1 << 3
B_CFIR_LUT_G5 = 1 << 5
XTAL_SI_PLL_1 = 0xE1                 # mac.h:1695
XTAL_SI_APBT = 0xD1                  # mac.h:1693
XTAL_SI_XTAL_PLL = 0x16             # mac.h:1659
RTW89_FW_ELEMENT_ID_RF_NCTL = 8      # fw.h:4336
R_GOTX_IQKDPK_C0 = 0xE464
R_GOTX_IQKDPK_C1 = 0xE564
B_GOTX_IQKDPK = 0x18000000           # GENMASK(28, 27)
R_IQKDPK_HC = 0x2AB8
B_IQKDPK_HC = 1 << 28
R_CLK_GCK = 0x1008
B_CLK_GCK = 0x01FFFFFF               # GENMASK(24, 0)
R_IOQ_IQK_DPK = 0x0C60
B_IOQ_IQK_DPK_CLKEN = 0x00000003     # GENMASK(1, 0)
R_IQK_DPK_RST = 0x0C6C
B_IQK_DPK_RST = 1 << 0
R_IQK_DPK_PRST = 0xE4AC
B_IQK_DPK_PRST = 1 << 27
R_IQK_DPK_PRST_C1 = 0xE5AC
R_TXRFC = 0x0C7C
B_TXRFC_RST = 0x00E00000             # GENMASK(23, 21)
R_IQK_DPK_RST_C1 = 0x1C6C
R_TXRFC_C1 = 0x1C7C

# set_txpwr_ref + power_trim (phycap PA/PAD bias). [SRC] rtw8922a.c:2429,942-1041, reg.h.
B_BE_PWR_REF_CTRL_OFDM = 0x000003FE  # GENMASK(9, 1)
B_BE_PWR_REF_CTRL_CCK = 0x0007FC00   # GENMASK(18, 10)
RR_BIASA = 0x60
RR_BIASA_TXG_V1 = 0x0000000F         # GENMASK(3, 0)
RR_BIASA_TXA_V1 = 0x00000F00         # GENMASK(11, 8)
RR_BIASD_TXG_V1 = 0x000000F0         # GENMASK(7, 4)
RR_BIASD_TXA_V1 = 0x0000F000         # GENMASK(15, 12)
PHYCAP_PA_PAD_CHECK_OFST = 0x1700    # check_pa_pad_trim_addr. rtw8922a.c:946
PABIAS_TRIM_OFST = (0x1707, 0x1734)  # pabias_trim_addr[]. rtw8922a.c:944
PADBIAS_TRIM_OFST = (0x1708, 0x1735) # pad_bias_trim_addr[]. rtw8922a.c:994

# bb_cfg_txrx_path (hal_reset + ctrl_trx_path) + mac band cfgs. [SRC] rtw8922a.c:2298-2626,
# mac_be.c:2585-2704, mac.c:6317, reg.h.
R_BE_CTN_DRV_TXEN = 0x10398
B_BE_CTN_TXEN_ALL_MASK = 0x0003FFFF  # GENMASK(17, 0)
R_BE_PPDU_STAT = 0x11440
B_BE_PPDU_STAT_RPT_EN = 1 << 0
R_DFS_EN = 0x2800                    # dfs_en_idx base; path B +0x100. rtw8922a.c:2229
B_DFS_EN = 1 << 1
R_TSSI_PWR = (0xE610, 0xE710)        # R_TSSI_PWR_P0/P1. tssi_cont_en
B_TSSI_CONT_EN = 1 << 3
R_ADC_FIFO_V1 = 0x10FC
B_ADC_FIFO_EN_V1 = 0xFF000000        # GENMASK(31, 24)
R_RXCCA_BE1 = 0x0520
B_RXCCA_BE1_DIS = 1 << 0
R_PD_CTRL = 0x0C3C
B_PD_HIT_DIS = 1 << 9
R_RSTB_ASYNC = 0x0704
B_RSTB_ASYNC_ALL = 1 << 1
R_MAC_SEL = 0x09A4
B_MAC_SEL = 0x000E0000               # GENMASK(19, 17)
PATH_COM_CR_AB = (                   # ctrl_tx_path_tmac RF_PATH_AB. rtw8922a.c:1872-1910
    (0x11A00, 0x21C86900), (0x11A04, 0x00E4E433), (0x11A08, 0x39390CC9),
    (0x11A0C, 0x4E433240), (0x11A10, 0x90CC900E), (0x11A14, 0x00240393),
    (0x11A18, 0x201C8600),
)
R_ANT_CHBW = 0x6B54
B_ANT_RX_SG0 = 0x0000000F            # GENMASK(3, 0)
R_FC0INV_SBW = 0x6B50
B_RX_1RCCA = 0x0003C000              # GENMASK(17, 14)
R_BRK_R = 0x0418
B_HTMCS_LMT = 0x00000300             # GENMASK(9, 8)
B_VHTMCS_LMT = 0x00600000            # GENMASK(22, 21)
R_BRK_HE = 0x0480
B_N_USR_MAX = 0x00003FC0             # GENMASK(13, 6)
B_NSS_MAX = 0x0001C000               # GENMASK(16, 14)
B_TB_NSS_MAX = 0x03800000            # GENMASK(25, 23)
R_BRK_EHT = 0x0474
B_RXEHT_NSS_MAX = 0x0000001C         # GENMASK(4, 2)
R_BRK_RXEHT = 0x0478
B_RXEHTTB_NSS_MAX = 0x0001C000       # GENMASK(16, 14)
B_RXEHT_N_USER_MAX = 0xFF000000      # GENMASK(31, 24)
HE_N_USER_MAX_8922A = 4
R_TXPWR_RST = (0xE60C, 0xE70C)       # R_TXPWR_RSTA/B (per phy). tssi_reset
B_TXPWR_RST = 1 << 16                # B_TXPWR_RSTA/B
R_BE_HW_PPDU_STATUS = 0x9C30
B_BE_FWD_PPDU_STAT_MASK = 0x000000FF # GENMASK(7, 0)
PPDU_STAT_RPT_VAL = 0x6D             # RPT_EN|APP_RX_CNT|APP_PLCP_HDR|RPT_CRC32|RPT_DMA
R_BE_RCR = 0x11400
B_BE_PHY_RPT_SZ_MASK = 0x00000030    # GENMASK(5, 4)
B_BE_HDR_CNV_SZ_MASK = 0x000000C0    # GENMASK(7, 6)
MAC_AX_PHY_RPT_SIZE_8 = 1            # mac.h:177
R_BE_DRV_INFO_OPTION = 0x11470
B_BE_DRV_INFO_PHYRPT_EN = 1 << 0
R_BE_AGG_LEN_HT_0 = 0x10814
B_AX_RTS_TXTIME_TH_MASK = 0x0000FF00 # GENMASK(15, 8)
B_AX_RTS_LEN_TH_MASK = 0x000000FF    # GENMASK(7, 0)
RTS_TXTIME_TH = 2                    # 88 >> 5 (default rts_threshold). mac.c:6318
RTS_LEN_TH = 0xFF                    # 4080 >> 4

# rfk_init_late H2Cs. [SRC] rtw8922a.c:2349-2367, fw.c:7360-7846, fw.h:4818-4831.
H2C_CL_OUTSRC_RF_FW_RFK = 0xB
H2C_CL_OUTSRC_RF_FW_NOTIFY = 0xA
H2C_FUNC_RFK_PRE_NOTIFY = 0x8
H2C_FUNC_RFK_DACK_OFFLOAD = 0x5
H2C_FUNC_RFK_RXDCK_OFFLOAD = 0x6
H2C_FUNC_OUTSRC_RF_MCC_INFO = 0xF

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

# mac80211 add-interface: rtw89_mac_port_update port-config registers (port 0, band 0).
# The BE port base is rtw89_port_base_be. [SRC] mac_be.c:35, reg.h.
R_BE_PORT_CFG_P0 = 0x10400              # reg.h:6951
R_BE_TBTT_PROHIB_P0 = 0x10404           # reg.h:6972
R_BE_BCNERLYINT_CFG_P0 = 0x1040C        # reg.h:6982
R_BE_TBTTERLYINT_CFG_P0 = 0x1040E       # reg.h:6986
R_BE_TBTT_AGG_P0 = 0x10412              # reg.h:6990
R_BE_BCN_SPACE_CFG_P0 = 0x10414         # reg.h:6994
R_BE_BCN_AREA_P0 = 0x10408              # reg.h:6977
R_BE_DTIM_CTRL_P0 = 0x10426             # reg.h:7018
R_BE_MBSSID_CTRL = 0x10568              # reg.h:7042
R_BE_P0MB_HGQ_WINDOW_CFG_0 = 0x10590    # reg.h:7062
R_BE_MBSSID_DROP_0 = 0x1083C            # reg.h:7174
R_BE_PTCL_BSS_COLOR_0 = 0x108A0         # reg.h:7233
R_BE_WMTX_MOREDATA_TSFT_STMP_CTL = 0x10E08   # reg.h:7619 (port_base .md_tsft)
R_BE_BCN_PSR_RPT_P0 = 0x11484           # reg.h:8250

B_AX_BRK_SETUP = 1 << 16                # BIT(16). reg.h:2384
B_AX_TBTT_PROHIB_EN = 1 << 13           # BIT(13). reg.h:2387
B_AX_BCNTX_EN = 1 << 12                 # BIT(12). reg.h:2388
B_AX_NET_TYPE_MASK = 0xC00              # GENMASK(11, 10). reg.h:2389
B_AX_RX_BSSID_FIT_EN = 1 << 4           # BIT(4). reg.h:2395
B_AX_TSF_UDT_EN = 1 << 3                # BIT(3). reg.h:2396
B_AX_PORT_FUNC_EN = 1 << 2              # BIT(2). reg.h:2397
B_AX_TXBCN_RPT_EN = 1 << 1              # BIT(1). reg.h:2398
B_AX_RXBCN_RPT_EN = 1 << 0             # BIT(0). reg.h:2399
B_AX_TBTT_HOLD_MASK = 0xFFF0000         # GENMASK(27, 16). reg.h:2406
B_AX_TBTT_SETUP_MASK = 0xFF             # GENMASK(7, 0). reg.h:2407
B_AX_BCN_MSK_AREA_MASK = 0xFFF0000      # GENMASK(27, 16). reg.h:2414
B_AX_BCNERLY_MASK = 0xFFF               # GENMASK(11, 0). reg.h:2422
B_AX_TBTTERLY_MASK = 0xFFF              # GENMASK(11, 0). reg.h:2429
B_AX_TBTT_AGG_NUM_MASK = 0xFF00         # GENMASK(15, 8). reg.h:2436
B_AX_BCN_SPACE_MASK = 0xFFFF            # GENMASK(15, 0). reg.h:2444
B_AX_DTIM_NUM_MASK = 0xFF00             # GENMASK(15, 8). reg.h:2483
B_AX_P0MB_ALL_MASK = 0xFFFFFE           # GENMASK(23, 1). reg.h:2526
B_AX_PORT_DROP_4_0_MASK = 0x1F0000      # GENMASK(20, 16). reg.h:2606
B_AX_BSS_COLOR_PORT_0_MASK = 0x3F       # B_AX_BSS_COLOB_AX_PORT_0_MASK, GENMASK(5, 0). reg.h:2640
B_AX_UPD_HGQMD = 1 << 1                 # BIT(1). reg.h:2997
B_AX_UPD_TIMIE = 1 << 0                 # BIT(0). reg.h:2998
B_AX_BCAID_P0_MASK = 0x7FF              # GENMASK(10, 0). reg.h:3406

# port_update defaults + monitor net_type. [SRC] mac.c:4547-4554, core.h:413.
RTW89_NET_TYPE_NO_LINK = 0       # monitor vif keeps zero-init net_type. core.h:413
BCN_INTERVAL = 100               # mac.c:4547
BCN_ERLY_DEF = 160               # mac.c:4548
BCN_SETUP_DEF = 2                # mac.c:4549
BCN_HOLD_DEF = 200               # mac.c:4551
BCN_MASK_DEF = 0                 # mac.c:4552
TBTT_ERLY_DEF = 5                # mac.c:4553
TBTT_AGG_DEF = 1                 # mac.c:4554
