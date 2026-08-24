"""MT7612U / mt76x2u register + vendor-request constants.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

All register addresses and vendor request codes were derived from
``driver_sources/mt76-source-v6.18/`` and confirmed against
``driver_captures/captures_mt76x2u/capture-1.pcap`` cold-boot traffic.
"""
from wifit3.chips.products import AMBIGUOUS_MT7612U, ASUS, AVM, Edimax, HighCloud, LiteOn, Mercury, Microsoft, Netgear, TPLink

# ---------------------------------------------------------------------------
# Known VID:PIDs claimed by mt76x2u in the kernel id_table.
# [SRC] driver_sources/mt76-source-v6.18/mt76x2/usb.c:12
# ---------------------------------------------------------------------------
USB_IDS_MT76X2U = [
    (0x0b05, 0x1833, "MT7612U", None, ASUS.USB_AC54),
    (0x0b05, 0x17eb, "MT7612U", None, ASUS.USB_AC55),
    (0x0b05, 0x180b, "MT7612U", None, ASUS.USB_N53_B1),
    (0x0e8d, 0x7612, "MT7612U", None, AMBIGUOUS_MT7612U),
    (0x057c, 0x8503, "MT7612U", None, AVM.FRITZ_WLAN_AC860),
    (0x7392, 0xb711, "MT7612U", None, Edimax.EW_7722UAC),
    (0x0e8d, 0x7632, "MT7662U", None, HighCloud.HC_M7662BU1),
    (0x0471, 0x2126, "MT7612U", None, LiteOn.WN4516R),
    (0x0471, 0x7600, "MT7612U", None, LiteOn.WN4519R),
    (0x2c4e, 0x0103, "MT7612U", None, Mercury.UD13),
    (0x0846, 0x9014, "MT7632U", None, Netgear.WNDA3100V3),
    (0x0846, 0x9053, "MT7612U", None, Netgear.A6210),
    (0x045e, 0x02e6, "MT7612U", None, Microsoft.ADAPTER_E6),
    (0x045e, 0x02fe, "MT7612U", None, Microsoft.ADAPTER_FE),
    (0x2357, 0x0137, "MT7612U", None, TPLink.TL_WDN6200),
]

# ---------------------------------------------------------------------------
# Vendor request codes (enum mt76_usb_vendor_req).
# [SRC] driver_sources/mt76-source-v6.18/mt76.h:617
# ---------------------------------------------------------------------------
MT_VEND_DEV_MODE     = 0x01  # device-mode write (FW reset, IVB, ...)
MT_VEND_POWER_ON     = 0x04
MT_VEND_MULTI_WRITE  = 0x06  # default register write
MT_VEND_MULTI_READ   = 0x07  # default register read
MT_VEND_READ_EEPROM  = 0x09
MT_VEND_WRITE_FCE    = 0x42  # FCE-bus write (firmware DMA programming)
MT_VEND_WRITE_CFG    = 0x46
MT_VEND_READ_CFG     = 0x47
MT_VEND_READ_EXT     = 0x63
MT_VEND_WRITE_EXT    = 0x66
MT_VEND_FEATURE_SET  = 0x91

# Virtual-address-space markers (kernel strips before encoding wValue/wIndex).
# [SRC] mt76.h:612
MT_VEND_TYPE_EEPROM = 1 << 31
MT_VEND_TYPE_CFG    = 1 << 30
MT_VEND_TYPE_MASK   = MT_VEND_TYPE_EEPROM | MT_VEND_TYPE_CFG

# ---------------------------------------------------------------------------
# Register addresses.
# [SRC] driver_sources/mt76-source-v6.18/mt76x02_regs.h
# ---------------------------------------------------------------------------
MT_ASIC_VERSION = 0x0000  # mt76x02 ASIC version (REV_E1..E5 in low nibble)

# FCE-bus registers (target of MT_VEND_WRITE_FCE during FW upload).
# [SRC] driver_sources/mt76-source-v6.18/mt76x02_usb_mcu.c:15
MT_FCE_DMA_ADDR = 0x0230
MT_FCE_DMA_LEN  = 0x0234
MT_TX_CPU_FROM_FCE_CPU_DESC_IDX = 0x09a8

# MCU registers (polled for FW-ready signals).
# [SRC] driver_sources/mt76-source-v6.18/mt76x02_mcu.h:13
# [SRC] driver_sources/mt76-source-v6.18/mt76x2/mcu.h:13
MT_MCU_COM_REG0      = 0x0730  # main FW running latch (bit 0)
MT_MCU_CLOCK_CTL     = 0x0708  # rev>=E3 ROM-patch-applied latch (bit 0)
MT_MCU_SEMAPHORE_03  = 0x07BC  # ROM-patch semaphore (NOT used for MT7612)

# Frontend-cmd-engine config regs (programmed before FW upload).
# [SRC] mt76x02_regs.h:242,259-263
MT_FCE_PSE_CTRL              = 0x0800
MT_TX_CPU_FROM_FCE_BASE_PTR  = 0x09a0
MT_TX_CPU_FROM_FCE_MAX_COUNT = 0x09a4
MT_FCE_PDMA_GLOBAL_CONF      = 0x09c4
MT_FCE_SKIP_FS               = 0x0a6c

# USB-DMA-cfg (CFG-bus address — top bit set in virtual address).
# [SRC] mt76x02_regs.h:77-87
MT_USB_U3DMA_CFG = 0x9018  # within CFG bus
MT_USB_DMA_CFG_RX_BULK_AGG_TOUT  = 0x000000ff
MT_USB_DMA_CFG_RX_DROP_OR_PAD    = 1 << 18
MT_USB_DMA_CFG_RX_BULK_AGG_EN    = 1 << 21
MT_USB_DMA_CFG_RX_BULK_EN        = 1 << 22
MT_USB_DMA_CFG_TX_BULK_EN        = 1 << 23
MT_USB_DMA_CFG_RX_BUSY           = 1 << 30
MT_USB_DMA_CFG_TX_BUSY           = 1 << 31

# ---------------------------------------------------------------------------
# WLAN function-control / MTCMOS power gating (used by power_on / reset_wlan).
# [SRC] mt76x02_regs.h:33-104, mt76x2/init.c:56 (mt76x2_reset_wlan).
# MT_WLAN_FUN_CTRL is on the DEFAULT bus; the 0x130/0x148/etc. addresses
# referenced in mt76x2u_power_on go through MT_VEND_ADDR(CFG, ...).
# ---------------------------------------------------------------------------
MT_WLAN_FUN_CTRL                = 0x0080
MT_WLAN_FUN_CTRL_WLAN_EN        = 1 << 0
MT_WLAN_FUN_CTRL_WLAN_CLK_EN    = 1 << 1
MT_WLAN_FUN_CTRL_WLAN_RESET_RF  = 1 << 2
MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL = 1 << 5

MT_WLAN_MTC_CTRL_MTCMOS_PWR_UP  = 1 << 0
MT_WLAN_MTC_CTRL_PWR_ACK        = 1 << 12
MT_WLAN_MTC_CTRL_PWR_ACK_S      = 1 << 13
MT_WLAN_MTC_CTRL_STATE_UP       = 1 << 28

# ---------------------------------------------------------------------------
# MAC / PHY / WPDMA register addresses (default bus unless noted).
# [SRC] mt76x02_regs.h.
# ---------------------------------------------------------------------------
MT_WPDMA_GLO_CFG               = 0x0208
MT_WPDMA_GLO_CFG_TX_DMA_BUSY   = 1 << 1
MT_WPDMA_GLO_CFG_RX_DMA_BUSY   = 1 << 3
MT_WPDMA_DELAY_INT_CFG         = 0x0210

MT_WMM_AIFSN                   = 0x0214
MT_WMM_CWMIN                   = 0x0218
MT_WMM_CWMAX                   = 0x021C
MT_TSO_CTRL                    = 0x0250
MT_HEADER_TRANS_CTRL_REG       = 0x0260
MT_US_CYC_CFG                  = 0x02a4
MT_US_CYC_CNT_MASK             = 0xFF
MT_PBF_SYS_CTRL                = 0x0400
MT_PBF_CFG                     = 0x0404
MT_PBF_TX_MAX_PCNT             = 0x0408
MT_PBF_RX_MAX_PCNT             = 0x040C

MT_FCE_L2_STUFF                = 0x080C
MT_FCE_L2_STUFF_WR_MPDU_LEN_EN = 1 << 4    # [SRC] mt76x02_regs.h:251
MT_FCE_WLAN_FLOW_CONTROL1      = 0x0824

# Bit-field positions used by xtal_fixup. [SRC] mt76x02_regs.h:314,321
MT_XIFS_TIME_CFG_OFDM_SIFS_SHIFT = 8   # GENMASK(15, 8)
MT_XIFS_TIME_CFG_OFDM_SIFS_MASK  = 0xFF << 8
MT_BKOFF_SLOT_CFG_CC_DELAY_SHIFT = 8   # GENMASK(11, 8)
MT_BKOFF_SLOT_CFG_CC_DELAY_MASK  = 0xF << 8

# XTAL trim CFG-bus registers + EEPROM offsets — used by
# `mt76x2u_mac_fixup_xtal` ([SRC] mt76x2/usb_mac.c:9-60).
# Without these the chip's reference oscillator runs at the silicon
# default, not the per-board calibrated frequency → TX/RX clock drift
# that may show up as spurious retries or framing at the AP.
MT_XO_CTRL5                     = 0x0114    # CFG-bus address
MT_XO_CTRL5_C2_VAL_MASK         = 0x7F << 8 # GENMASK(14, 8)
MT_XO_CTRL5_C2_VAL_SHIFT        = 8
MT_XO_CTRL6                     = 0x0118    # CFG-bus address
MT_XO_CTRL6_C2_CTRL_MASK        = 0x7F << 8 # GENMASK(14, 8)
MT_XO_CTRL7                     = 0x011C    # CFG-bus address
MT_EE_XTAL_TRIM_1               = 0x03A
MT_EE_XTAL_TRIM_2               = 0x09E
MT_EE_NIC_CONF_2                = 0x042
MT_EE_NIC_CONF_2_XTAL_OPTION_MASK  = 0x3 << 9   # GENMASK(10, 9)
MT_EE_NIC_CONF_2_XTAL_OPTION_SHIFT = 9

MT_COEXCFG0                    = 0x0040
MT_COEXCFG0_COEX_EN            = 1 << 0
MT_EFUSE_CTRL                  = 0x0024
MT_PAUSE_ENABLE_CONTROL1       = 0x0a38

MT_MAC_SYS_CTRL                = 0x1004
MT_MAC_SYS_CTRL_RESET_CSR      = 1 << 0
MT_MAC_SYS_CTRL_RESET_BBP      = 1 << 1
MT_MAC_SYS_CTRL_ENABLE_TX      = 1 << 2
MT_MAC_SYS_CTRL_ENABLE_RX      = 1 << 3

MT_MAC_ADDR_DW0                = 0x1008
MT_MAC_ADDR_DW1                = 0x100C
MT_MAC_BSSID_DW0               = 0x1010
MT_MAC_BSSID_DW1               = 0x1014

# MAC_ADDR_DW1 / MAC_BSSID_DW1 sub-fields.
# [SRC] mt76x02_regs.h:277-285.
MT_MAC_ADDR_DW1_U2ME_MASK      = 0xFF << 16    # GENMASK(23, 16)
MT_MAC_BSSID_DW1_MBSS_MODE_MASK  = 0x3 << 16   # GENMASK(17, 16)
MT_MAC_BSSID_DW1_MBSS_MODE_SHIFT = 16
MT_MAC_BSSID_DW1_MBEACON_N_MASK  = 0x7 << 18   # GENMASK(20, 18)
MT_MAC_BSSID_DW1_MBEACON_N_SHIFT = 18
MT_MAC_BSSID_DW1_MBSS_LOCAL_BIT  = 1 << 21     # BIT(21)

# Per-vif BSSID slots — chip's hardware address-match table. Kernel
# clears 8 slots (loop runs 16× but `idx &= 7` masks down) at the end
# of `mt76x02_mac_setaddr`. [SRC] mt76x02_regs.h:306-309.
MT_MAC_APC_BSSID_BASE          = 0x1090
MT_MAC_APC_BSSID_H_ADDR_MASK   = 0xFFFF        # GENMASK(15, 0)
MT76_N_BSSID_SLOTS             = 8
MT_MAX_LEN_CFG                 = 0x1018
MT_AMPDU_MAX_LEN_20M1S         = 0x1030
MT_AMPDU_MAX_LEN_20M2S         = 0x1034

MT_XIFS_TIME_CFG               = 0x1100
MT_BKOFF_SLOT_CFG              = 0x1104
MT_TBTT_SYNC_CFG               = 0x1118
# Channel-time + beacon-time engine — [SRC] mt76x02_regs.h:323-360.
MT_CH_TIME_CFG                 = 0x110C
MT_CH_TIME_CFG_TIMER_EN        = 1 << 0
MT_CH_TIME_CFG_TX_AS_BUSY      = 1 << 1
MT_CH_TIME_CFG_RX_AS_BUSY      = 1 << 2
MT_CH_TIME_CFG_NAV_AS_BUSY     = 1 << 3
MT_CH_TIME_CFG_EIFS_AS_BUSY    = 1 << 4
MT_CH_CCA_RC_EN                = 1 << 6
MT_CH_TIME_CFG_CH_TIMER_CLR_MASK  = 0x3 << 8   # GENMASK(9, 8)
MT_CH_TIME_CFG_CH_TIMER_CLR_SHIFT = 8
MT_CH_IDLE                     = 0x1130
MT_CH_BUSY                     = 0x1134
MT_BEACON_TIME_CFG             = 0x1114
MT_BEACON_TIME_CFG_TIMER_EN    = 1 << 16
MT_BEACON_TIME_CFG_SYNC_MODE_MASK  = 0x3 << 17  # GENMASK(18, 17)
MT_BEACON_TIME_CFG_TBTT_EN     = 1 << 19
MT_BEACON_TIME_CFG_BEACON_TX   = 1 << 20
# Per-slot beacon offset table + bypass mask — kernel clears at init via
# `mt76x02_init_beacon_config` (`mt76x02_beacon.c:205`). We don't TX
# beacons, but the kernel init still does this for hardware state
# hygiene. [SRC] mt76x02_regs.h:194, 304.
MT_BCN_OFFSET_BASE             = 0x041C
MT_BCN_BYPASS_MASK             = 0x108C
N_BCN_SLOTS                    = 5
MT_MAC_STATUS                  = 0x1200
MT_MAC_STATUS_TX               = 1 << 0
MT_MAC_STATUS_RX               = 1 << 1
MT_PWR_PIN_CFG                 = 0x1204
MT_AUX_CLK_CFG                 = 0x120C
MT_DACCLK_EN_DLY_CFG           = 0x1264

# WCID (Wireless Client ID) tables — [SRC] mt76x02_regs.h:643-688.
# Per-station state: 256 entries × 4 bytes (ATTR) + 128 entries × 8 bytes (ADDR).
# Kernel clears all entries at init (usb_init.c:165-167) via
# mt76x02_mac_wcid_setup(idx, 0, NULL). Stale entries in wcid=0xFF (the
# slot the chip looks up for inject TX with wcid=0xff in TXWI) can
# corrupt the chip's rate / key-index / cipher-mode lookup → silent TX
# misbehavior even though MGMT frames go out fine.
MT_WCID_ADDR_BASE              = 0x1800
MT_WCID_ATTR_BASE              = 0xA800
MT_WCID_ATTR_BSS_IDX_MASK      = 0x7 << 4    # GENMASK(6, 4)
MT_WCID_ATTR_BSS_IDX_SHIFT     = 4
MT_WCID_ATTR_BSS_IDX_EXT       = 1 << 11     # BIT(11)
MT76_N_WCIDS                   = 256
MT76_WCID_ADDR_SLOTS           = 128    # Only entries 0-127 have ADDR storage

# Shared key tables — [SRC] mt76x02_regs.h:666-679. 16 vifs × 4 keys = 64
# slots total. Each MT_SKEY entry is 32 bytes (key + tx_mic + rx_mic).
# Cipher mode for all 4 keys of one vif packs into 16 bits of a shared
# MT_SKEY_MODE register (one register per pair of vifs).
MT_SKEY_BASE_0                 = 0xAC00     # vifs 0-7
MT_SKEY_BASE_1                 = 0xB400     # vifs 8-15
MT_SKEY_MODE_BASE_0            = 0xB000     # 4 regs, vifs 0-7 paired
MT_SKEY_MODE_BASE_1            = 0xB3F0     # 4 regs, vifs 8-15 paired
MT_SKEY_MODE_MASK              = 0xF        # 4 bits per cipher
MT76_N_VIFS                    = 16
MT76_N_KEYS_PER_VIF            = 4
MT76_SKEY_ENTRY_BYTES          = 32

# Cipher type enum — [SRC] mt76x02_regs.h:697.
MT76X02_CIPHER_NONE            = 0
MT76X02_CIPHER_WEP40           = 1
MT76X02_CIPHER_WEP104          = 2
MT76X02_CIPHER_TKIP            = 3
MT76X02_CIPHER_AES_CCMP        = 4

MT_TX_BAND_CFG                 = 0x132C
MT_TX_BAND_CFG_UPPER_40M       = 1 << 0
MT_TX_BAND_CFG_5G              = 1 << 1
MT_TX_BAND_CFG_2G              = 1 << 2

# TX antenna pin enable. [SRC] mt76x02_regs.h:392-396. Set by
# mt76x02_edcca_tx_enable(true) at the end of every channel-tune. Without
# the host writing it, MT_TX_PIN_CFG stays at boot defaults and no antenna
# pins are driven → catastrophic TX attenuation.
MT_TX_PIN_CFG                  = 0x1328
MT_TX_PIN_CFG_TXANT            = 0xF       # GENMASK(3, 0)
MT_TX_PIN_CFG_RXANT            = 0xF << 8  # GENMASK(11, 8)
MT_TX_PIN_RFTR_EN              = 1 << 16
MT_TX_PIN_TRSW_EN              = 1 << 18

# EDCCA / TX-link config bits. [SRC] mt76x02_regs.h:429, 417, 555.
MT_TX_CFACK_EN                 = 1 << 12   # in MT_TX_LINK_CFG
MT_TXOP_ED_CCA_EN              = 1 << 20   # in MT_TXOP_CTRL_CFG
MT_TXOP_HLDR_TX40M_BLK_EN      = 1 << 1    # in MT_TXOP_HLDR_ET
MT_ED_CCA_TIMER                = 0x1140    # read-to-clear CCA timer

MT_AUTO_RSP_EN                 = 1 << 0    # in MT_AUTO_RSP_CFG

# Per-band PA / RF gain config — programmed every set_channel by the kernel
# in mt76x2_phy_set_txpower_regs. [SRC] mt76x2/phy.c:45. These must be
# written: without them the chip TX's at its power-on PA default, which is
# enough for an occasional Auth/Assoc round-trip but drops sustained data
# injection (ARP replay / ChopChop / Fragmentation) at the AP.
MT_BB_PA_MODE_CFG0             = 0x1214
MT_BB_PA_MODE_CFG1             = 0x1218
MT_RF_PA_MODE_CFG0             = 0x121C
MT_RF_PA_MODE_CFG1             = 0x1220
MT_RF_PA_MODE_ADJ0             = 0x1228
MT_RF_PA_MODE_ADJ1             = 0x122C
MT_TX0_RF_GAIN_CORR            = 0x13A0
MT_TX1_RF_GAIN_CORR            = 0x13A4
MT_TX_ALC_CFG_2                = 0x13A8
MT_TX_ALC_CFG_3                = 0x13AC

MT_TX_PWR_CFG_0                = 0x1314
MT_TX_PWR_CFG_1                = 0x1318
MT_TX_PWR_CFG_2                = 0x131C
MT_TX_PWR_CFG_3                = 0x1320
MT_TX_PWR_CFG_4                = 0x1324
MT_TX_SW_CFG0                  = 0x1330
MT_TX_SW_CFG1                  = 0x1334
MT_TX_SW_CFG2                  = 0x1338
MT_TXOP_CTRL_CFG               = 0x1340
MT_TX_RTS_CFG                  = 0x1344
MT_TX_TIMEOUT_CFG              = 0x1348
MT_TX_RETRY_CFG                = 0x134C
MT_TX_LINK_CFG                 = 0x1350
MT_VHT_HT_FBK_CFG1             = 0x1358
MT_CCK_PROT_CFG                = 0x1364
MT_OFDM_PROT_CFG               = 0x1368
MT_MM20_PROT_CFG               = 0x136C
MT_MM40_PROT_CFG               = 0x1370
MT_GF20_PROT_CFG               = 0x1374
MT_GF40_PROT_CFG               = 0x1378
MT_EXP_ACK_TIME                = 0x1380
MT_HT_FBK_TO_LEGACY            = 0x1384
MT_TX_ALC_CFG_4                = 0x13C0
MT_TX_ALC_VGA3                 = 0x13C8
MT_TX_PWR_CFG_7                = 0x13D4
MT_TX_PWR_CFG_8                = 0x13D8
MT_TX_PWR_CFG_9                = 0x13DC
MT_TX_PROT_CFG6                = 0x13E0
MT_TX_PROT_CFG7                = 0x13E4
MT_TX_PROT_CFG8                = 0x13E8
MT_PIFS_TX_CFG                 = 0x13EC

MT_RX_FILTR_CFG                = 0x1400
# SET drops that frame class; CLEAR admits it. [SRC] mt76x02_regs.h:524
MT_RX_FILTR_CFG_ACK            = 1 << 10   # link-layer ACK
MT_AUTO_RSP_CFG                = 0x1404
MT_LEGACY_BASIC_RATE           = 0x1408
MT_HT_BASIC_RATE               = 0x140C
MT_HT_CTRL_CFG                 = 0x1410
MT_EXT_CCA_CFG                 = 0x141C
MT_EXT_CCA_CFG_CCA0_SHIFT      = 0
MT_EXT_CCA_CFG_CCA1_SHIFT      = 2
MT_EXT_CCA_CFG_CCA2_SHIFT      = 4
MT_EXT_CCA_CFG_CCA3_SHIFT      = 6
MT_EXT_CCA_CFG_CCA_MASK_SHIFT  = 8
MT_TX_SW_CFG3                  = 0x1478
MT_PN_PAD_MODE                 = 0x150C
MT_TXOP_HLDR_ET                = 0x1608
MT_PROT_AUTO_TX_CFG            = 0x1648

# BBP regions (default bus). Macro: MT_BBP(type, n) = base + n*4.
MT_BBP_CORE_BASE               = 0x2000
MT_BBP_IBI_BASE                = 0x2100
MT_BBP_AGC_BASE                = 0x2300
MT_BBP_TXBE_BASE               = 0x2700
MT_BBP_RXO_BASE                = 0x2900

# Bitfield positions in BBP registers (we treat these as raw masks).
MT_BBP_CORE_R1_BW_SHIFT        = 3   # GENMASK(4, 3) = bits 4..3
MT_BBP_CORE_R1_BW_MASK         = 0b11 << 3
MT_BBP_AGC_R0_CTRL_CHAN_SHIFT  = 8
MT_BBP_AGC_R0_CTRL_CHAN_MASK   = 0b11 << 8
MT_BBP_AGC_R0_BW_SHIFT         = 12
MT_BBP_AGC_R0_BW_MASK          = 0b111 << 12
MT_BBP_TXBE_R0_CTRL_CHAN_SHIFT = 0
MT_BBP_TXBE_R0_CTRL_CHAN_MASK  = 0b11

# Special-case BBP regs used directly during set_channel post-MCU.
# [SRC] mt76x2/usb_phy.c:161-168
MT_BBP_AGC_R61                 = MT_BBP_AGC_BASE + 61 * 4   # 0x23f4
MT_BBP_AGC_R7                  = MT_BBP_AGC_BASE + 7 * 4    # 0x231c
MT_BBP_AGC_R11                 = MT_BBP_AGC_BASE + 11 * 4   # 0x232c
MT_BBP_AGC_R2                  = MT_BBP_AGC_BASE + 2 * 4    # 0x2308
MT_BBP_RXO_R13                 = MT_BBP_RXO_BASE + 13 * 4   # 0x2934
MT_BBP_TXO_R4_ADDR             = 0x2600 + 4 * 4             # 0x2610 (MT_BBP_TXO_BASE)
MT_BBP_AGC_R0                  = MT_BBP_AGC_BASE + 0 * 4    # 0x2300
MT_BBP_TXBE_R5                 = MT_BBP_TXBE_BASE + 5 * 4   # 0x2714
MT_BBP_CORE_R1                 = MT_BBP_CORE_BASE + 1 * 4   # 0x2004

# BBP AGC regs used by mt76x2_apply_gain_adj. [SRC] mt76x2/phy.c:33-42.
# Regs 4/5 hold high-LNA gain (chain 0/1); regs 8/9 hold AGC gain (chain 0/1).
MT_BBP_AGC_R4                  = MT_BBP_AGC_BASE + 4 * 4    # 0x2310
MT_BBP_AGC_R5                  = MT_BBP_AGC_BASE + 5 * 4    # 0x2314
MT_BBP_AGC_R8                  = MT_BBP_AGC_BASE + 8 * 4    # 0x2320
MT_BBP_AGC_R9                  = MT_BBP_AGC_BASE + 9 * 4    # 0x2324
# Field positions inside the AGC registers. [SRC] mt76x02_regs.h:627,636.
MT_BBP_AGC_LNA_HIGH_GAIN_SHIFT = 16
MT_BBP_AGC_LNA_HIGH_GAIN_MASK  = 0x3F << 16   # GENMASK(21, 16)
MT_BBP_AGC_GAIN_SHIFT          = 8
MT_BBP_AGC_GAIN_MASK           = 0x7F << 8    # GENMASK(14, 8)

# EEPROM offsets for the RX gain machinery. [SRC] mt76x02_eeprom.h.
MT_EE_LNA_GAIN                 = 0x044
MT_EE_RSSI_OFFSET_2G_0         = 0x046
MT_EE_RSSI_OFFSET_2G_1         = 0x048
MT_EE_LNA_GAIN_5GHZ_1          = 0x049
MT_EE_RSSI_OFFSET_5G_0         = 0x04A
MT_EE_RSSI_OFFSET_5G_1         = 0x04C
MT_EE_LNA_GAIN_5GHZ_2          = 0x04D
MT_EE_RF_2G_RX_HIGH_GAIN       = 0x0F8
MT_EE_RF_5G_GRP0_1_RX_HIGH_GAIN = 0x0FA
MT_EE_RF_5G_GRP2_3_RX_HIGH_GAIN = 0x0FC
MT_EE_RF_5G_GRP4_5_RX_HIGH_GAIN = 0x0FE

# EEPROM offsets for the per-rate TX power machinery. [SRC] mt76x02_eeprom.h.
MT_EE_TX_POWER_DELTA_BW40      = 0x050
MT_EE_TX_POWER_DELTA_BW80      = 0x052
MT_EE_TX_POWER_EXT_PA_5G       = 0x054
MT_EE_TX_POWER_0_START_2G      = 0x056
MT_EE_TX_POWER_1_START_2G      = 0x05C
MT_EE_TX_POWER_0_START_5G      = 0x062
MT_EE_TX_POWER_1_START_5G      = 0x080
MT_EE_TX_POWER_CCK             = 0x0A0
MT_EE_TX_POWER_OFDM_2G_6M      = 0x0A2
MT_EE_TX_POWER_OFDM_2G_24M     = 0x0A4
MT_EE_TX_POWER_HT_MCS0         = 0x0A6
MT_EE_TX_POWER_HT_MCS4         = 0x0A8
MT_EE_TX_POWER_HT_MCS8         = 0x0AA
MT_EE_TX_POWER_HT_MCS12        = 0x0AC
MT_EE_TX_POWER_OFDM_5G_6M      = 0x0B2
MT_EE_TX_POWER_OFDM_5G_24M     = 0x0B4
MT_EE_TX_POWER_VHT_MCS8        = 0x0BE
MT_EE_RF_2G_TSSI_OFF_TXPOWER   = 0x0F6
MT_EE_RF_TEMP_COMP_SLOPE_5G    = 0x0F2
MT_EE_RF_TEMP_COMP_SLOPE_2G    = 0x0F4

# 5G TX-power table is split into 6 channel-group records, 5 bytes each.
# [SRC] mt76x02_eeprom.h:46.
MT_TX_POWER_GROUP_SIZE_5G      = 5

# MT_EE_NIC_CONF_1 flag bits. [SRC] mt76x02_eeprom.h:108-112.
MT_EE_NIC_CONF_1_TEMP_TX_ALC   = 1 << 1
MT_EE_NIC_CONF_1_LNA_EXT_2G    = 1 << 2
MT_EE_NIC_CONF_1_LNA_EXT_5G    = 1 << 3
MT_EE_NIC_CONF_1_TX_ALC_EN     = 1 << 13

# TX_ALC_CFG_0/1/2 sub-fields. [SRC] mt76x02_regs.h:488-497.
MT_TX_ALC_CFG_0                = 0x13B0
MT_TX_ALC_CFG_0_CH_INIT_0_MASK = 0x3F        # GENMASK(5, 0)
MT_TX_ALC_CFG_0_CH_INIT_1_MASK = 0x3F << 8   # GENMASK(13, 8)
MT_TX_ALC_CFG_0_CH_INIT_1_SHIFT = 8
MT_TX_ALC_CFG_1                = 0x13B4
MT_TX_ALC_CFG_1_TEMP_COMP_MASK = 0x3F        # GENMASK(5, 0)
MT_TX_ALC_CFG_2_TEMP_COMP_MASK = 0x3F        # GENMASK(5, 0)

# RX_STAT_1 CCA error counter. [SRC] mt76x02_regs.h:566.
MT_RX_STAT_1                   = 0x1704
MT_RX_STAT_1_CCA_ERRORS_MASK   = 0xFFFF      # GENMASK(15, 0)

# BBP CORE register 34 — TSSI status bit checked by tssi_compensate.
MT_BBP_CORE_R34                = MT_BBP_CORE_BASE + 34 * 4   # 0x2088

# BBP AGC reg 26 — written by update_channel_gain only on the 80 MHz-width
# path (unused at 20 MHz, but kept so the port mirrors the kernel function).
MT_BBP_AGC_R26                 = MT_BBP_AGC_BASE + 26 * 4    # 0x2368
# BBP AGC reg 35 / 37 — written by update_channel_gain.
# [SRC] mt76x2/phy.c:329, 330, 339, 340.
MT_BBP_AGC_R35                 = MT_BBP_AGC_BASE + 35 * 4    # 0x238C
MT_BBP_AGC_R37                 = MT_BBP_AGC_BASE + 37 * 4    # 0x2394
# BBP RXO reg 14 / 18 — written by update_channel_gain.
MT_BBP_RXO_R14                 = MT_BBP_RXO_BASE + 14 * 4    # 0x2938
MT_BBP_RXO_R18                 = MT_BBP_RXO_BASE + 18 * 4    # 0x2948

# kernel: MT_CALIBRATE_INTERVAL = HZ (= 1 second). [SRC] mt76x02.h:21.
MT_CALIBRATE_INTERVAL_S        = 1.0

# RSSI gain threshold sentinels used by update_channel_gain's low_gain calc.
# Kernel reads them from constant tables (mt76x02_get_rssi_gain_thresh /
# _low_rssi_gain_thresh — chip + band specific). For 2.4 GHz mt76x2 the
# kernel returns -68 / -55; for 5 GHz -64 / -51. We expose them here so the
# driver can supply them per-channel.
MT76X2_RSSI_GAIN_THRESH_2G     = -68
MT76X2_LOW_RSSI_GAIN_THRESH_2G = -55
MT76X2_RSSI_GAIN_THRESH_5G     = -64
MT76X2_LOW_RSSI_GAIN_THRESH_5G = -51

# CFG-bus power-on raw addresses (used as MT_VEND_TYPE_CFG | <addr>).
# [SRC] mt76x2/usb_init.c:28-104
MT_CFG_RF_BG                   = 0x0130  # RF analog bias / LDO/AFE/ABB/ADDA enables
MT_CFG_RF_PATCH_PWR_CTRL_14C   = 0x014C  # last write in power_on_rf_patch
MT_CFG_RF_PATCH_PWR_CTRL_1C    = 0x001C
MT_CFG_RF_PATCH_PWR_CTRL_14    = 0x0014
MT_CFG_WLAN_MTC_CTRL           = 0x0148
MT_CFG_AD_DA_PWR_DN            = 0x1204  # within CFG bus address space
MT_CFG_WLAN_FUNC_EN            = 0x0080  # equals MT_WLAN_FUN_CTRL but via CFG bus path used elsewhere
MT_CFG_BBP_SW_RESET            = 0x0064  # bit(18) BBP reset

# ---------------------------------------------------------------------------
# USB endpoint order (kernel iterates intf descriptors and assigns by index).
# [SRC] driver_sources/mt76-source-v6.18/usb.c:292
# [SRC] mt76.h:632 (enum mt76u_in_ep / mt76u_out_ep)
# [WIRE] confirmed via pyusb dump of 0e8d:7612 on dev machine.
# Endpoint order in descriptor: bulk-IN 0x84, 0x85; bulk-OUT 0x08, 0x04, 0x05,
# 0x06, 0x07, 0x09.
# ---------------------------------------------------------------------------
EP_IN_PKT_RX       = 0x84   # MT_EP_IN_PKT_RX (in_ep[0])
EP_IN_CMD_RESP     = 0x85   # MT_EP_IN_CMD_RESP (in_ep[1])
EP_OUT_INBAND_CMD  = 0x08   # MT_EP_OUT_INBAND_CMD (out_ep[0]) — FW upload + MCU
EP_OUT_AC_BE       = 0x04   # MT_EP_OUT_AC_BE (out_ep[1])
EP_OUT_AC_BK       = 0x05   # MT_EP_OUT_AC_BK (out_ep[2])
EP_OUT_AC_VI       = 0x06   # MT_EP_OUT_AC_VI (out_ep[3])
EP_OUT_AC_VO       = 0x07   # MT_EP_OUT_AC_VO (out_ep[4])
EP_OUT_HCCA        = 0x09   # MT_EP_OUT_HCCA (out_ep[5])

# ---------------------------------------------------------------------------
# Firmware blob sizes (verified by extract_mt7662_fw.py from capture-1).
# These are body sizes — no header was on the wire.
# ---------------------------------------------------------------------------
ROM_PATCH_BODY_SIZE = 26320
ILM_SIZE            = 64448
DLM_SIZE            = 17428

# Firmware upload offsets in chip SRAM.
# [SRC] driver_sources/mt76-source-v6.18/mt76x2/usb_mcu.c:17
MT76U_MCU_ILM_OFFSET       = 0x080000
MT76U_MCU_DLM_OFFSET       = 0x110000
MT76U_MCU_DLM_OFFSET_E3    = 0x110800  # rev>=E3 adds 0x800
MT76U_MCU_ROM_PATCH_OFFSET = 0x090000

MCU_FW_URB_MAX_PAYLOAD      = 0x3900   # 14592 — ILM/DLM chunk size
MCU_ROM_PATCH_MAX_PAYLOAD   = 2048     # ROM patch chunk size
MT_CMD_HDR_LEN              = 4        # mt76 info header per chunk

# ROM patch is gated by MT_MCU_SEMAPHORE_03 only on non-MT7612 silicon.
# For MT7612, `rom_protect = !is_mt7612(dev)` evaluates false → SKIP semaphore.
# [SRC] mt76x2/usb_mcu.c:59  → THIS is why MT7612 sidesteps the MT7921 wall.

# ASIC revisions (low 16 bits of MT_ASIC_VERSION readback).
# [SRC] mt76x2/mt76x2.h
MT76XX_REV_E1 = 0x22
MT76XX_REV_E3 = 0x33
MT76XX_REV_E4 = 0x44
