"""MT7601U register addresses, bitfields, vendor requests, and accessors.

Generated from driver_sources/mt7601u-source-v7.2/ (tag v7.2); every value carries
its [SRC] file:line. Do NOT hand-edit: re-run scripts/chips/mt7601u/gen_constants.py instead.
"""
from __future__ import annotations

HZ = 1000            # Linux CONFIG_HZ, the unit every *_INTERVAL below counts in
PAGE_SIZE = 0x1000   # Linux page size on x86_64

# USB endpoint roles, positional indices into in_eps/out_eps (usb.c:28-42).
# The addresses themselves come from the descriptor at claim time.
MT_EP_IN_PKT_RX = 0
MT_EP_IN_CMD_RESP = 1
MT_EP_OUT_INBAND_CMD = 0
MT_EP_OUT_AC_BK = 1
MT_EP_OUT_AC_BE = 2
MT_EP_OUT_AC_VI = 3
MT_EP_OUT_AC_VO = 4
MT_EP_OUT_HCCA = 5

MT7601U_FIRMWARE = 'mt7601u.bin'   # usb.h:11
MT_VEND_BUF = 4                  # usb.h:19 sizeof(__le32)

# enum mt_vendor_req (usb.h:21-26) and enum mt_vendor_req's reset value (usb.h:17).
MT_VEND_DEV_MODE = 1            # usb.h:22
MT_VEND_WRITE = 2               # usb.h:23
MT_VEND_MULTI_READ = 7          # usb.h:24
MT_VEND_WRITE_FCE = 0x42        # usb.h:25
MT_VEND_DEV_MODE_RESET = 1      # usb.h:17
MT_VEND_REQ_MAX_RETRY = 10      # usb.h:14
MT_VEND_REQ_TOUT_MS = 300       # usb.h:15

# enum mt7601u_eeprom_access_modes (eeprom.h:70-73): how efuse_read addresses the array.
MT_EE_READ = 0
MT_EE_PHYSICAL_READ = 1

# Struct field offsets the EEPROM code indexes with (eeprom.h:75-92).
N_CHAN_PWR = 14            # len(dev->ee->chan_pwr), eeprom.h:100
N_RATE_POWER_GROUPS = 5    # the `for (i = 0; i < 5; i++)` loop, eeprom.c:317
MAX_PWR = 0x3f             # s6_validate's GENMASK(5, 0), eeprom.h:118
S6_SIGN_BIT = 1 << 5       # BIT(5) in s6_to_int, eeprom.h:127
S6_MODULUS = 1 << 6        # BIT(6) in s6_to_int, eeprom.h:128


def _field_prep(field_mask: int, value: int) -> int:
    """FIELD_PREP: shift value into the field the mask describes."""
    shift = (field_mask & -field_mask).bit_length() - 1
    width = bin(field_mask >> shift).count('1')
    return (value & ((1 << width) - 1)) << shift


def _field_get(field_mask: int, value: int) -> int:
    """FIELD_GET: extract the field the mask describes from value."""
    shift = (field_mask & -field_mask).bit_length() - 1
    return (value & field_mask) >> shift


def _two_base(index: int, limit: int, low: int, high: int) -> int:
    """A C ternary `i < limit ? low : high` over two register banks."""
    return low if index < limit else high

BBP_R47_FLAG = 0x7                          # [SRC] phy.c:467
BBP_R47_F_TEMP = 4                          # [SRC] phy.c:471

# ============================================================
# Register addresses and bitfields — [SRC] regs.h
# ============================================================
MT_ASIC_VERSION = 0x0000                        # [SRC] regs.h:12
MT76XX_REV_E3 = 0x22                            # [SRC] regs.h:14
MT76XX_REV_E4 = 0x33                            # [SRC] regs.h:15
MT_CMB_CTRL = 0x0020                            # [SRC] regs.h:17
MT_CMB_CTRL_XTAL_RDY = 1 << 22                  # [SRC] regs.h:18
MT_CMB_CTRL_PLL_LD = 1 << 23                    # [SRC] regs.h:19
MT_EFUSE_CTRL = 0x0024                          # [SRC] regs.h:21
MT_EFUSE_CTRL_AOUT = 0x3f                       # [SRC] regs.h:22
MT_EFUSE_CTRL_MODE = 0xc0                       # [SRC] regs.h:23
MT_EFUSE_CTRL_LDO_OFF_TIME = 0x3f00             # [SRC] regs.h:24
MT_EFUSE_CTRL_LDO_ON_TIME = 0xc000              # [SRC] regs.h:25
MT_EFUSE_CTRL_AIN = 0x3ff0000                   # [SRC] regs.h:26
MT_EFUSE_CTRL_KICK = 1 << 30                    # [SRC] regs.h:27
MT_EFUSE_CTRL_SEL = 1 << 31                     # [SRC] regs.h:28
MT_EFUSE_DATA_BASE = 0x0028                     # [SRC] regs.h:30
MT_COEXCFG0 = 0x0040                            # [SRC] regs.h:33
MT_COEXCFG0_COEX_EN = 1 << 0                    # [SRC] regs.h:34
MT_WLAN_FUN_CTRL = 0x0080                       # [SRC] regs.h:36
MT_WLAN_FUN_CTRL_WLAN_EN = 1 << 0               # [SRC] regs.h:37
MT_WLAN_FUN_CTRL_WLAN_CLK_EN = 1 << 1           # [SRC] regs.h:38
MT_WLAN_FUN_CTRL_WLAN_RESET_RF = 1 << 2         # [SRC] regs.h:39
MT_WLAN_FUN_CTRL_WLAN_RESET = 1 << 3            # [SRC] regs.h:41  # MT76x0
MT_WLAN_FUN_CTRL_CSR_F20M_CKEN = 1 << 3         # [SRC] regs.h:42  # MT76x2
MT_WLAN_FUN_CTRL_PCIE_CLK_REQ = 1 << 4          # [SRC] regs.h:44
MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL = 1 << 5        # [SRC] regs.h:45
MT_WLAN_FUN_CTRL_INV_ANT_SEL = 1 << 6           # [SRC] regs.h:46
MT_WLAN_FUN_CTRL_WAKE_HOST = 1 << 7             # [SRC] regs.h:47
MT_WLAN_FUN_CTRL_THERM_RST = 1 << 8             # [SRC] regs.h:49  # MT76x2
MT_WLAN_FUN_CTRL_THERM_CKEN = 1 << 9            # [SRC] regs.h:50  # MT76x2
MT_WLAN_FUN_CTRL_GPIO_IN = 0xff00               # [SRC] regs.h:52  # MT76x0
MT_WLAN_FUN_CTRL_GPIO_OUT = 0xff0000            # [SRC] regs.h:53  # MT76x0
MT_WLAN_FUN_CTRL_GPIO_OUT_EN = 0xff000000       # [SRC] regs.h:54  # MT76x0
MT_XO_CTRL0 = 0x0100                            # [SRC] regs.h:56
MT_XO_CTRL1 = 0x0104                            # [SRC] regs.h:57
MT_XO_CTRL2 = 0x0108                            # [SRC] regs.h:58
MT_XO_CTRL3 = 0x010c                            # [SRC] regs.h:59
MT_XO_CTRL4 = 0x0110                            # [SRC] regs.h:60
MT_XO_CTRL5 = 0x0114                            # [SRC] regs.h:62
MT_XO_CTRL5_C2_VAL = 0x7f00                     # [SRC] regs.h:63
MT_XO_CTRL6 = 0x0118                            # [SRC] regs.h:65
MT_XO_CTRL6_C2_CTRL = 0x7f00                    # [SRC] regs.h:66
MT_XO_CTRL7 = 0x011c                            # [SRC] regs.h:68
MT_WLAN_MTC_CTRL = 0x10148                      # [SRC] regs.h:70
MT_WLAN_MTC_CTRL_MTCMOS_PWR_UP = 1 << 0         # [SRC] regs.h:71
MT_WLAN_MTC_CTRL_PWR_ACK = 1 << 12              # [SRC] regs.h:72
MT_WLAN_MTC_CTRL_PWR_ACK_S = 1 << 13            # [SRC] regs.h:73
MT_WLAN_MTC_CTRL_BBP_MEM_PD = 0xf0000           # [SRC] regs.h:74
MT_WLAN_MTC_CTRL_PBF_MEM_PD = 1 << 20           # [SRC] regs.h:75
MT_WLAN_MTC_CTRL_FCE_MEM_PD = 1 << 21           # [SRC] regs.h:76
MT_WLAN_MTC_CTRL_TSO_MEM_PD = 1 << 22           # [SRC] regs.h:77
MT_WLAN_MTC_CTRL_BBP_MEM_RB = 1 << 24           # [SRC] regs.h:78
MT_WLAN_MTC_CTRL_PBF_MEM_RB = 1 << 25           # [SRC] regs.h:79
MT_WLAN_MTC_CTRL_FCE_MEM_RB = 1 << 26           # [SRC] regs.h:80
MT_WLAN_MTC_CTRL_TSO_MEM_RB = 1 << 27           # [SRC] regs.h:81
MT_WLAN_MTC_CTRL_STATE_UP = 1 << 28             # [SRC] regs.h:82
MT_INT_SOURCE_CSR = 0x0200                      # [SRC] regs.h:84
MT_INT_MASK_CSR = 0x0204                        # [SRC] regs.h:85
MT_INT_RX_DONE_ALL = 0x3                        # [SRC] regs.h:88
MT_INT_TX_DONE_ALL = 0x3ff0                     # [SRC] regs.h:89
MT_INT_RX_COHERENT = 1 << 16                    # [SRC] regs.h:91
MT_INT_TX_COHERENT = 1 << 17                    # [SRC] regs.h:92
MT_INT_ANY_COHERENT = 1 << 18                   # [SRC] regs.h:93
MT_INT_MCU_CMD = 1 << 19                        # [SRC] regs.h:94
MT_INT_TBTT = 1 << 20                           # [SRC] regs.h:95
MT_INT_PRE_TBTT = 1 << 21                       # [SRC] regs.h:96
MT_INT_TX_STAT = 1 << 22                        # [SRC] regs.h:97
MT_INT_AUTO_WAKEUP = 1 << 23                    # [SRC] regs.h:98
MT_INT_GPTIMER = 1 << 24                        # [SRC] regs.h:99
MT_INT_RXDELAYINT = 1 << 26                     # [SRC] regs.h:100
MT_INT_TXDELAYINT = 1 << 27                     # [SRC] regs.h:101
MT_WPDMA_GLO_CFG = 0x0208                       # [SRC] regs.h:103
MT_WPDMA_GLO_CFG_TX_DMA_EN = 1 << 0             # [SRC] regs.h:104
MT_WPDMA_GLO_CFG_TX_DMA_BUSY = 1 << 1           # [SRC] regs.h:105
MT_WPDMA_GLO_CFG_RX_DMA_EN = 1 << 2             # [SRC] regs.h:106
MT_WPDMA_GLO_CFG_RX_DMA_BUSY = 1 << 3           # [SRC] regs.h:107
MT_WPDMA_GLO_CFG_DMA_BURST_SIZE = 0x30          # [SRC] regs.h:108
MT_WPDMA_GLO_CFG_TX_WRITEBACK_DONE = 1 << 6     # [SRC] regs.h:109
MT_WPDMA_GLO_CFG_BIG_ENDIAN = 1 << 7            # [SRC] regs.h:110
MT_WPDMA_GLO_CFG_HDR_SEG_LEN = 0xff00           # [SRC] regs.h:111
MT_WPDMA_GLO_CFG_CLK_GATE_DIS = 1 << 30         # [SRC] regs.h:112
MT_WPDMA_GLO_CFG_RX_2B_OFFSET = 1 << 31         # [SRC] regs.h:113
MT_WPDMA_RST_IDX = 0x020c                       # [SRC] regs.h:115
MT_WPDMA_DELAY_INT_CFG = 0x0210                 # [SRC] regs.h:117
MT_WMM_AIFSN = 0x0214                           # [SRC] regs.h:119
MT_WMM_AIFSN_MASK = 0xf                         # [SRC] regs.h:120
MT_WMM_CWMIN = 0x0218                           # [SRC] regs.h:123
MT_WMM_CWMIN_MASK = 0xf                         # [SRC] regs.h:124
MT_WMM_CWMAX = 0x021c                           # [SRC] regs.h:127
MT_WMM_CWMAX_MASK = 0xf                         # [SRC] regs.h:128
MT_WMM_TXOP_BASE = 0x0220                       # [SRC] regs.h:131
MT_WMM_TXOP_MASK = 0xffff                       # [SRC] regs.h:134
MT_FCE_DMA_ADDR = 0x0230                        # [SRC] regs.h:136
MT_FCE_DMA_LEN = 0x0234                         # [SRC] regs.h:137
MT_USB_DMA_CFG = 0x238                          # [SRC] regs.h:139
MT_USB_DMA_CFG_RX_BULK_AGG_TOUT = 0xff          # [SRC] regs.h:140
MT_USB_DMA_CFG_RX_BULK_AGG_LMT = 0xff00         # [SRC] regs.h:141
MT_USB_DMA_CFG_PHY_CLR = 1 << 16                # [SRC] regs.h:142
MT_USB_DMA_CFG_TX_CLR = 1 << 19                 # [SRC] regs.h:143
MT_USB_DMA_CFG_TXOP_HALT = 1 << 20              # [SRC] regs.h:144
MT_USB_DMA_CFG_RX_BULK_AGG_EN = 1 << 21         # [SRC] regs.h:145
MT_USB_DMA_CFG_RX_BULK_EN = 1 << 22             # [SRC] regs.h:146
MT_USB_DMA_CFG_TX_BULK_EN = 1 << 23             # [SRC] regs.h:147
MT_USB_DMA_CFG_UDMA_RX_WL_DROP = 1 << 25        # [SRC] regs.h:148
MT_USB_DMA_CFG_EP_OUT_VALID = 0x38000000        # [SRC] regs.h:149
MT_USB_DMA_CFG_RX_BUSY = 1 << 30                # [SRC] regs.h:150
MT_USB_DMA_CFG_TX_BUSY = 1 << 31                # [SRC] regs.h:151
MT_TSO_CTRL = 0x0250                            # [SRC] regs.h:153
MT_HEADER_TRANS_CTRL_REG = 0x0260               # [SRC] regs.h:154
MT_US_CYC_CFG = 0x02a4                          # [SRC] regs.h:156
MT_US_CYC_CNT = 0xff                            # [SRC] regs.h:157
MT_TX_RING_BASE = 0x0300                        # [SRC] regs.h:159
MT_RX_RING_BASE = 0x03c0                        # [SRC] regs.h:160
MT_RING_SIZE = 0x10                             # [SRC] regs.h:161
MT_TX_HW_QUEUE_MCU = 8                          # [SRC] regs.h:163
MT_TX_HW_QUEUE_MGMT = 9                         # [SRC] regs.h:164
MT_PBF_SYS_CTRL = 0x0400                        # [SRC] regs.h:166
MT_PBF_SYS_CTRL_MCU_RESET = 1 << 0              # [SRC] regs.h:167
MT_PBF_SYS_CTRL_DMA_RESET = 1 << 1              # [SRC] regs.h:168
MT_PBF_SYS_CTRL_MAC_RESET = 1 << 2              # [SRC] regs.h:169
MT_PBF_SYS_CTRL_PBF_RESET = 1 << 3              # [SRC] regs.h:170
MT_PBF_SYS_CTRL_ASY_RESET = 1 << 4              # [SRC] regs.h:171
MT_PBF_CFG = 0x0404                             # [SRC] regs.h:173
MT_PBF_CFG_TX0Q_EN = 1 << 0                     # [SRC] regs.h:174
MT_PBF_CFG_TX1Q_EN = 1 << 1                     # [SRC] regs.h:175
MT_PBF_CFG_TX2Q_EN = 1 << 2                     # [SRC] regs.h:176
MT_PBF_CFG_TX3Q_EN = 1 << 3                     # [SRC] regs.h:177
MT_PBF_CFG_RX0Q_EN = 1 << 4                     # [SRC] regs.h:178
MT_PBF_CFG_RX_DROP_EN = 1 << 8                  # [SRC] regs.h:179
MT_PBF_TX_MAX_PCNT = 0x0408                     # [SRC] regs.h:181
MT_PBF_RX_MAX_PCNT = 0x040c                     # [SRC] regs.h:182
MT_BCN_OFFSET_BASE = 0x041c                     # [SRC] regs.h:184
MT_RXQ_STA = 0x0430                             # [SRC] regs.h:187
MT_TXQ_STA = 0x0434                             # [SRC] regs.h:188
# The two teardown drains at init.c:272-276 and :285-296 read three page-count registers
# regs.h never names, as bare literals. Named here by address so the citation carries them.
MT_PCNT_0438 = 0x0438                           # [SRC] init.c:273
MT_PCNT_0A30 = 0x0a30                           # [SRC] init.c:274
MT_PCNT_0A34 = 0x0a34                           # [SRC] init.c:275
MT_RF_CSR_CFG = 0x0500                          # [SRC] regs.h:190
MT_RF_CSR_CFG_DATA = 0xff                       # [SRC] regs.h:191
MT_RF_CSR_CFG_REG_ID = 0x3f00                   # [SRC] regs.h:192
MT_RF_CSR_CFG_REG_BANK = 0x3c000                # [SRC] regs.h:193
MT_RF_CSR_CFG_WR = 1 << 30                      # [SRC] regs.h:194
MT_RF_CSR_CFG_KICK = 1 << 31                    # [SRC] regs.h:195
MT_RF_BYPASS_0 = 0x0504                         # [SRC] regs.h:197
MT_RF_BYPASS_1 = 0x0508                         # [SRC] regs.h:198
MT_RF_SETTING_0 = 0x050c                        # [SRC] regs.h:199
MT_RF_DATA_WRITE = 0x0524                       # [SRC] regs.h:201
MT_RF_CTRL = 0x0528                             # [SRC] regs.h:203
MT_RF_CTRL_ADDR = 0xfff                         # [SRC] regs.h:204
MT_RF_CTRL_WRITE = 1 << 12                      # [SRC] regs.h:205
MT_RF_CTRL_BUSY = 1 << 13                       # [SRC] regs.h:206
MT_RF_CTRL_IDX = 1 << 16                        # [SRC] regs.h:207
MT_RF_DATA_READ = 0x052c                        # [SRC] regs.h:209
MT_FCE_PSE_CTRL = 0x0800                        # [SRC] regs.h:211
MT_FCE_PARAMETERS = 0x0804                      # [SRC] regs.h:212
MT_FCE_CSO = 0x0808                             # [SRC] regs.h:213
MT_FCE_L2_STUFF = 0x080c                        # [SRC] regs.h:215
MT_FCE_L2_STUFF_HT_L2_EN = 1 << 0               # [SRC] regs.h:216
MT_FCE_L2_STUFF_QOS_L2_EN = 1 << 1              # [SRC] regs.h:217
MT_FCE_L2_STUFF_RX_STUFF_EN = 1 << 2            # [SRC] regs.h:218
MT_FCE_L2_STUFF_TX_STUFF_EN = 1 << 3            # [SRC] regs.h:219
MT_FCE_L2_STUFF_WR_MPDU_LEN_EN = 1 << 4         # [SRC] regs.h:220
MT_FCE_L2_STUFF_MVINV_BSWAP = 1 << 5            # [SRC] regs.h:221
MT_FCE_L2_STUFF_TS_CMD_QSEL_EN = 0xff00         # [SRC] regs.h:222
MT_FCE_L2_STUFF_TS_LEN_EN = 0xff0000            # [SRC] regs.h:223
MT_FCE_L2_STUFF_OTHER_PORT = 0x3000000          # [SRC] regs.h:224
MT_FCE_WLAN_FLOW_CONTROL1 = 0x0824              # [SRC] regs.h:226
MT_TX_CPU_FROM_FCE_BASE_PTR = 0x09a0            # [SRC] regs.h:228
MT_TX_CPU_FROM_FCE_MAX_COUNT = 0x09a4           # [SRC] regs.h:229
MT_TX_CPU_FROM_FCE_CPU_DESC_IDX = 0x09a8        # [SRC] regs.h:230
MT_FCE_PDMA_GLOBAL_CONF = 0x09c4                # [SRC] regs.h:232
MT_PAUSE_ENABLE_CONTROL1 = 0x0a38               # [SRC] regs.h:234
MT_FCE_SKIP_FS = 0x0a6c                         # [SRC] regs.h:236
MT_MAC_CSR0 = 0x1000                            # [SRC] regs.h:238
MT_MAC_SYS_CTRL = 0x1004                        # [SRC] regs.h:240
MT_MAC_SYS_CTRL_RESET_CSR = 1 << 0              # [SRC] regs.h:241
MT_MAC_SYS_CTRL_RESET_BBP = 1 << 1              # [SRC] regs.h:242
MT_MAC_SYS_CTRL_ENABLE_TX = 1 << 2              # [SRC] regs.h:243
MT_MAC_SYS_CTRL_ENABLE_RX = 1 << 3              # [SRC] regs.h:244
MT_MAC_ADDR_DW0 = 0x1008                        # [SRC] regs.h:246
MT_MAC_ADDR_DW1 = 0x100c                        # [SRC] regs.h:247
MT_MAC_ADDR_DW1_U2ME_MASK = 0xff0000            # [SRC] regs.h:248
MT_MAC_BSSID_DW0 = 0x1010                       # [SRC] regs.h:250
MT_MAC_BSSID_DW1 = 0x1014                       # [SRC] regs.h:251
MT_MAC_BSSID_DW1_ADDR = 0xffff                  # [SRC] regs.h:252
MT_MAC_BSSID_DW1_MBSS_MODE = 0x30000            # [SRC] regs.h:253
MT_MAC_BSSID_DW1_MBEACON_N = 0x1c0000           # [SRC] regs.h:254
MT_MAC_BSSID_DW1_MBSS_LOCAL_BIT = 1 << 21       # [SRC] regs.h:255
MT_MAC_BSSID_DW1_MBSS_MODE_B2 = 1 << 22         # [SRC] regs.h:256
MT_MAC_BSSID_DW1_MBEACON_N_B3 = 1 << 23         # [SRC] regs.h:257
MT_MAC_BSSID_DW1_MBSS_IDX_BYTE = 0x7000000      # [SRC] regs.h:258
MT_MAX_LEN_CFG = 0x1018                         # [SRC] regs.h:260
MT_MAX_LEN_CFG_AMPDU = 0x3000                   # [SRC] regs.h:261
MT_BBP_CSR_CFG = 0x101c                         # [SRC] regs.h:263
MT_BBP_CSR_CFG_VAL = 0xff                       # [SRC] regs.h:264
MT_BBP_CSR_CFG_REG_NUM = 0xff00                 # [SRC] regs.h:265
MT_BBP_CSR_CFG_READ = 1 << 16                   # [SRC] regs.h:266
MT_BBP_CSR_CFG_BUSY = 1 << 17                   # [SRC] regs.h:267
MT_BBP_CSR_CFG_PAR_DUR = 1 << 18                # [SRC] regs.h:268
MT_BBP_CSR_CFG_RW_MODE = 1 << 19                # [SRC] regs.h:269
MT_AMPDU_MAX_LEN_20M1S = 0x1030                 # [SRC] regs.h:271
MT_AMPDU_MAX_LEN_20M2S = 0x1034                 # [SRC] regs.h:272
MT_AMPDU_MAX_LEN_40M1S = 0x1038                 # [SRC] regs.h:273
MT_AMPDU_MAX_LEN_40M2S = 0x103c                 # [SRC] regs.h:274
MT_AMPDU_MAX_LEN = 0x1040                       # [SRC] regs.h:275
MT_WCID_DROP_BASE = 0x106c                      # [SRC] regs.h:277
MT_BCN_BYPASS_MASK = 0x108c                     # [SRC] regs.h:281
MT_MAC_APC_BSSID_BASE = 0x1090                  # [SRC] regs.h:283
MT_MAC_APC_BSSID_H_ADDR = 0xffff                # [SRC] regs.h:286
MT_MAC_APC_BSSID0_H_EN = 1 << 16                # [SRC] regs.h:287
MT_XIFS_TIME_CFG = 0x1100                       # [SRC] regs.h:289
MT_XIFS_TIME_CFG_CCK_SIFS = 0xff                # [SRC] regs.h:290
MT_XIFS_TIME_CFG_OFDM_SIFS = 0xff00             # [SRC] regs.h:291
MT_XIFS_TIME_CFG_OFDM_XIFS = 0xf0000            # [SRC] regs.h:292
MT_XIFS_TIME_CFG_EIFS = 0x1ff00000              # [SRC] regs.h:293
MT_XIFS_TIME_CFG_BB_RXEND_EN = 1 << 29          # [SRC] regs.h:294
MT_BKOFF_SLOT_CFG = 0x1104                      # [SRC] regs.h:296
MT_BKOFF_SLOT_CFG_SLOTTIME = 0xff               # [SRC] regs.h:297
MT_BKOFF_SLOT_CFG_CC_DELAY = 0xf00              # [SRC] regs.h:298
MT_BEACON_TIME_CFG = 0x1114                     # [SRC] regs.h:300
MT_BEACON_TIME_CFG_INTVAL = 0xffff              # [SRC] regs.h:301
MT_BEACON_TIME_CFG_TIMER_EN = 1 << 16           # [SRC] regs.h:302
MT_BEACON_TIME_CFG_SYNC_MODE = 0x60000          # [SRC] regs.h:303
MT_BEACON_TIME_CFG_TBTT_EN = 1 << 19            # [SRC] regs.h:304
MT_BEACON_TIME_CFG_BEACON_TX = 1 << 20          # [SRC] regs.h:305
MT_BEACON_TIME_CFG_TSF_COMP = 0xff000000        # [SRC] regs.h:306
MT_TBTT_SYNC_CFG = 0x1118                       # [SRC] regs.h:308
MT_TBTT_TIMER_CFG = 0x1124                      # [SRC] regs.h:309
MT_INT_TIMER_CFG = 0x1128                       # [SRC] regs.h:311
MT_INT_TIMER_CFG_PRE_TBTT = 0xffff              # [SRC] regs.h:312
MT_INT_TIMER_CFG_GP_TIMER = 0xffff0000          # [SRC] regs.h:313
MT_INT_TIMER_EN = 0x112c                        # [SRC] regs.h:315
MT_INT_TIMER_EN_PRE_TBTT_EN = 1 << 0            # [SRC] regs.h:316
MT_INT_TIMER_EN_GP_TIMER_EN = 1 << 1            # [SRC] regs.h:317
MT_MAC_STATUS = 0x1200                          # [SRC] regs.h:319
MT_MAC_STATUS_TX = 1 << 0                       # [SRC] regs.h:320
MT_MAC_STATUS_RX = 1 << 1                       # [SRC] regs.h:321
MT_PWR_PIN_CFG = 0x1204                         # [SRC] regs.h:323
MT_AUX_CLK_CFG = 0x120c                         # [SRC] regs.h:324
MT_BB_PA_MODE_CFG0 = 0x1214                     # [SRC] regs.h:326
MT_BB_PA_MODE_CFG1 = 0x1218                     # [SRC] regs.h:327
MT_RF_PA_MODE_CFG0 = 0x121c                     # [SRC] regs.h:328
MT_RF_PA_MODE_CFG1 = 0x1220                     # [SRC] regs.h:329
MT_RF_PA_MODE_ADJ0 = 0x1228                     # [SRC] regs.h:331
MT_RF_PA_MODE_ADJ1 = 0x122c                     # [SRC] regs.h:332
MT_DACCLK_EN_DLY_CFG = 0x1264                   # [SRC] regs.h:334
MT_EDCA_CFG_BASE = 0x1300                       # [SRC] regs.h:336
MT_EDCA_CFG_TXOP = 0xff                         # [SRC] regs.h:338
MT_EDCA_CFG_AIFSN = 0xf00                       # [SRC] regs.h:339
MT_EDCA_CFG_CWMIN = 0xf000                      # [SRC] regs.h:340
MT_EDCA_CFG_CWMAX = 0xf0000                     # [SRC] regs.h:341
MT_TX_PWR_CFG_0 = 0x1314                        # [SRC] regs.h:343
MT_TX_PWR_CFG_1 = 0x1318                        # [SRC] regs.h:344
MT_TX_PWR_CFG_2 = 0x131c                        # [SRC] regs.h:345
MT_TX_PWR_CFG_3 = 0x1320                        # [SRC] regs.h:346
MT_TX_PWR_CFG_4 = 0x1324                        # [SRC] regs.h:347
MT_TX_BAND_CFG = 0x132c                         # [SRC] regs.h:349
MT_TX_BAND_CFG_UPPER_40M = 1 << 0               # [SRC] regs.h:350
MT_TX_BAND_CFG_5G = 1 << 1                      # [SRC] regs.h:351
MT_TX_BAND_CFG_2G = 1 << 2                      # [SRC] regs.h:352
MT_HT_FBK_TO_LEGACY = 0x1384                    # [SRC] regs.h:354
MT_TX_MPDU_ADJ_INT = 0x1388                     # [SRC] regs.h:355
MT_TX_PWR_CFG_7 = 0x13d4                        # [SRC] regs.h:357
MT_TX_PWR_CFG_8 = 0x13d8                        # [SRC] regs.h:358
MT_TX_PWR_CFG_9 = 0x13dc                        # [SRC] regs.h:359
MT_TX_SW_CFG0 = 0x1330                          # [SRC] regs.h:361
MT_TX_SW_CFG1 = 0x1334                          # [SRC] regs.h:362
MT_TX_SW_CFG2 = 0x1338                          # [SRC] regs.h:363
MT_TXOP_CTRL_CFG = 0x1340                       # [SRC] regs.h:365
MT_TXOP_TRUN_EN = 0x3f                          # [SRC] regs.h:366
MT_TXOP_EXT_CCA_DLY = 0xff00                    # [SRC] regs.h:367
MT_TX_RTS_CFG = 0x1344                          # [SRC] regs.h:370
MT_TX_RTS_CFG_RETRY_LIMIT = 0xff                # [SRC] regs.h:371
MT_TX_RTS_CFG_THRESH = 0xffff00                 # [SRC] regs.h:372
MT_TX_RTS_FALLBACK = 1 << 24                    # [SRC] regs.h:373
MT_TX_TIMEOUT_CFG = 0x1348                      # [SRC] regs.h:375
MT_TX_RETRY_CFG = 0x134c                        # [SRC] regs.h:376
MT_TX_LINK_CFG = 0x1350                         # [SRC] regs.h:377
MT_HT_FBK_CFG0 = 0x1354                         # [SRC] regs.h:378
MT_HT_FBK_CFG1 = 0x1358                         # [SRC] regs.h:379
MT_LG_FBK_CFG0 = 0x135c                         # [SRC] regs.h:380
MT_LG_FBK_CFG1 = 0x1360                         # [SRC] regs.h:381
MT_CCK_PROT_CFG = 0x1364                        # [SRC] regs.h:383
MT_OFDM_PROT_CFG = 0x1368                       # [SRC] regs.h:384
MT_MM20_PROT_CFG = 0x136c                       # [SRC] regs.h:385
MT_MM40_PROT_CFG = 0x1370                       # [SRC] regs.h:386
MT_GF20_PROT_CFG = 0x1374                       # [SRC] regs.h:387
MT_GF40_PROT_CFG = 0x1378                       # [SRC] regs.h:388
MT_PROT_RATE = 0xffff                           # [SRC] regs.h:390
MT_PROT_CTRL_RTS_CTS = 1 << 16                  # [SRC] regs.h:391
MT_PROT_CTRL_CTS2SELF = 1 << 17                 # [SRC] regs.h:392
MT_PROT_NAV_SHORT = 1 << 18                     # [SRC] regs.h:393
MT_PROT_NAV_LONG = 1 << 19                      # [SRC] regs.h:394
MT_PROT_TXOP_ALLOW_CCK = 1 << 20                # [SRC] regs.h:395
MT_PROT_TXOP_ALLOW_OFDM = 1 << 21               # [SRC] regs.h:396
MT_PROT_TXOP_ALLOW_MM20 = 1 << 22               # [SRC] regs.h:397
MT_PROT_TXOP_ALLOW_MM40 = 1 << 23               # [SRC] regs.h:398
MT_PROT_TXOP_ALLOW_GF20 = 1 << 24               # [SRC] regs.h:399
MT_PROT_TXOP_ALLOW_GF40 = 1 << 25               # [SRC] regs.h:400
MT_PROT_RTS_THR_EN = 1 << 26                    # [SRC] regs.h:401
MT_PROT_RATE_CCK_11 = 0x0003                    # [SRC] regs.h:402
MT_PROT_RATE_OFDM_6 = 0x4000                    # [SRC] regs.h:403
MT_PROT_RATE_OFDM_24 = 0x4004                   # [SRC] regs.h:404
MT_PROT_RATE_DUP_OFDM_24 = 0x4084               # [SRC] regs.h:405
MT_PROT_TXOP_ALLOW_ALL = 0x3f00000              # [SRC] regs.h:406
MT_PROT_TXOP_ALLOW_BW20 = (MT_PROT_TXOP_ALLOW_ALL & ~MT_PROT_TXOP_ALLOW_MM40 & ~MT_PROT_TXOP_ALLOW_GF40)  # [SRC] regs.h:407
MT_EXP_ACK_TIME = 0x1380                        # [SRC] regs.h:411
MT_TX_PWR_CFG_0_EXT = 0x1390                    # [SRC] regs.h:413
MT_TX_PWR_CFG_1_EXT = 0x1394                    # [SRC] regs.h:414
MT_TX_FBK_LIMIT = 0x1398                        # [SRC] regs.h:416
MT_TX_FBK_LIMIT_MPDU_FBK = 0xff                 # [SRC] regs.h:417
MT_TX_FBK_LIMIT_AMPDU_FBK = 0xff00              # [SRC] regs.h:418
MT_TX_FBK_LIMIT_MPDU_UP_CLEAR = 1 << 16         # [SRC] regs.h:419
MT_TX_FBK_LIMIT_AMPDU_UP_CLEAR = 1 << 17        # [SRC] regs.h:420
MT_TX_FBK_LIMIT_RATE_LUT = 1 << 18              # [SRC] regs.h:421
MT_TX0_RF_GAIN_CORR = 0x13a0                    # [SRC] regs.h:423
MT_TX1_RF_GAIN_CORR = 0x13a4                    # [SRC] regs.h:424
MT_TX0_RF_GAIN_ATTEN = 0x13a8                   # [SRC] regs.h:425
MT_TX_ALC_CFG_0 = 0x13b0                        # [SRC] regs.h:427
MT_TX_ALC_CFG_0_CH_INIT_0 = 0x3f                # [SRC] regs.h:428
MT_TX_ALC_CFG_0_CH_INIT_1 = 0x3f00              # [SRC] regs.h:429
MT_TX_ALC_CFG_0_LIMIT_0 = 0x3f0000              # [SRC] regs.h:430
MT_TX_ALC_CFG_0_LIMIT_1 = 0x3f000000            # [SRC] regs.h:431
MT_TX_ALC_CFG_1 = 0x13b4                        # [SRC] regs.h:433
MT_TX_ALC_CFG_1_TEMP_COMP = 0x3f                # [SRC] regs.h:434
MT_TX_ALC_CFG_2 = 0x13a8                        # [SRC] regs.h:436
MT_TX_ALC_CFG_2_TEMP_COMP = 0x3f                # [SRC] regs.h:437
MT_TX0_BB_GAIN_ATTEN = 0x13c0                   # [SRC] regs.h:439
MT_TX_ALC_VGA3 = 0x13c8                         # [SRC] regs.h:441
MT_TX_PROT_CFG6 = 0x13e0                        # [SRC] regs.h:443
MT_TX_PROT_CFG7 = 0x13e4                        # [SRC] regs.h:444
MT_TX_PROT_CFG8 = 0x13e8                        # [SRC] regs.h:445
MT_PIFS_TX_CFG = 0x13ec                         # [SRC] regs.h:447
MT_RX_FILTR_CFG = 0x1400                        # [SRC] regs.h:449
MT_RX_FILTR_CFG_CRC_ERR = 1 << 0                # [SRC] regs.h:451
MT_RX_FILTR_CFG_PHY_ERR = 1 << 1                # [SRC] regs.h:452
MT_RX_FILTR_CFG_PROMISC = 1 << 2                # [SRC] regs.h:453
MT_RX_FILTR_CFG_OTHER_BSS = 1 << 3              # [SRC] regs.h:454
MT_RX_FILTR_CFG_VER_ERR = 1 << 4                # [SRC] regs.h:455
MT_RX_FILTR_CFG_MCAST = 1 << 5                  # [SRC] regs.h:456
MT_RX_FILTR_CFG_BCAST = 1 << 6                  # [SRC] regs.h:457
MT_RX_FILTR_CFG_DUP = 1 << 7                    # [SRC] regs.h:458
MT_RX_FILTR_CFG_CFACK = 1 << 8                  # [SRC] regs.h:459
MT_RX_FILTR_CFG_CFEND = 1 << 9                  # [SRC] regs.h:460
MT_RX_FILTR_CFG_ACK = 1 << 10                   # [SRC] regs.h:461
MT_RX_FILTR_CFG_CTS = 1 << 11                   # [SRC] regs.h:462
MT_RX_FILTR_CFG_RTS = 1 << 12                   # [SRC] regs.h:463
MT_RX_FILTR_CFG_PSPOLL = 1 << 13                # [SRC] regs.h:464
MT_RX_FILTR_CFG_BA = 1 << 14                    # [SRC] regs.h:465
MT_RX_FILTR_CFG_BAR = 1 << 15                   # [SRC] regs.h:466
MT_RX_FILTR_CFG_CTRL_RSV = 1 << 16              # [SRC] regs.h:467
MT_AUTO_RSP_CFG = 0x1404                        # [SRC] regs.h:469
MT_AUTO_RSP_PREAMB_SHORT = 1 << 4               # [SRC] regs.h:471
MT_LEGACY_BASIC_RATE = 0x1408                   # [SRC] regs.h:473
MT_HT_BASIC_RATE = 0x140c                       # [SRC] regs.h:474
MT_RX_PARSER_CFG = 0x1418                       # [SRC] regs.h:476
MT_RX_PARSER_RX_SET_NAV_ALL = 1 << 0            # [SRC] regs.h:477
MT_EXT_CCA_CFG = 0x141c                         # [SRC] regs.h:479
MT_EXT_CCA_CFG_CCA0 = 0x3                       # [SRC] regs.h:480
MT_EXT_CCA_CFG_CCA1 = 0xc                       # [SRC] regs.h:481
MT_EXT_CCA_CFG_CCA2 = 0x30                      # [SRC] regs.h:482
MT_EXT_CCA_CFG_CCA3 = 0xc0                      # [SRC] regs.h:483
MT_EXT_CCA_CFG_CCA_MASK = 0xf00                 # [SRC] regs.h:484
MT_EXT_CCA_CFG_ED_CCA_MASK = 0xf000             # [SRC] regs.h:485
MT_TX_SW_CFG3 = 0x1478                          # [SRC] regs.h:487
MT_PN_PAD_MODE = 0x150c                         # [SRC] regs.h:489
MT_TXOP_HLDR_ET = 0x1608                        # [SRC] regs.h:491
MT_PROT_AUTO_TX_CFG = 0x1648                    # [SRC] regs.h:493
MT_RX_STA_CNT0 = 0x1700                         # [SRC] regs.h:495
MT_RX_STA_CNT1 = 0x1704                         # [SRC] regs.h:496
MT_RX_STA_CNT2 = 0x1708                         # [SRC] regs.h:497
MT_TX_STA_CNT0 = 0x170c                         # [SRC] regs.h:498
MT_TX_STA_CNT1 = 0x1710                         # [SRC] regs.h:499
MT_TX_STA_CNT2 = 0x1714                         # [SRC] regs.h:500
MT_TX_STAT_FIFO = 0x1718                        # [SRC] regs.h:510
MT_TX_STAT_FIFO_VALID = 1 << 0                  # [SRC] regs.h:511
MT_TX_STAT_FIFO_PID_TYPE = 0x1e                 # [SRC] regs.h:512
MT_TX_STAT_FIFO_SUCCESS = 1 << 5                # [SRC] regs.h:513
MT_TX_STAT_FIFO_AGGR = 1 << 6                   # [SRC] regs.h:514
MT_TX_STAT_FIFO_ACKREQ = 1 << 7                 # [SRC] regs.h:515
MT_TX_STAT_FIFO_WCID = 0xff00                   # [SRC] regs.h:516
MT_TX_STAT_FIFO_RATE = 0xffff0000               # [SRC] regs.h:517
MT_TX_AGG_STAT = 0x171c                         # [SRC] regs.h:519
MT_TX_AGG_CNT_BASE0 = 0x1720                    # [SRC] regs.h:521
MT_MPDU_DENSITY_CNT = 0x1740                    # [SRC] regs.h:523
MT_TX_AGG_CNT_BASE1 = 0x174c                    # [SRC] regs.h:525
MT_TX_STAT_FIFO_EXT = 0x1798                    # [SRC] regs.h:531
MT_TX_STAT_FIFO_EXT_RETRY = 0xff                # [SRC] regs.h:532
MT_BBP_CORE_BASE = 0x2000                       # [SRC] regs.h:534
MT_BBP_IBI_BASE = 0x2100                        # [SRC] regs.h:535
MT_BBP_AGC_BASE = 0x2300                        # [SRC] regs.h:536
MT_BBP_TXC_BASE = 0x2400                        # [SRC] regs.h:537
MT_BBP_RXC_BASE = 0x2500                        # [SRC] regs.h:538
MT_BBP_TXO_BASE = 0x2600                        # [SRC] regs.h:539
MT_BBP_TXBE_BASE = 0x2700                       # [SRC] regs.h:540
MT_BBP_RXFE_BASE = 0x2800                       # [SRC] regs.h:541
MT_BBP_RXO_BASE = 0x2900                        # [SRC] regs.h:542
MT_BBP_DFS_BASE = 0x2a00                        # [SRC] regs.h:543
MT_BBP_TR_BASE = 0x2b00                         # [SRC] regs.h:544
MT_BBP_CAL_BASE = 0x2c00                        # [SRC] regs.h:545
MT_BBP_DSC_BASE = 0x2e00                        # [SRC] regs.h:546
MT_BBP_PFMU_BASE = 0x2f00                       # [SRC] regs.h:547
MT_BBP_CORE_R1_BW = 0x18                        # [SRC] regs.h:551
MT_BBP_AGC_R0_CTRL_CHAN = 0x300                 # [SRC] regs.h:553
MT_BBP_AGC_R0_BW = 0x7000                       # [SRC] regs.h:554
MT_BBP_AGC_LNA_GAIN = 0x3f0000                  # [SRC] regs.h:557
MT_BBP_AGC_GAIN = 0x7f00                        # [SRC] regs.h:560
MT_BBP_AGC20_RSSI0 = 0xff                       # [SRC] regs.h:562
MT_BBP_AGC20_RSSI1 = 0xff00                     # [SRC] regs.h:563
MT_BBP_TXBE_R0_CTRL_CHAN = 0x3                  # [SRC] regs.h:565
MT_WCID_ADDR_BASE = 0x1800                      # [SRC] regs.h:567
MT_SRAM_BASE = 0x4000                           # [SRC] regs.h:570
MT_WCID_KEY_BASE = 0x8000                       # [SRC] regs.h:572
MT_WCID_IV_BASE = 0xa000                        # [SRC] regs.h:575
MT_WCID_ATTR_BASE = 0xa800                      # [SRC] regs.h:578
MT_WCID_ATTR_PAIRWISE = 1 << 0                  # [SRC] regs.h:581
MT_WCID_ATTR_PKEY_MODE = 0xe                    # [SRC] regs.h:582
MT_WCID_ATTR_BSS_IDX = 0x70                     # [SRC] regs.h:583
MT_WCID_ATTR_BSS_IDX_SHIFT = 4                  # [SRC] regs.h:583 GENMASK(6, 4), for FIELD_PREP
MT_WCID_ATTR_RXWI_UDF = 0x380                   # [SRC] regs.h:584
MT_WCID_ATTR_PKEY_MODE_EXT = 1 << 10            # [SRC] regs.h:585
MT_WCID_ATTR_BSS_IDX_EXT = 1 << 11              # [SRC] regs.h:586
MT_WCID_ATTR_WAPI_MCBC = 1 << 15                # [SRC] regs.h:587
MT_WCID_ATTR_WAPI_KEYID = 0xff000000            # [SRC] regs.h:588
MT_SKEY_BASE_0 = 0xac00                         # [SRC] regs.h:590
MT_SKEY_BASE_1 = 0xb400                         # [SRC] regs.h:591
MT_SKEY_MODE_BASE_0 = 0xb000                    # [SRC] regs.h:599
MT_SKEY_MODE_BASE_1 = 0xb3f0                    # [SRC] regs.h:600
MT_SKEY_MODE_MASK = 0xf                         # [SRC] regs.h:607
MT_BEACON_BASE = 0xc000                         # [SRC] regs.h:610
MT_TEMP_SENSOR = 0x1d000                        # [SRC] regs.h:612
MT_TEMP_SENSOR_VAL = 0x7f                       # [SRC] regs.h:613
def MT_EFUSE_DATA(n) -> int:
    return MT_EFUSE_DATA_BASE + ((n) << 2)  # [SRC] regs.h:31
def MT_INT_RX_DONE(n) -> int:
    return 1 << (n)  # [SRC] regs.h:87
def MT_INT_TX_DONE(n) -> int:
    return 1 << (n + 4)  # [SRC] regs.h:90
def MT_WMM_AIFSN_SHIFT(n) -> int:
    return (n) * 4  # [SRC] regs.h:121
def MT_WMM_CWMIN_SHIFT(n) -> int:
    return (n) * 4  # [SRC] regs.h:125
def MT_WMM_CWMAX_SHIFT(n) -> int:
    return (n) * 4  # [SRC] regs.h:129
def MT_WMM_TXOP(n) -> int:
    return MT_WMM_TXOP_BASE + (((n) // 2) << 2)  # [SRC] regs.h:132
def MT_WMM_TXOP_SHIFT(n) -> int:
    return ((n) & 1) * 16  # [SRC] regs.h:133
def MT_BCN_OFFSET(n) -> int:
    return MT_BCN_OFFSET_BASE + ((n) << 2)  # [SRC] regs.h:185
def MT_WCID_DROP(n) -> int:
    return MT_WCID_DROP_BASE + ((n) >> 5) * 4  # [SRC] regs.h:278
def MT_WCID_DROP_MASK(n) -> int:
    return 1 << (n % 32)  # [SRC] regs.h:279
def MT_MAC_APC_BSSID_L(n) -> int:
    return MT_MAC_APC_BSSID_BASE + ((n) * 8)  # [SRC] regs.h:284
def MT_MAC_APC_BSSID_H(n) -> int:
    return MT_MAC_APC_BSSID_BASE + ((n) * 8 + 4)  # [SRC] regs.h:285
def MT_EDCA_CFG_AC(n) -> int:
    return MT_EDCA_CFG_BASE + ((n) << 2)  # [SRC] regs.h:337
def MT_TX_AGG_CNT(idx) -> int:
    return _two_base(idx, 8, MT_TX_AGG_CNT_BASE0 + (idx << 2), MT_TX_AGG_CNT_BASE1 + ((idx - 8) << 2))  # [SRC] regs.h:527
def MT_WCID_ADDR(n) -> int:
    return MT_WCID_ADDR_BASE + (n) * 8  # [SRC] regs.h:568
def MT_WCID_KEY(n) -> int:
    return MT_WCID_KEY_BASE + (n) * 32  # [SRC] regs.h:573
def MT_WCID_IV(n) -> int:
    return MT_WCID_IV_BASE + (n) * 8  # [SRC] regs.h:576
def MT_WCID_ATTR(n) -> int:
    return MT_WCID_ATTR_BASE + (n) * 4  # [SRC] regs.h:579
def MT_SKEY_0(bss, idx) -> int:
    return MT_SKEY_BASE_0 + (4 * bss + idx) * 32  # [SRC] regs.h:592
def MT_SKEY_1(bss, idx) -> int:
    return MT_SKEY_BASE_1 + (4 * (bss & 7) + idx) * 32  # [SRC] regs.h:594
def MT_SKEY(bss, idx) -> int:
    return MT_SKEY_1(bss, idx) if bss & 8 else MT_SKEY_0(bss, idx)  # [SRC] regs.h:596
def MT_SKEY_MODE_0(bss) -> int:
    return MT_SKEY_MODE_BASE_0 + ((bss // 2) << 2)  # [SRC] regs.h:601
def MT_SKEY_MODE_1(bss) -> int:
    return MT_SKEY_MODE_BASE_1 + ((((bss) & 7) // 2) << 2)  # [SRC] regs.h:603
def MT_SKEY_MODE(bss) -> int:
    return MT_SKEY_MODE_1(bss) if bss & 8 else MT_SKEY_MODE_0(bss)  # [SRC] regs.h:605
def MT_SKEY_MODE_SHIFT(bss, idx) -> int:
    return 4 * (idx + 4 * (bss & 1))  # [SRC] regs.h:608

# ============================================================
# Device state bits and small helpers — [SRC] mt7601u.h
# ============================================================
MT_CALIBRATE_INTERVAL = 4 * HZ                  # [SRC] mt7601u.h:22
MT_FREQ_CAL_INIT_DELAY = 30 * HZ                # [SRC] mt7601u.h:24
MT_FREQ_CAL_CHECK_INTERVAL = 10 * HZ            # [SRC] mt7601u.h:25
MT_FREQ_CAL_ADJ_INTERVAL = HZ // 2              # [SRC] mt7601u.h:26
MT_BBP_REG_VERSION = 0x00                       # [SRC] mt7601u.h:28
MT_USB_AGGR_SIZE_LIMIT = 28                     # [SRC] mt7601u.h:30  # * 1024B
MT_USB_AGGR_TIMEOUT = 0x80                      # [SRC] mt7601u.h:31  # * 33ns
MT_RX_ORDER = 3                                 # [SRC] mt7601u.h:32
MT_RX_URB_SIZE = PAGE_SIZE << MT_RX_ORDER       # [SRC] mt7601u.h:33
N_RX_ENTRIES = 16                               # [SRC] mt7601u.h:66
N_TX_ENTRIES = 64                               # [SRC] mt7601u.h:81
N_WCIDS = 128                                   # [SRC] mt7601u.h:105
MT_EE_TEMPERATURE_SLOPE = 39                    # [SRC] mt7601u.h:110
MT_FREQ_OFFSET_INVALID = -128                   # [SRC] mt7601u.h:111
def GROUP_WCID(idx) -> int:
    return N_WCIDS - 2 - idx  # [SRC] mt7601u.h:106

# ============================================================
# DMA descriptor and info-header fields — [SRC] dma.h
# ============================================================
MT_DMA_HDR_LEN = 4                              # [SRC] dma.h:13
MT_RX_INFO_LEN = 4                              # [SRC] dma.h:14
MT_FCE_INFO_LEN = 4                             # [SRC] dma.h:15
MT_DMA_HDRS = (MT_DMA_HDR_LEN + MT_RX_INFO_LEN)  # [SRC] dma.h:16
MT_TXD_INFO_LEN = 0xffff                        # [SRC] dma.h:19
MT_TXD_INFO_D_PORT = 0x38000000                 # [SRC] dma.h:20
MT_TXD_INFO_TYPE = 0xc0000000                   # [SRC] dma.h:21
MT_TXD_PKT_INFO_NEXT_VLD = 1 << 16              # [SRC] dma.h:39
MT_TXD_PKT_INFO_TX_BURST = 1 << 17              # [SRC] dma.h:40
MT_TXD_PKT_INFO_80211 = 1 << 19                 # [SRC] dma.h:41
MT_TXD_PKT_INFO_TSO = 1 << 20                   # [SRC] dma.h:42
MT_TXD_PKT_INFO_CSO = 1 << 21                   # [SRC] dma.h:43
MT_TXD_PKT_INFO_WIV = 1 << 24                   # [SRC] dma.h:44
MT_TXD_PKT_INFO_QSEL = 0x6000000                # [SRC] dma.h:45
MT_TXD_CMD_INFO_SEQ = 0xf0000                   # [SRC] dma.h:55
MT_TXD_CMD_INFO_TYPE = 0x7f00000                # [SRC] dma.h:56
MT_RXD_INFO_LEN = 0x3fff                        # [SRC] dma.h:88
MT_RXD_INFO_PCIE_INTR = 1 << 24                 # [SRC] dma.h:89
MT_RXD_INFO_QSEL = 0x6000000                    # [SRC] dma.h:90
MT_RXD_INFO_PORT = 0x38000000                   # [SRC] dma.h:91
MT_RXD_INFO_TYPE = 0xc0000000                   # [SRC] dma.h:92
MT_RXD_PKT_INFO_UDP_ERR = 1 << 16               # [SRC] dma.h:95
MT_RXD_PKT_INFO_TCP_ERR = 1 << 17               # [SRC] dma.h:96
MT_RXD_PKT_INFO_IP_ERR = 1 << 18                # [SRC] dma.h:97
MT_RXD_PKT_INFO_PKT_80211 = 1 << 19             # [SRC] dma.h:98
MT_RXD_PKT_INFO_L3L4_DONE = 1 << 20             # [SRC] dma.h:99
MT_RXD_PKT_INFO_MAC_LEN = 0xe00000              # [SRC] dma.h:100
MT_RXD_CMD_INFO_SELF_GEN = 1 << 15              # [SRC] dma.h:103
MT_RXD_CMD_INFO_CMD_SEQ = 0xf0000               # [SRC] dma.h:104
MT_RXD_CMD_INFO_EVT_TYPE = 0xf00000             # [SRC] dma.h:105

# ============================================================
# MCU message envelope — [SRC] mcu.h
# ============================================================
MT_MCU_RESET_CTL = 0x070C                       # [SRC] mcu.h:13
MT_MCU_INT_LEVEL = 0x0718                       # [SRC] mcu.h:14
MT_MCU_COM_REG0 = 0x0730                        # [SRC] mcu.h:15
MT_MCU_COM_REG1 = 0x0734                        # [SRC] mcu.h:16
MT_MCU_COM_REG2 = 0x0738                        # [SRC] mcu.h:17
MT_MCU_COM_REG3 = 0x073C                        # [SRC] mcu.h:18
MT_MCU_IVB_SIZE = 0x40                          # [SRC] mcu.h:20
MT_MCU_DLM_OFFSET = 0x80000                     # [SRC] mcu.h:21
MT_MCU_MEMMAP_WLAN = 0x00410000                 # [SRC] mcu.h:23
MT_MCU_MEMMAP_BBP = 0x40000000                  # [SRC] mcu.h:24
MT_MCU_MEMMAP_RF = 0x80000000                   # [SRC] mcu.h:25
INBAND_PACKET_MAX_LEN = 192                     # [SRC] mcu.h:27

# ============================================================
# MAC/EDCA/WMM — [SRC] mac.h
# ============================================================
MT_RXINFO_BA = 1 << 0                           # [SRC] mac.h:46
MT_RXINFO_DATA = 1 << 1                         # [SRC] mac.h:47
MT_RXINFO_NULL = 1 << 2                         # [SRC] mac.h:48
MT_RXINFO_FRAG = 1 << 3                         # [SRC] mac.h:49
MT_RXINFO_U2M = 1 << 4                          # [SRC] mac.h:50
MT_RXINFO_MULTICAST = 1 << 5                    # [SRC] mac.h:51
MT_RXINFO_BROADCAST = 1 << 6                    # [SRC] mac.h:52
MT_RXINFO_MYBSS = 1 << 7                        # [SRC] mac.h:53
MT_RXINFO_CRCERR = 1 << 8                       # [SRC] mac.h:54
MT_RXINFO_ICVERR = 1 << 9                       # [SRC] mac.h:55
MT_RXINFO_MICERR = 1 << 10                      # [SRC] mac.h:56
MT_RXINFO_AMSDU = 1 << 11                       # [SRC] mac.h:57
MT_RXINFO_HTC = 1 << 12                         # [SRC] mac.h:58
MT_RXINFO_RSSI = 1 << 13                        # [SRC] mac.h:59
MT_RXINFO_L2PAD = 1 << 14                       # [SRC] mac.h:60
MT_RXINFO_AMPDU = 1 << 15                       # [SRC] mac.h:61
MT_RXINFO_DECRYPT = 1 << 16                     # [SRC] mac.h:62
MT_RXINFO_BSSIDX3 = 1 << 17                     # [SRC] mac.h:63
MT_RXINFO_WAPI_KEY = 1 << 18                    # [SRC] mac.h:64
MT_RXINFO_PN_LEN = 0x380000                     # [SRC] mac.h:65
MT_RXINFO_SW_PKT_80211 = 1 << 22                # [SRC] mac.h:66
MT_RXINFO_TCP_SUM_BYPASS = 1 << 28              # [SRC] mac.h:67
MT_RXINFO_IP_SUM_BYPASS = 1 << 29               # [SRC] mac.h:68
MT_RXINFO_TCP_SUM_ERR = 1 << 30                 # [SRC] mac.h:69
MT_RXINFO_IP_SUM_ERR = 1 << 31                  # [SRC] mac.h:70
MT_RXWI_CTL_WCID = 0xff                         # [SRC] mac.h:72
MT_RXWI_CTL_KEY_IDX = 0x300                     # [SRC] mac.h:73
MT_RXWI_CTL_BSS_IDX = 0x1c00                    # [SRC] mac.h:74
MT_RXWI_CTL_UDF = 0xe000                        # [SRC] mac.h:75
MT_RXWI_CTL_MPDU_LEN = 0xfff0000                # [SRC] mac.h:76
MT_RXWI_CTL_TID = 0xf0000000                    # [SRC] mac.h:77
MT_RXWI_FRAG = 0xf                              # [SRC] mac.h:79
MT_RXWI_SN = 0xfff0                             # [SRC] mac.h:80
MT_RXWI_RATE_MCS = 0x7f                         # [SRC] mac.h:82
MT_RXWI_RATE_BW = 1 << 7                        # [SRC] mac.h:83
MT_RXWI_RATE_SGI = 1 << 8                       # [SRC] mac.h:84
MT_RXWI_RATE_STBC = 0x600                       # [SRC] mac.h:85
MT_RXWI_RATE_ETXBF = 1 << 11                    # [SRC] mac.h:86
MT_RXWI_RATE_SND = 1 << 12                      # [SRC] mac.h:87
MT_RXWI_RATE_ITXBF = 1 << 13                    # [SRC] mac.h:88
MT_RXWI_RATE_PHY = 0xc000                       # [SRC] mac.h:89
MT_RXWI_GAIN_RSSI_VAL = 0x3f                    # [SRC] mac.h:91
MT_RXWI_GAIN_RSSI_LNA_ID = 0xc0                 # [SRC] mac.h:92
MT_RXWI_ANT_AUX_LNA = 1 << 7                    # [SRC] mac.h:93
MT_RXWI_EANT_ENC_ANT_ID = 0xff                  # [SRC] mac.h:95
MT_TXWI_FLAGS_FRAG = 1 << 0                     # [SRC] mac.h:126
MT_TXWI_FLAGS_MMPS = 1 << 1                     # [SRC] mac.h:127
MT_TXWI_FLAGS_CFACK = 1 << 2                    # [SRC] mac.h:128
MT_TXWI_FLAGS_TS = 1 << 3                       # [SRC] mac.h:129
MT_TXWI_FLAGS_AMPDU = 1 << 4                    # [SRC] mac.h:130
MT_TXWI_FLAGS_MPDU_DENSITY = 0xe0               # [SRC] mac.h:131
MT_TXWI_FLAGS_TXOP = 0x300                      # [SRC] mac.h:132
MT_TXWI_FLAGS_CWMIN = 0x1c00                    # [SRC] mac.h:133
MT_TXWI_FLAGS_NO_RATE_FALLBACK = 1 << 13        # [SRC] mac.h:134
MT_TXWI_FLAGS_TX_RPT = 1 << 14                  # [SRC] mac.h:135
MT_TXWI_FLAGS_TX_RATE_LUT = 1 << 15             # [SRC] mac.h:136
MT_TXWI_RATE_MCS = 0x7f                         # [SRC] mac.h:138
MT_TXWI_RATE_BW = 1 << 7                        # [SRC] mac.h:139
MT_TXWI_RATE_SGI = 1 << 8                       # [SRC] mac.h:140
MT_TXWI_RATE_STBC = 0x600                       # [SRC] mac.h:141
MT_TXWI_RATE_PHY_MODE = 0xc000                  # [SRC] mac.h:142
MT_TXWI_ACK_CTL_REQ = 1 << 0                    # [SRC] mac.h:144
MT_TXWI_ACK_CTL_NSEQ = 1 << 1                   # [SRC] mac.h:145
MT_TXWI_ACK_CTL_BA_WINDOW = 0xfc                # [SRC] mac.h:146
MT_TXWI_LEN_BYTE_CNT = 0xfff                    # [SRC] mac.h:148
MT_TXWI_LEN_PKTID = 0xf000                      # [SRC] mac.h:149
MT_TXWI_CTL_TX_POWER_ADJ = 0xf                  # [SRC] mac.h:151
MT_TXWI_CTL_CHAN_CHECK_PKT = 1 << 4             # [SRC] mac.h:152
MT_TXWI_CTL_PIFS_REV = 1 << 6                   # [SRC] mac.h:153

# ============================================================
# EEPROM field offsets and bitfields — [SRC] eeprom.h
# ============================================================
MT7601U_EE_MAX_VER = 0x0d                       # [SRC] eeprom.h:12
MT7601U_EEPROM_SIZE = 256                       # [SRC] eeprom.h:13
MT7601U_DEFAULT_TX_POWER = 6                    # [SRC] eeprom.h:15
MT_EE_NIC_CONF_0_RX_PATH = 0xf                  # [SRC] eeprom.h:47
MT_EE_NIC_CONF_0_TX_PATH = 0xf0                 # [SRC] eeprom.h:48
MT_EE_NIC_CONF_0_BOARD_TYPE = 0x3000            # [SRC] eeprom.h:49
MT_EE_NIC_CONF_1_HW_RF_CTRL = 1 << 0            # [SRC] eeprom.h:51
MT_EE_NIC_CONF_1_TEMP_TX_ALC = 1 << 1           # [SRC] eeprom.h:52
MT_EE_NIC_CONF_1_LNA_EXT_2G = 1 << 2            # [SRC] eeprom.h:53
MT_EE_NIC_CONF_1_LNA_EXT_5G = 1 << 3            # [SRC] eeprom.h:54
MT_EE_NIC_CONF_1_TX_ALC_EN = 1 << 13            # [SRC] eeprom.h:55
MT_EE_NIC_CONF_2_RX_STREAM = 0xf                # [SRC] eeprom.h:57
MT_EE_NIC_CONF_2_TX_STREAM = 0xf0               # [SRC] eeprom.h:58
MT_EE_NIC_CONF_2_HW_ANTDIV = 1 << 8             # [SRC] eeprom.h:59
MT_EE_NIC_CONF_2_XTAL_OPTION = 0x600            # [SRC] eeprom.h:60
MT_EE_NIC_CONF_2_TEMP_DISABLE = 1 << 11         # [SRC] eeprom.h:61
MT_EE_NIC_CONF_2_COEX_METHOD = 0xe000           # [SRC] eeprom.h:62
def MT_EE_TX_POWER_BYRATE(i) -> int:
    return MT_EE_TX_POWER_BYRATE_BASE + (i) * 4  # [SRC] eeprom.h:64

# ============================================================
# Enum constants (C enums, implicit auto-increment resolved)
# ============================================================
MT_CIPHER_NONE = 0
MT_CIPHER_WEP40 = 1
MT_CIPHER_WEP104 = 2
MT_CIPHER_TKIP = 3
MT_CIPHER_AES_CCMP = 4
MT_CIPHER_CKIP40 = 5
MT_CIPHER_CKIP104 = 6
MT_CIPHER_CKIP128 = 7
MT_CIPHER_WAPI = 8
MT_TEMP_MODE_NORMAL = 0
MT_TEMP_MODE_HIGH = 1
MT_TEMP_MODE_LOW = 2
MT_BW_20 = 0
MT_BW_40 = 1
WLAN_PORT = 0
CPU_RX_PORT = 1
CPU_TX_PORT = 2
HOST_PORT = 3
VIRTUAL_CPU_RX_PORT = 4
VIRTUAL_CPU_TX_PORT = 5
DISCARD = 6
DMA_PACKET = 0
DMA_COMMAND = 1
MT_QSEL_MGMT = 0
MT_QSEL_HCCA = 1
MT_QSEL_EDCA = 2
MT_QSEL_EDCA_2 = 3
CMD_DONE = 0
CMD_ERROR = 1
CMD_RETRY = 2
EVENT_PWR_RSP = 3
EVENT_WOW_RSP = 4
EVENT_CARRIER_DETECT_RSP = 5
EVENT_DFS_DETECT_RSP = 6
CMD_FUN_SET_OP = 1
CMD_LOAD_CR = 2
CMD_INIT_GAIN_OP = 3
CMD_DYNC_VGA_OP = 6
CMD_TDLS_CH_SW = 7
CMD_BURST_WRITE = 8
CMD_READ_MODIFY_WRITE = 9
CMD_RANDOM_READ = 10
CMD_BURST_READ = 11
CMD_RANDOM_WRITE = 12
CMD_LED_MODE_OP = 16
CMD_POWER_SAVING_OP = 20
CMD_WOW_CONFIG = 21
CMD_WOW_QUERY = 22
CMD_WOW_FEATURE = 24
CMD_CARRIER_DETECT_OP = 28
CMD_RADOR_DETECT_OP = 29
CMD_SWITCH_CHANNEL_OP = 30
CMD_CALIBRATION_OP = 31
CMD_BEACON_OP = 32
CMD_ANTENNA_OP = 33
Q_SELECT = 1
ATOMIC_TSSI_SETTING = 5
RADIO_OFF = 48
RADIO_ON = 49
RADIO_OFF_AUTO_WAKEUP = 50
RADIO_OFF_ADVANCE = 51
RADIO_ON_ADVANCE = 52
MCU_CAL_R = 1
MCU_CAL_DCOC = 2
MCU_CAL_LC = 3
MCU_CAL_LOFT = 4
MCU_CAL_TXIQ = 5
MCU_CAL_BW = 6
MCU_CAL_DPD = 7
MCU_CAL_RXIQ = 8
MCU_CAL_TXDCOC = 9
MT_PHY_TYPE_CCK = 0
MT_PHY_TYPE_OFDM = 1
MT_PHY_TYPE_HT = 2
MT_PHY_TYPE_HT_GF = 3
MT_PHY_BW_20 = 0
MT_PHY_BW_40 = 1
MT_EE_CHIP_ID = 0
MT_EE_VERSION_FAE = 2
MT_EE_VERSION_EE = 3
MT_EE_MAC_ADDR = 4
MT_EE_NIC_CONF_0 = 52
MT_EE_NIC_CONF_1 = 54
MT_EE_COUNTRY_REGION = 57
MT_EE_FREQ_OFFSET = 58
MT_EE_NIC_CONF_2 = 66
MT_EE_LNA_GAIN = 68
MT_EE_RSSI_OFFSET = 70
MT_EE_TX_POWER_DELTA_BW40 = 80
MT_EE_TX_POWER_OFFSET = 82
MT_EE_TX_TSSI_SLOPE = 110
MT_EE_TX_TSSI_OFFSET_GROUP = 111
MT_EE_TX_TSSI_OFFSET = 118
MT_EE_TX_TSSI_TARGET_POWER = 208
MT_EE_REF_TEMP = 209
MT_EE_FREQ_OFFSET_COMPENSATION = 219
MT_EE_TX_POWER_BYRATE_BASE = 222
MT_EE_USAGE_MAP_START = 480
MT_EE_USAGE_MAP_END = 508
MT_EE_READ = 0
MT_EE_PHYSICAL_READ = 1

# ============================================================
# Derived sizes — defined above the enum fields they reference
# ============================================================
MT_EFUSE_USAGE_MAP_SIZE = (MT_EE_USAGE_MAP_END - MT_EE_USAGE_MAP_START + 1)  # [SRC] eeprom.h:67
