"""RTL8188FTV — MAC register addresses + bit flags.

Symbols mirror `driver_sources/rtl8xxxu-source-v6.12.107/{regs.h,rtl8xxxu.h}`
with line numbers cited inline.  organised by milestone region so the
correct subset can be traced from the C source.

Wire protocol constants (USB_CMD_REQ, etc.) are at the top — shared
across all rtl8xxxu chips.
"""
from __future__ import annotations

# ---- USB vendor-control wire protocol --------------------------------
# `rtl8xxxu.h:34-36`
USB_CMD_REQ = 0x05
USB_REQTYPE_READ = 0xC0
USB_REQTYPE_WRITE = 0x40
USB_VENQT_CMD_IDX = 0x00
USB_CONTROL_TIMEOUT_MS = 500  # `rtl8xxxu.h:28  RTW_USB_CONTROL_MSG_TIMEOUT`

# ---- Firmware upload page size ---------------------------------------
# `rtl8xxxu.h:76`
RTL_FW_PAGE_SIZE = 4096
FW_WRITE_BLOCK_SIZE = 128          # fops.writeN_block_size for 8188F
FW_HEADER_SIZE = 32                # struct rtl8xxxu_firmware_header
FW_SIGNATURE_88F = 0x88F0          # 8188f signature family (core.c:2120)
RTL8XXXU_FIRMWARE_POLL_MAX = 1000  # poll iteration limit (core.c)

# ---- MAC register addresses (regs.h) ---------------------------------
REG_SYS_ISO_CTRL = 0x0000           # regs.h:9
REG_SYS_FUNC = 0x0002               # regs.h:16
REG_APS_FSMCO = 0x0004              # regs.h:34
REG_SYS_CLKR = 0x0008               # regs.h:46
REG_RSV_CTRL = 0x001C               # regs.h:71
REG_RF_CTRL = 0x001F                # regs.h:75
REG_AFE_XTAL_CTRL = 0x0024          # regs.h:98
REG_EFUSE_CTRL = 0x0030             # regs.h:120
REG_EFUSE_TEST = 0x0034             # regs.h:121
REG_GPIO_MUXCFG = 0x0040            # regs.h:140
REG_LEDCFG2 = 0x004E                # regs.h:168
REG_MCU_FW_DL = 0x0080              # regs.h:218
REG_AFE_CTRL4 = 0x0078              # regs.h:215
REG_SYS_CFG = 0x00F0                # regs.h:312
REG_CR = 0x0100                     # regs.h:370
REG_PBP = 0x0104                    # regs.h:391
REG_TRXFF_BNDY = 0x0114             # regs.h:423
REG_HMTFR = 0x01CC                  # regs.h:456
REG_LLT_INIT = 0x01E0               # regs.h:462
REG_RQPN = 0x0200                   # regs.h:477
REG_TDECTRL = 0x0208                # regs.h:484
REG_RQPN_NPQ = 0x0214               # regs.h:492
REG_TXPKTBUF_BCNQ_BDNY = 0x0424     # regs.h:550
REG_TXPKTBUF_MGQ_BDNY = 0x0425      # regs.h:551
REG_TXPKTBUF_WMAC_LBK_BF_HD = 0x045D  # regs.h:609
REG_MAX_AGGR_NUM = 0x04CA           # regs.h:632
REG_BT_COEX_TABLE = 0x0500          # regs.h:734
REG_BT_COEX_CTRL = 0x0520           # regs.h:754
REG_FW_START_ADDRESS = 0x1000       # regs.h:1199

# ---- REG_SYS_ISO_CTRL bits (regs.h:10-15) ----------------------------
SYS_ISO_PWC_EV12V = 1 << 15           # regs.h:14 (1.2V EFUSE power)

# ---- REG_SYS_CLKR bits (regs.h:47-52) --------------------------------
SYS_CLK_ANA8M = 1 << 1                # regs.h:48
SYS_CLK_LOADER_ENABLE = 1 << 5        # regs.h:50

# ---- REG_SYS_FUNC bits (regs.h:17-30) --------------------------------
SYS_FUNC_BBRSTB = 1 << 0
SYS_FUNC_BB_GLB_RSTN = 1 << 1
SYS_FUNC_USBA = 1 << 2
SYS_FUNC_UPLL = 1 << 3
SYS_FUNC_USBD = 1 << 4
SYS_FUNC_DIO_PCIE = 1 << 5
SYS_FUNC_PCIEA = 1 << 6
SYS_FUNC_PPLL = 1 << 7
SYS_FUNC_PCIED = 1 << 8
SYS_FUNC_DIOE = 1 << 9
SYS_FUNC_CPU_ENABLE = 1 << 10
SYS_FUNC_DCORE = 1 << 11
SYS_FUNC_ELDR = 1 << 12
SYS_FUNC_DIO_RF = 1 << 13
SYS_FUNC_HWPDN = 1 << 14
SYS_FUNC_MREGEN = 1 << 15

# ---- REG_APS_FSMCO bits (regs.h:35-45) -------------------------------
APS_FSMCO_PFM_ALDN = 1 << 1
APS_FSMCO_PFM_WOWL = 1 << 3
APS_FSMCO_ENABLE_POWERDOWN = 1 << 4
APS_FSMCO_MAC_ENABLE = 1 << 8
APS_FSMCO_MAC_OFF = 1 << 9
APS_FSMCO_SW_LPS = 1 << 10
APS_FSMCO_HW_SUSPEND = 1 << 11
APS_FSMCO_PCIE = 1 << 12
APS_FSMCO_HW_POWERDOWN = 1 << 15
APS_FSMCO_WLON_RESET = 1 << 16

# ---- REG_MCU_FW_DL bits (regs.h:219-227) -----------------------------
MCU_FW_DL_ENABLE = 1 << 0
MCU_FW_DL_READY = 1 << 1
MCU_FW_DL_CSUM_REPORT = 1 << 2
MCU_WINT_INIT_READY = 1 << 6
MCU_FW_RAM_SEL = 1 << 7

# ---- REG_SYS_CFG bits (regs.h:312-338) --------------------------------
SYS_CFG_CHIP_VERSION_MASK = 0x000F_0000  # GENMASK(19,16)
SYS_CFG_VENDOR_EXT_MASK = 0x000C_0000    # BIT(18)|BIT(19)
SYS_CFG_TRP_VAUX_EN = 1 << 23

# ---- REG_CR bits (regs.h:370-381) ------------------------------------
CR_HCI_TXDMA_ENABLE = 1 << 0
CR_HCI_RXDMA_ENABLE = 1 << 1
CR_TXDMA_ENABLE = 1 << 2
CR_RXDMA_ENABLE = 1 << 3
CR_PROTOCOL_ENABLE = 1 << 4
CR_SCHEDULE_ENABLE = 1 << 5
CR_MAC_TX_ENABLE = 1 << 6
CR_MAC_RX_ENABLE = 1 << 7
CR_SECURITY_ENABLE = 1 << 9
CR_CALTIMER_ENABLE = 1 << 10
CR_ENSWBCN = 1 << 21

# ---- REG_AFE_XTAL_CTRL (8188f.c:1647-1648) ---------------------------
XTAL0_MASK = 0x0001_F800  # GENMASK(16, 11)
XTAL1_MASK = 0x007E_0000  # GENMASK(22, 17)
XTAL0_SHIFT = 11
XTAL1_SHIFT = 17

# ---- REG_RF_CTRL bits (regs.h:75-78) --------------------------------
RF_ENABLE = 1 << 0
RF_RSTB = 1 << 1
RF_SDMRSTB = 1 << 2

# ---- M3: init_device post-PHY config (regs.h) -----------------------
REG_FPGA0_TX_INFO = 0x0804          # regs.h:889
REG_TXDMA_OFFSET_CHK = 0x020C       # regs.h:488
TXDMA_OFFSET_DROP_DATA_EN = 1 << 9  # regs.h:489
TX_TOTAL_PAGE_NUM_8188F = 0xF7      # rtl8xxxu.h:40 (write +1)
REG_AUTO_LLT = 0x0224               # regs.h:495
AUTO_LLT_INIT_LLT = 1 << 16         # regs.h:496
PBP_PAGE_SIZE_RX_SHIFT = 0          # regs.h:391
PBP_PAGE_SIZE_TX_SHIFT = 4          # regs.h:392
PBP_PAGE_SIZE_256 = 0x02            # regs.h:395
REG_TX_REPORT_CTRL = 0x04EC         # regs.h:647
TX_REPORT_CTRL_TIMER_ENABLE = 1 << 1  # regs.h:648
REG_TX_REPORT_TIME = 0x04F0         # regs.h:650
REG_RX_DRVINFO_SZ = 0x060F          # regs.h:786
REG_HISR0 = 0x00B4                  # regs.h:271
REG_HISR1 = 0x00BC                  # regs.h:295
REG_RCR = 0x0608                    # regs.h:745
RCR_ACCEPT_PHYS_MATCH = 1 << 1      # regs.h:747
RCR_ACCEPT_MCAST = 1 << 2           # regs.h:748
RCR_ACCEPT_BCAST = 1 << 3           # regs.h:749
RCR_ACCEPT_MGMT_FRAME = 1 << 13     # regs.h:763
RCR_HTC_LOC_CTRL = 1 << 14          # regs.h:765
RCR_APPEND_PHYSTAT = 1 << 28        # regs.h:779
RCR_APPEND_ICV = 1 << 29            # regs.h:780
RCR_APPEND_MIC = 1 << 30            # regs.h:781
REG_RXFLTMAP2 = 0x06A4              # regs.h:858 (data)
REG_RXFLTMAP1 = 0x06A2              # regs.h:859 (control)
REG_RXFLTMAP0 = 0x06A0              # regs.h:860 (mgmt)
REG_RESPONSE_RATE_SET = 0x0440      # regs.h:568
RESPONSE_RATE_BITMAP_ALL = 0xFFFFF  # regs.h:569
RESPONSE_RATE_RRSR_CCK_ONLY_1M = 0xFFFF1  # regs.h:570
REG_SPEC_SIFS = 0x0428              # regs.h:554
REG_RETRY_LIMIT = 0x042A            # regs.h:560
REG_MAC_SPEC_SIFS = 0x063A          # regs.h:794
REG_SIFS_CCK = 0x0514               # regs.h:664
REG_SIFS_OFDM = 0x0516              # regs.h:665
REG_EDCA_VO_PARAM = 0x0500          # regs.h:654
REG_EDCA_VI_PARAM = 0x0504          # regs.h:655
REG_EDCA_BE_PARAM = 0x0508          # regs.h:656
REG_EDCA_BK_PARAM = 0x050C          # regs.h:657
REG_DARFRC = 0x0430                 # regs.h:566
REG_RARFRC = 0x0438                 # regs.h:567
REG_FWHW_TXQ_CTRL = 0x0420          # regs.h:543
FWHW_TXQ_CTRL_AMPDU_RETRY = 1 << 7  # regs.h:544
REG_ACKTO = 0x0640                  # regs.h:801
REG_BEACON_CTRL = 0x0550            # regs.h:677
BEACON_DISABLE_TSF_UPDATE = 1 << 4  # regs.h:683
REG_TBTT_PROHIBIT = 0x0540          # regs.h:673
REG_BEACON_DMA_TIME = 0x0559        # regs.h:699
BEACON_DMA_ATIME_INT_TIME = 2       # regs.h:700
REG_BEACON_TCFG = 0x0510            # regs.h:661
REG_RXDMA_PRO_8723B = 0x0290        # regs.h:515
RXDMA_PRO_DMA_MODE = 1 << 1         # regs.h:516
RXDMA_PRO_DMA_BURST_CNT = (3 << 2)  # regs.h:517
RXDMA_PRO_DMA_BURST_SIZE = (1 << 4)  # regs.h:518
REG_HT_SINGLE_AMPDU_8723B = 0x04C7  # regs.h:629
HT_SINGLE_AMPDU_ENABLE = 1 << 7     # regs.h:630
REG_AMPDU_MAX_TIME_8723B = 0x0456   # regs.h:605
REG_AGGLEN_LMT = 0x0458             # regs.h:606
REG_FAST_EDCA_CTRL = 0x0460         # regs.h:609
REG_RX_PKT_LIMIT = 0x060C           # regs.h:784
REG_PIFS = 0x0512                   # regs.h:662
REG_USTIME_TSF_8723B = 0x055C       # regs.h:703
REG_USTIME_EDCA = 0x0638            # regs.h:793
RSV_CTRL_WLOCK_1C = 1 << 5          # regs.h:72
RSV_CTRL_DIS_PRST = 1 << 6          # regs.h:73
REG_TRXDMA_CTRL = 0x010C            # regs.h:404
TRXDMA_CTRL_RXDMA_AGG_EN = 1 << 2   # regs.h:405
REG_DWBCN1_CTRL_8723B = 0x0228      # regs.h:498
REG_RXDMA_AGG_PG_TH = 0x0280        # regs.h:502
RXDMA_USB_AGG_ENABLE = 1 << 31      # regs.h:507
TRXDMA_CTRL_RXDMA_AGG_EN = 1 << 2   # regs.h:405
REG_PKT_VO_VI_LIFE_TIME = 0x04C0    # regs.h:624
REG_PKT_BE_BK_LIFE_TIME = 0x04C2    # regs.h:625 (implied)
REG_CAM_CMD = 0x0670                # regs.h:823
CAM_CMD_POLLING = 1 << 31           # regs.h:824
REG_HWSEQ_CTRL = 0x0423             # regs.h:548
REG_BAR_MODE_CTRL = 0x04CC          # regs.h:633
REG_NHM_TH9_TH10_8723B = 0x0890     # regs.h:977
REG_NHM_TIMER_8723B = 0x0894        # regs.h:978
REG_NHM_TH3_TO_TH0_8723B = 0x0898   # regs.h:979
REG_NHM_TH7_TO_TH4_8723B = 0x089C   # regs.h:980
REG_FPGA0_IQK = 0x0E28              # regs.h:1145
REG_OFDM0_FA_RSTC = 0x0C0C          # regs.h:1070
GPIO_MUXCFG_IO_SEL_ENBT = 1 << 5    # regs.h:141

# ---- RFSW antenna select (regs.h:953-959) ---------------------------
FPGA0_RF_TRSW = 1 << 5              # regs.h:953
FPGA0_RF_TRSWB = 1 << 6             # regs.h:954
FPGA0_RF_ANTSW = 1 << 8             # regs.h:955
FPGA0_RF_ANTSWB = 1 << 9            # regs.h:956
FPGA0_RF_PAPE = 1 << 10             # regs.h:957
FPGA0_RF_BD_CTRL_SHIFT = 16         # regs.h:959

# ---- PHY / baseband (regs.h:883+) -----------------------------------
REG_FPGA0_RF_MODE = 0x0800          # regs.h:883
FPGA_RF_MODE = 1 << 0
FPGA_RF_MODE_CCK = 1 << 24
FPGA_RF_MODE_OFDM = 1 << 25
REG_FPGA1_RF_MODE = 0x0900          # regs.h:988
REG_FPGA0_ANALOG4 = 0x088C          # regs.h:975
REG_FPGA0_PSD_FUNC = 0x0808         # regs.h:894
REG_FPGA0_PSD_REPORT = 0x08B4       # regs.h:984
REG_FPGA0_XA_HSSI_PARM2 = 0x0824     # regs.h:904
FPGA0_HSSI_3WIRE_DATA_LEN = 0x800    # regs.h:907
FPGA0_HSSI_3WIRE_ADDR_LEN = 0x400    # regs.h:908
REG_FPGA0_XA_RF_INT_OE = 0x0860     # regs.h:930
REG_FPGA0_XA_RF_SW_CTRL = 0x0870    # regs.h:942 (16-bit)
REG_FPGA0_XAB_RF_SW_CTRL = 0x0870   # regs.h:939 (32-bit alias)
FPGA0_RF_RFENV = 1 << 4             # regs.h:952
REG_FPGA0_XA_LSSI_PARM = 0x0840     # regs.h:919 (RFREG data reg, path A)
REG_FPGA0_XB_LSSI_PARM = 0x0844     # regs.h:920 (RFREG data reg, path B)
REG_S0S1_PATH_SWITCH = 0x0948       # regs.h:1009

# ---- CCK (regs.h:1014+) ---------------------------------------------
REG_CCK0_SYSTEM = 0x0A00            # regs.h:1014
CCK0_SIDEBAND = 1 << 4
REG_CCK_PD_THRESH = 0x0A0A          # regs.h:1039
CCK_PD_TYPE1_LV1_TH = 0x83

# ---- OFDM (regs.h:1056+) --------------------------------------------
REG_OFDM0_TRX_PATH_ENABLE = 0x0C04  # regs.h:1056
OFDM_RF_PATH_RX_MASK = 0x0F
OFDM_RF_PATH_RX_A = 1 << 0
OFDM_RF_PATH_TX_MASK = 0xF0
OFDM_RF_PATH_TX_A = 1 << 4
REG_OFDM0_RX_D_SYNC_PATH = 0x0C40   # regs.h:1080
REG_OFDM1_CFO_TRACKING = 0x0D2C     # regs.h:1126
REG_OFDM1_CSI_FIX_MASK1 = 0x0D40    # regs.h:1128
REG_OFDM1_CSI_FIX_MASK2 = 0x0D44    # regs.h:1129

# ---- TX power AGC (regs.h:1133+) ------------------------------------
REG_TX_AGC_A_RATE18_06 = 0x0E00
REG_TX_AGC_A_RATE54_24 = 0x0E04
REG_TX_AGC_A_CCK1_MCS32 = 0x0E08
REG_TX_AGC_A_MCS03_MCS00 = 0x0E10
REG_TX_AGC_A_MCS07_MCS04 = 0x0E14
REG_TX_AGC_A_MCS11_MCS08 = 0x0E18
REG_TX_AGC_A_MCS15_MCS12 = 0x0E1C
REG_TX_AGC_B_CCK11_A_CCK2_11 = 0x086C  # regs.h:939

# ---- Data / bandwidth (regs.h:613+) ---------------------------------
REG_DATA_SUBCHANNEL = 0x0483        # regs.h:613

# ---- RF6052 register addresses (regs.h:1308+) ------------------------
RF6052_REG_AC = 0x00
RF6052_REG_IQADJ_G1 = 0x01
RF6052_REG_MODE_AG = 0x18
MODE_AG_CHANNEL_MASK = 0x03FF
MODE_AG_BW_20MHZ_8723B = (3 << 10)
MODE_AG_BW_40MHZ_8723B = (1 << 10)
RF6052_REG_RX_G2 = 0x1B
RF6052_REG_RX_BB2 = 0x1C
RF6052_REG_GAIN_CCA = 0xDF
RF6052_REG_RXG_MIX_SWBW = 0x87
RF6052_REG_S0S1 = 0xB0
RF6052_REG_T_METER_8723B = 0x42
RF6052_REG_UNKNOWN_55 = 0x55

# ---- EFUSE (regs.h:120+, rtl8xxxu.h:87-91) ---------------------------
REG_9346CR = 0x000A                 # regs.h:59 (EEPROM/EFUSE boot cfg)
EEPROM_BOOT = 1 << 4                # regs.h:60
EEPROM_ENABLE = 1 << 5              # regs.h:61
REG_EFUSE_ACCESS = 0x00CF           # regs.h:300
EFUSE_ACCESS_ENABLE = 0x69          # regs.h:132
EFUSE_ACCESS_DISABLE = 0x00         # regs.h:133
RTL8XXXU_MAX_REG_POLL = 1000
EFUSE_MAP_LEN = 512                 # rtl8xxxu.h:87
EFUSE_REAL_CONTENT_LEN_8723A = 512  # rtl8xxxu.h:89
EFUSE_MAX_WORD_UNIT = 4             # rtl8xxxu.h:91
EFUSE_RTL_ID = 0x8129               # 8188f.c:710 (cpu_to_le16)

# EFUSE tx-power sanity bounds (8188f.c:700-702, 721-731)
TX_POWER_INDEX_MAX = 0x3F
TX_POWER_INDEX_DEFAULT_CCK = 0x22
TX_POWER_INDEX_DEFAULT_HT40 = 0x27
