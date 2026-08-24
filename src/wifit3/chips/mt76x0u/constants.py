"""MT76x0U register addresses, vendor requests, and bit masks.

Every constant here is grepped verbatim from driver_sources/mt76-source-v6.18/
and cross-checked against capture-2.pcap WIRE evidence. See MT76X0U.md for
the verification table. Do NOT add a symbol here without a corresponding
[SRC] line in the kernel and (where applicable) a [WIRE] confirmation.

Per [[feedback_prefer_fork_over_base]] this module is INTENTIONALLY a sibling
of chips/mt76x2u/constants.py — duplication is fine, do NOT extract to a
shared `mt76x02_base/` module until 2+ feature-complete siblings exist.
"""
from __future__ import annotations
from wifit3.chips.products import ALFA, ASUS, AVM, AboCom, Comcast, DLink, Devolo, Edimax, IODATA, Linksys, Planex, Sitecom, TPLink, TRENDnet, Zyxel

# ============================================================
# USB device id_table — ported 1:1 from driver_sources/mt76-source-v6.18/
# mt76x0/usb.c:14-43. Format matches what test_hw_mt76x0u.py uses.
# ============================================================
USB_IDS_MT76X0U: list[tuple[int, int, str, str | None, str | None]] = [
    (0x148F, 0x7610, "MT7610U", None, None),  # Generic https://linux-hardware.org/?id=usb:148f-7610
    (0x13B1, 0x003E, "MT7610U", None, Linksys.AE6000),
    (0x0E8D, 0x7610, "MT7610U", None, ALFA.AWUS036ACHM),
    (0x7392, 0xa711, "MT7610U", None, Edimax.EW_7711MAC),
    # 148f:761a can be one of Archer T2U, Archer T2UH, or TL-WDN5200 - https://wikidevi.wi-cat.ru/TP-LINK_Archer_T2U
    (0x148F, 0x761a, "MT7610U", None, TPLink.ARCHER_T2U),  # Chosen by a dice roll.
    (0x148F, 0x760a, "MT7610U", None, None),  # (Ralink) MT7601U https://linux-hardware.org/?id=usb:148f-760a
    (0x0B05, 0x17d1, "MT7610U", None, ASUS.USB_AC51),
    (0x0B05, 0x17db, "MT7610U", None, ASUS.USB_AC50),
    (0x0DF6, 0x0075, "MT7610U", None, Sitecom.WLA_3100),
    (0x2019, 0xab31, "MT7610U", None, Planex.GW_450D),
    (0x2001, 0x3d02, "MT7610U", None, DLink.DWA_171_REV_B),
    (0x0586, 0x3425, "MT7610U", None, Zyxel.NWD6505),
    (0x07B8, 0x7610, "MT7610U", None, AboCom.AU7212),
    (0x04BB, 0x0951, "MT7610U", None, IODATA.WN_AC433UK),
    (0x057C, 0x8502, "MT7610U", None, AVM.FRITZ_WLAN_AC430),
    (0x293C, 0x5702, "MT7610U", None, Comcast.KXW02AAA),
    (0x20F4, 0x806b, "MT7610U", None, TRENDnet.TEW_806UBH),
    (0x7392, 0xc711, "MT7610U", None, Devolo.STICK),
    (0x0DF6, 0x0079, "MT7610U", None, Sitecom.WL3001),
    (0x2357, 0x0123, "MT7610U", None, TPLink.ARCHER_T2UHP),
    (0x2357, 0x010b, "MT7610U", None, TPLink.ARCHER_T2UHP),
    (0x2357, 0x0105, "MT7610U", None, TPLink.ARCHER_T1U),
    (0x0E8D, 0x7630, "MT7630U", None, None),  # Generic https://linux-hardware.org/?id=usb:0e8d-7630
    (0x0E8D, 0x7650, "MT7650U", None, TPLink.ARCHER_T2U_V2),
]

# ============================================================
# Endpoint addresses (mt76u_set_endpoints positional, verified by
# probe_hw.py descriptor dump on 0e8d:7610). Same layout as mt76x2u.
# ============================================================
EP_IN_PKT_RX        = 0x84   # in_ep[0]
EP_IN_CMD_RESP      = 0x85   # in_ep[1]
EP_OUT_INBAND_CMD   = 0x08   # out_ep[0]  <- FW upload + MCU
EP_OUT_AC_BE        = 0x04   # out_ep[1]
EP_OUT_AC_BK        = 0x05   # out_ep[2]
EP_OUT_AC_VI        = 0x06   # out_ep[3]
EP_OUT_AC_VO        = 0x07   # out_ep[4]
EP_OUT_HCCA         = 0x09   # out_ep[5]  <- MGMT TX queue (qsel=MGMT triggered by ep==HCCA)

# ---------------------------------------------------------------------------
# M6: TX descriptor bit-fields.
# ---------------------------------------------------------------------------
# DMA-info word ([SRC] mt76x02_dma.h:12-21).
MT_TXD_INFO_LEN_MASK       = 0xFFFF       # GENMASK(15, 0)
MT_TXD_INFO_LEN_SHIFT      = 0
MT_TXD_INFO_NEXT_VLD       = 1 << 16
MT_TXD_INFO_TX_BURST       = 1 << 17
MT_TXD_INFO_80211          = 1 << 19      # set: payload is 802.11 frame (not Ethernet)
MT_TXD_INFO_TSO            = 1 << 20
MT_TXD_INFO_CSO            = 1 << 21
MT_TXD_INFO_WIV            = 1 << 24      # set: "wcid invalid" - no HW key
MT_TXD_INFO_QSEL_MASK      = 0x06000000   # GENMASK(26, 25)
MT_TXD_INFO_QSEL_SHIFT     = 25
MT_TXD_INFO_DPORT_MASK     = 0x38000000   # GENMASK(29, 27)
MT_TXD_INFO_DPORT_SHIFT    = 27

# mt76_qsel - [SRC] dma.h:143-148
MT_QSEL_MGMT               = 0
MT_QSEL_HCCA               = 1
MT_QSEL_EDCA               = 2
MT_QSEL_EDCA_2             = 3

# dma_msg_port - [SRC] mt76x02_dma.h:43-51
WLAN_PORT                  = 0
CPU_RX_PORT                = 1
# CPU_TX_PORT already defined later as 2 (mt76x02_dma.h:46).
HOST_PORT                  = 3

# TXWI ack_ctl bits - [SRC] mt76x02_mac.h:131-133.
MT_TXWI_ACK_CTL_REQ        = 1 << 0       # request ACK from receiver
MT_TXWI_ACK_CTL_NSEQ       = 1 << 1       # firmware-assigned sequence
MT_TXWI_ACK_CTL_BA_WIN_MASK = 0xFC        # GENMASK(7, 2)
MT_TXWI_ACK_CTL_BA_WIN_SHIFT = 2

# TXWI sizes.
MT_TXWI_SIZE               = 20           # sizeof(struct mt76x02_txwi)

# Bottom of mt76x0u_init_hardware ([SRC] mt76x0/usb.c:171-174) — written
# at the end of init_hardware, AFTER mt76x0_init_hardware (full PHY init).
# Without these the TX engine queues frames but never actually transmits:
# TXOP_TRUN_EN gates TX opportunity grants from the EDCA scheduler.
MT_US_CYC_CFG              = 0x02a4       # [SRC] mt76x02_regs.h:166
MT_US_CYC_CNT_MASK         = 0xFF         # GENMASK(7, 0)
MT_US_CYC_CNT_DEFAULT      = 0x1e

MT_TXOP_CTRL_CFG           = 0x1340       # [SRC] mt76x02_regs.h:414
MT_TXOP_TRUN_EN_MASK       = 0x3F         # GENMASK(5, 0)
MT_TXOP_TRUN_EN_SHIFT      = 0
MT_TXOP_EXT_CCA_DLY_MASK   = 0xFF00       # GENMASK(15, 8)
MT_TXOP_EXT_CCA_DLY_SHIFT  = 8
MT_TXOP_TRUN_EN_DEFAULT    = 0x3F         # all 6 TX rings eligible for TXOP
MT_TXOP_EXT_CCA_DLY_DEFAULT = 0x58

# ============================================================
# Vendor bRequest codes — mt76 family shared.
# [SRC] driver_sources/mt76-source-v6.18/mt76x02_usb_core.c
# ============================================================
MT_VEND_DEV_MODE      = 0x01   # control: FW reset / IVB trigger via wValue
MT_VEND_MULTI_WRITE   = 0x06   # default-bus register write (4 bytes payload)
MT_VEND_MULTI_READ    = 0x07   # default-bus register read  (4 bytes payload)
MT_VEND_WRITE_FCE     = 0x42   # FCE single_wr (value in wValue, no payload)

# DEV_MODE wValue constants
MT_DEV_MODE_FW_RESET     = 0x0001
MT_DEV_MODE_IVB_TRIGGER  = 0x0012

# ============================================================
# Register addresses — [SRC] mt76x02_regs.h + mt76x02_mcu.h + mt76x0/mcu.h
# ============================================================
MT_CMB_CTRL                       = 0x0020   # [SRC] mt76x02_regs.h:14
MT_CMB_CTRL_XTAL_RDY              = 1 << 22  # BIT(22)
MT_CMB_CTRL_PLL_LD                = 1 << 23  # BIT(23)

MT_COEXCFG3                       = 0x004c   # [SRC] mt76x02_regs.h:38

# RF misc — toggle external PA enable bits for A/G band.
# [SRC] mt76x02_regs.h:209
MT_RF_MISC                        = 0x0518
MT_RF_MISC_EXT_PA_A_BAND          = 1 << 2     # BIT(2) — per phy.c:367 comment
MT_RF_MISC_EXT_PA_G_BAND          = 1 << 3     # BIT(3) — per phy.c:368 comment

MT_WLAN_FUN_CTRL                  = 0x0080   # chip on/off + reset
# MT_WLAN_FUN_CTRL bits — [SRC] mt76x02_regs.h:34-56
MT_WLAN_FUN_CTRL_WLAN_EN          = 1 << 0
MT_WLAN_FUN_CTRL_WLAN_CLK_EN      = 1 << 1
MT_WLAN_FUN_CTRL_WLAN_RESET_RF    = 1 << 2
MT_WLAN_FUN_CTRL_WLAN_RESET       = 1 << 3   # MT76x0 only (BIT(3) is CSR_F20M_CKEN on MT76x2)
MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL   = 1 << 5
MT_WLAN_FUN_CTRL_GPIO_OUT_EN      = 0xFF << 24   # GENMASK(31, 24)
MT_FCE_DMA_ADDR                   = 0x0230   # single_wr destination — chunk addr
MT_FCE_DMA_LEN                    = 0x0234   # single_wr destination — chunk len
MT_USB_DMA_CFG                    = 0x0238
MT_MCU_COM_REG0                   = 0x0730   # FW_READY = BIT(0)
MT_FCE_PSE_CTRL                   = 0x0800
MT_TX_CPU_FROM_FCE_BASE_PTR       = 0x09a0
MT_TX_CPU_FROM_FCE_MAX_COUNT      = 0x09a4
MT_TX_CPU_FROM_FCE_CPU_DESC_IDX   = 0x09a8
MT_FCE_PDMA_GLOBAL_CONF           = 0x09c4
MT_FCE_SKIP_FS                    = 0x0a6c
# ============================================================
# WPDMA — present on USB even though it's mostly a PCIe-path concept;
# kernel still polls it during init_hardware. [SRC] mt76x02_regs.h:125-135
# ============================================================
MT_WPDMA_GLO_CFG                  = 0x0208
MT_WPDMA_GLO_CFG_TX_DMA_BUSY      = 1 << 1   # BIT(1)
MT_WPDMA_GLO_CFG_RX_DMA_BUSY      = 1 << 3   # BIT(3)

# ============================================================
# M3a — registers touched by init_mac_registers (mt76x0/init.c:110-134).
# Most addresses live in mt76x02_regs.h. The two tables under
# initvals_init.py reference all of these.
# ============================================================
# Beacon offset table base, [SRC] mt76x02_regs.h:193-194
MT_BCN_OFFSET_BASE                = 0x041c
def MT_BCN_OFFSET(n: int) -> int:
    return MT_BCN_OFFSET_BASE + (n << 2)

# PBF (packet buffer) — [SRC] mt76x02_regs.h:175-191
MT_PBF_SYS_CTRL                   = 0x0400
MT_PBF_CFG                        = 0x0404
MT_PBF_TX_MAX_PCNT                = 0x0408
MT_PBF_RX_MAX_PCNT                = 0x040c

# FCE L2 stuff — [SRC] mt76x02_regs.h:246-255
MT_FCE_L2_STUFF                   = 0x080c
MT_FCE_L2_STUFF_WR_MPDU_LEN_EN    = 1 << 4   # BIT(4)

# IO + LDO + low-addr — [SRC] mt76x02_regs.h:40-75
MT_LDO_CTRL_0                     = 0x006c
MT_LDO_CTRL_1                     = 0x0070
MT_IOCFG_6                        = 0x0124

# DMA-side / TSO — [SRC] mt76x02_regs.h:163-164
MT_TSO_CTRL                       = 0x0250
MT_HEADER_TRANS_CTRL_REG          = 0x0260

# WMM (MT76x0-only alias of FCE_DMA_ADDR address space; bit 11:0 controls
# tx ring rules for queues 8/9). [SRC] mt76x02_regs.h:158 — note this
# shares 0x0230 with FCE_DMA_ADDR but post-FW the chip treats it as WMM.
MT_WMM_CTRL                       = 0x0230

# ASIC version — [SRC] mt76x02_regs.h:9. `mt76_chip = rev >> 16` [SRC] mt76.h:1231,
# where `rev = mt76_rr(MT_ASIC_VERSION)` [SRC] mt76x0/usb.c:266. High 16 bits are the
# chip strap (0x7610 WiFi-only / 0x7630 combo-2.4G / 0x7650 dual) — the is_mt7630 gate.
MT_ASIC_VERSION                   = 0x0000

# MAC + addr + BSSID — [SRC] mt76x02_regs.h:267-294
MT_MAC_CSR0                       = 0x1000   # ASIC version probe (wait_for_mac)
MT_MAC_SYS_CTRL                   = 0x1004   # [SRC] mt76x02_regs.h:269
MT_MAC_ADDR_DW0                   = 0x1008
MT_MAC_ADDR_DW1                   = 0x100c
MT_MAC_BSSID_DW0                  = 0x1010
MT_MAC_BSSID_DW1                  = 0x1014
MT_MAX_LEN_CFG                    = 0x1018
MT_LED_CFG                        = 0x102c
MT_AMPDU_MAX_LEN_20M1S            = 0x1030

# Timing / backoff — [SRC] mt76x02_regs.h:312-319
MT_XIFS_TIME_CFG                  = 0x1100
MT_BKOFF_SLOT_CFG                 = 0x1104

# MAC status — [SRC] mt76x02_regs.h:363-365
MT_MAC_STATUS                     = 0x1200
MT_MAC_STATUS_TX                  = 1 << 0   # BIT(0)
MT_MAC_STATUS_RX                  = 1 << 1   # BIT(1)

# Power-related MAC registers — [SRC] mt76x02_regs.h:367-373
MT_PWR_PIN_CFG                    = 0x1204
MT_BB_PA_MODE_CFG1                = 0x1218
MT_RF_PA_MODE_CFG1                = 0x1220

# MAC addr + BSSID bit fields — [SRC] mt76x02_regs.h:277-287
MT_MAC_ADDR_DW1_U2ME_MASK         = 0xFF << 16    # GENMASK(23, 16)
MT_MAC_BSSID_DW1_MBSS_MODE_SHIFT  = 16            # GENMASK(17, 16)
MT_MAC_BSSID_DW1_MBSS_MODE_MASK   = 0x3 << 16
MT_MAC_BSSID_DW1_MBEACON_N_SHIFT  = 18            # GENMASK(20, 18)
MT_MAC_BSSID_DW1_MBEACON_N_MASK   = 0x7 << 18
MT_MAC_BSSID_DW1_MBSS_LOCAL_BIT   = 1 << 21       # BIT(21)

# Per-vif BSSID slots — [SRC] mt76x02_regs.h:306-310
MT_MAC_APC_BSSID_BASE             = 0x1090
def MT_MAC_APC_BSSID_L(n: int) -> int: return MT_MAC_APC_BSSID_BASE + (n * 8)
def MT_MAC_APC_BSSID_H(n: int) -> int: return MT_MAC_APC_BSSID_BASE + (n * 8) + 4
MT_MAC_APC_BSSID_H_ADDR_MASK      = 0xFFFF        # GENMASK(15, 0)

# WCID — [SRC] mt76x02_regs.h:643-664
MT_WCID_ADDR_BASE                 = 0x1800
def MT_WCID_ADDR(n: int) -> int: return MT_WCID_ADDR_BASE + (n * 8)
MT_WCID_ATTR_BASE                 = 0xa800
def MT_WCID_ATTR(n: int) -> int: return MT_WCID_ATTR_BASE + (n * 4)
MT_WCID_ATTR_BSS_IDX_SHIFT        = 4             # GENMASK(6, 4)
MT_WCID_ATTR_BSS_IDX_MASK         = 0x7 << 4
MT_WCID_ATTR_BSS_IDX_EXT          = 1 << 11       # BIT(11)

# Shared keys + cipher modes — [SRC] mt76x02_regs.h:666-678
MT_SKEY_BASE_0                    = 0xac00
MT_SKEY_BASE_1                    = 0xb400
MT_SKEY_MODE_BASE_0               = 0xb000
MT_SKEY_MODE_BASE_1               = 0xb3f0
MT_SKEY_MODE_MASK                 = 0xF           # GENMASK(3, 0)


def MT_SKEY(bss: int, idx: int) -> int:
    """[SRC] mt76x02_regs.h:670 — 32-byte slot for shared-key data."""
    if bss & 8:
        return MT_SKEY_BASE_1 + (4 * (bss & 7) + idx) * 32
    return MT_SKEY_BASE_0 + (4 * bss + idx) * 32


def MT_SKEY_MODE(bss: int) -> int:
    """[SRC] mt76x02_regs.h:676 — per-bss cipher mode register."""
    if bss & 8:
        return MT_SKEY_MODE_BASE_1 + ((bss & 7) // 2) * 4
    return MT_SKEY_MODE_BASE_0 + (bss // 2) * 4


def MT_SKEY_MODE_SHIFT(bss: int, idx: int) -> int:
    """[SRC] mt76x02_regs.h:678."""
    return 4 * (idx + 4 * (bss & 1))


MT76X02_CIPHER_NONE               = 0    # enum mt76x02_cipher_type:0

# Per-card EEPROM sizing — [SRC] mt76x0/eeprom.h:15-16
MT76X0_EEPROM_SIZE                = 512
MT76X0U_EE_MAX_VER                = 0x0c

# Additional EFUSE field offsets — [SRC] mt76x02_eeprom.h
MT_EE_2G_TARGET_POWER             = 0x0d0
MT_EE_TEMP_OFFSET                 = 0x0d1
MT_EE_5G_TARGET_POWER             = 0x0d2
MT_EE_TSSI_BOUND4                 = 0x0da
MT_EE_TSSI_BOUND_COMPENSATION     = 0x0db   # MT_EE_FREQ_OFFSET_COMPENSATION

# EFUSE fields used by ant_select. [SRC] mt76x02_eeprom.h:17, 18, 24, 98, 114-115.
MT_EE_ANTENNA                     = 0x022
MT_EE_CFG1_INIT                   = 0x024
MT_EE_NIC_CONF_2                  = 0x042
MT_EE_ANTENNA_DUAL                = 1 << 15        # BIT(15)
MT_EE_NIC_CONF_2_ANT_OPT          = 1 << 3         # BIT(3)
MT_EE_NIC_CONF_2_ANT_DIV          = 1 << 4         # BIT(4)

# TX power tables — [SRC] mt76x02_regs.h:387-408
MT_TX_PWR_CFG_0                   = 0x1314
MT_TX_PWR_CFG_1                   = 0x1318
MT_TX_PWR_CFG_2                   = 0x131c
MT_TX_PWR_CFG_3                   = 0x1320
MT_TX_PWR_CFG_4                   = 0x1324
MT_TX_PWR_CFG_7                   = 0x13d4
MT_TX_PWR_CFG_8                   = 0x13d8
MT_TX_PWR_CFG_9                   = 0x13dc

# TX SW/CTRL — [SRC] mt76x02_regs.h:410-428
MT_TX_SW_CFG0                     = 0x1330
MT_TX_SW_CFG1                     = 0x1334
MT_TX_SW_CFG2                     = 0x1338
MT_TXOP_CTRL_CFG                  = 0x1340
MT_TX_RTS_CFG                     = 0x1344
MT_TX_TIMEOUT_CFG                 = 0x1348
MT_TX_RETRY_CFG                   = 0x134c
MT_TX_LINK_CFG                    = 0x1350
MT_VHT_HT_FBK_CFG1                = 0x1358

# Protection rate configs — [SRC] mt76x02_regs.h:441-446
MT_CCK_PROT_CFG                   = 0x1364
MT_OFDM_PROT_CFG                  = 0x1368
MT_MM20_PROT_CFG                  = 0x136c
MT_MM40_PROT_CFG                  = 0x1370
MT_GF20_PROT_CFG                  = 0x1374
MT_GF40_PROT_CFG                  = 0x1378

# ACK + ALC — [SRC] mt76x02_regs.h:470, 487, 502
MT_EXP_ACK_TIME                   = 0x1380
MT_TX_ALC_CFG_0                   = 0x13b0
MT_TX0_BB_GAIN_ATTEN              = 0x13c0   # MT76x0-only

# More TX prot configs — [SRC] mt76x02_regs.h:506-508
MT_TX_PROT_CFG6                   = 0x13e0
MT_TX_PROT_CFG7                   = 0x13e4
MT_TX_PROT_CFG8                   = 0x13e8

# RX filter + rate base + HT — [SRC] mt76x02_regs.h:512-538
MT_RX_FILTR_CFG                   = 0x1400
# SET drops that frame class; CLEAR admits it. [SRC] mt76x02_regs.h:524
MT_RX_FILTR_CFG_ACK               = 1 << 10   # link-layer ACK
MT_AUTO_RSP_CFG                   = 0x1404
MT_LEGACY_BASIC_RATE              = 0x1408
MT_HT_BASIC_RATE                  = 0x140c
MT_HT_CTRL_CFG                    = 0x1410

# Extended CCA — [SRC] mt76x02_regs.h:542-548
MT_EXT_CCA_CFG                    = 0x141c
MT_EXT_CCA_CFG_CCA0_MASK          = 0x3        # GENMASK(1, 0)
MT_EXT_CCA_CFG_CCA0_SHIFT         = 0
MT_EXT_CCA_CFG_CCA1_MASK          = 0xC        # GENMASK(3, 2)
MT_EXT_CCA_CFG_CCA1_SHIFT         = 2
MT_EXT_CCA_CFG_CCA2_MASK          = 0x30       # GENMASK(5, 4)
MT_EXT_CCA_CFG_CCA2_SHIFT         = 4
MT_EXT_CCA_CFG_CCA3_MASK          = 0xC0       # GENMASK(7, 6)
MT_EXT_CCA_CFG_CCA3_SHIFT         = 6
MT_EXT_CCA_CFG_CCA_MASK_MASK      = 0xF00      # GENMASK(11, 8)
MT_EXT_CCA_CFG_CCA_MASK_SHIFT     = 8
MT_EXT_CCA_CFG_ED_CCA_MASK_MASK   = 0xF000     # GENMASK(15, 12)
MT_EXT_CCA_CFG_ED_CCA_MASK_SHIFT  = 12

# TX band cfg — [SRC] mt76x02_regs.h:398-401
MT_TX_BAND_CFG                    = 0x132c
MT_TX_BAND_CFG_UPPER_40M          = 1 << 0     # BIT(0)
MT_TX_BAND_CFG_5G                 = 1 << 1     # BIT(1)
MT_TX_BAND_CFG_2G                 = 1 << 2     # BIT(2)

# Per-band TX correction + VGA — [SRC] mt76x02_regs.h:482, 504
MT_TX0_RF_GAIN_CORR               = 0x13a0
MT_TX0_RF_GAIN_ATTEN              = 0x13a8     # MT76x0-only — [SRC] mt76x02_regs.h:485
MT_TX_ALC_CFG_1                   = 0x13b4     # already in M3a but re-declare for clarity
MT_TX_ALC_VGA3                    = 0x13c8

# EFUSE NIC_CONF_0 PA-internal bits — [SRC] mt76x02_eeprom.h:103-104
MT_EE_NIC_CONF_0_PA_INT_2G        = 1 << 8     # BIT(8)
MT_EE_NIC_CONF_0_PA_INT_5G        = 1 << 9     # BIT(9)

# BBP bit fields used by mt76x02_phy_set_bw / set_band — [SRC] mt76x02_regs.h:621-641
MT_BBP_CORE_R1_BW_MASK            = 0x18       # GENMASK(4, 3)
MT_BBP_CORE_R1_BW_SHIFT           = 3
MT_BBP_AGC_R0_CTRL_CHAN_MASK      = 0x300      # GENMASK(9, 8)
MT_BBP_AGC_R0_CTRL_CHAN_SHIFT     = 8
MT_BBP_AGC_R0_BW_MASK             = 0x7000     # GENMASK(14, 12)
MT_BBP_AGC_R0_BW_SHIFT            = 12
MT_BBP_TXBE_R0_CTRL_CHAN_MASK     = 0x3        # GENMASK(1, 0)
MT_BBP_TXBE_R0_CTRL_CHAN_SHIFT    = 0

# BW_SETTING values for CMD_FUN_SET_OP(BW_SETTING, ...) — [SRC] mt76x0/phy.c:475
BW_SETTING_BW20                   = 0
BW_SETTING_BW40                   = 1
BW_SETTING_BW80                   = 2
BW_SETTING_BW10                   = 4

# 802.11 channel-width enum (subset; only what wifit3 cares about).
# Maps to nl80211_chan_width — we just use these as opaque tags.
NL80211_CHAN_WIDTH_20_NOHT        = 0
NL80211_CHAN_WIDTH_20             = 1
NL80211_CHAN_WIDTH_40             = 2
NL80211_CHAN_WIDTH_80             = 3
NL80211_CHAN_WIDTH_80P80          = 4
NL80211_CHAN_WIDTH_160            = 5
NL80211_CHAN_WIDTH_5              = 6
NL80211_CHAN_WIDTH_10             = 7

# nl80211 band — [SRC] nl80211.h, used as int tags.
NL80211_BAND_2GHZ                 = 0
NL80211_BAND_5GHZ                 = 1

# PN pad mode + TXOP holder — [SRC] mt76x02_regs.h:552-555
MT_PN_PAD_MODE                    = 0x150c
MT_TXOP_HLDR_ET                   = 0x1608

# ============================================================
# BBP register groups — [SRC] mt76x02_regs.h:604-617.
# MT_BBP(_type, _n) = MT_BBP_<type>_BASE + (n << 2)
# ============================================================
MT_BBP_CORE_BASE                  = 0x2000
MT_BBP_IBI_BASE                   = 0x2100
MT_BBP_AGC_BASE                   = 0x2300
MT_BBP_TXC_BASE                   = 0x2400
MT_BBP_RXC_BASE                   = 0x2500
MT_BBP_TXO_BASE                   = 0x2600
MT_BBP_TXBE_BASE                  = 0x2700
MT_BBP_RXFE_BASE                  = 0x2800
MT_BBP_RXO_BASE                   = 0x2900
MT_BBP_DFS_BASE                   = 0x2a00
MT_BBP_TR_BASE                    = 0x2b00
MT_BBP_CAL_BASE                   = 0x2c00
MT_BBP_DSC_BASE                   = 0x2e00
MT_BBP_PFMU_BASE                  = 0x2f00


def MT_BBP_CORE(n: int) -> int: return MT_BBP_CORE_BASE + (n << 2)
def MT_BBP_IBI(n: int)  -> int: return MT_BBP_IBI_BASE  + (n << 2)
def MT_BBP_AGC(n: int)  -> int: return MT_BBP_AGC_BASE  + (n << 2)
def MT_BBP_TXC(n: int)  -> int: return MT_BBP_TXC_BASE  + (n << 2)
def MT_BBP_RXC(n: int)  -> int: return MT_BBP_RXC_BASE  + (n << 2)
def MT_BBP_TXO(n: int)  -> int: return MT_BBP_TXO_BASE  + (n << 2)
def MT_BBP_TXBE(n: int) -> int: return MT_BBP_TXBE_BASE + (n << 2)
def MT_BBP_RXFE(n: int) -> int: return MT_BBP_RXFE_BASE + (n << 2)
def MT_BBP_RXO(n: int)  -> int: return MT_BBP_RXO_BASE  + (n << 2)
def MT_BBP_CAL(n: int)  -> int: return MT_BBP_CAL_BASE  + (n << 2)


# RF band + bandwidth tags for `mt76x0_bbp_switch_tab` filtering.
# [SRC] mt76x0/phy.h:9-19
RF_G_BAND                         = 0x0100
RF_A_BAND                         = 0x0200
RF_A_BAND_LB                      = 0x0400
RF_A_BAND_MB                      = 0x0800
RF_A_BAND_HB                      = 0x1000
RF_A_BAND_11J                     = 0x2000

RF_BW_20                          = 1
RF_BW_40                          = 2
RF_BW_10                          = 4
RF_BW_80                          = 8

# MT_MAC_SYS_CTRL bit fields. Kernel pre-FW writes 0x2c = ENABLE_TX | ENABLE_RX | BIT(5).
MT_MAC_SYS_CTRL_RESET_CSR         = 1 << 0
MT_MAC_SYS_CTRL_RESET_BBP         = 1 << 1
MT_MAC_SYS_CTRL_ENABLE_TX         = 1 << 2
MT_MAC_SYS_CTRL_ENABLE_RX         = 1 << 3
MT_MAC_SYS_CTRL_PRE_FW_VALUE      = 0x2c     # [SRC] mt76x0/usb_mcu.c:125

# (MT_CMB_CTRL above replaced the misnamed MT_PROBE_REG_0X20.)

# MT_USB_DMA_CFG bit fields — [SRC] mt76x02_regs.h:78-87
MT_USB_DMA_CFG_RX_BULK_AGG_TOUT_MASK = 0xFF  # GENMASK(7,0)
MT_USB_DMA_CFG_UDMA_TX_WL_DROP    = 1 << 16  # BIT(16)
MT_USB_DMA_CFG_RX_DROP_OR_PAD     = 1 << 18  # BIT(18)
MT_USB_DMA_CFG_RX_BULK_AGG_EN     = 1 << 21  # BIT(21)
MT_USB_DMA_CFG_RX_BULK_EN         = 1 << 22  # BIT(22)
MT_USB_DMA_CFG_TX_BULK_EN         = 1 << 23  # BIT(23)

# MT_MCU_COM_REG0 — FW running flag
MT_MCU_COM_REG0_FW_READY          = 1 << 0   # BIT(0)

# ============================================================
# MCU msg info-header fields (the 4 bytes prepended to each bulk-OUT chunk).
# [SRC] mt76x02_dma.h:33-46
# ============================================================
MT_MCU_MSG_LEN_MASK     = 0xFFFF        # GENMASK(15,0)
MT_MCU_MSG_CMD_SEQ_SHIFT = 16           # GENMASK(19,16) — 4-bit cmd sequence id
MT_MCU_MSG_CMD_SEQ_MASK = 0xF << 16
MT_MCU_MSG_CMD_TYPE_SHIFT = 20          # GENMASK(26,20) — 7-bit cmd code
MT_MCU_MSG_CMD_TYPE_MASK = 0x7F << 20
MT_MCU_MSG_PORT_SHIFT   = 27            # GENMASK(29,27)
MT_MCU_MSG_PORT_MASK    = 0x7 << 27
MT_MCU_MSG_TYPE_CMD     = 1 << 30       # BIT(30)
CPU_TX_PORT             = 2             # enum dma_msg_port — mt76x02_dma.h:43-51

# MCU response RX-FCE header (first 4 bytes of bulk-IN payload on EP 0x85).
# [SRC] mt76x02_dma.h:25-26
MT_RX_FCE_INFO_CMD_SEQ_SHIFT = 16       # GENMASK(19,16)
MT_RX_FCE_INFO_CMD_SEQ_MASK = 0xF << 16
MT_RX_FCE_INFO_EVT_TYPE_SHIFT = 20      # GENMASK(23,20)
MT_RX_FCE_INFO_EVT_TYPE_MASK = 0xF << 20

# enum mt76_mcu_evt_type — [SRC] dma.h:150-158. Implicit 0-based.
EVT_CMD_DONE = 0
EVT_CMD_ERROR = 1
EVT_CMD_RETRY = 2

# MCU command codes — [SRC] mt76x02_usb_mcu.c (inline `const int` declarations).
CMD_FUN_SET_OP      = 1
CMD_LOAD_CR         = 2
CMD_INIT_GAIN_OP    = 3
CMD_DYNC_VGA_OP     = 6
CMD_TDLS_CH_SW      = 7
CMD_BURST_WRITE     = 8
CMD_READ_MODIFY_WRITE = 9
CMD_RANDOM_READ     = 10
CMD_BURST_READ      = 11
CMD_RANDOM_WRITE    = 12
CMD_LED_MODE_OP     = 16
CMD_POWER_SAVING_OP = 20
CMD_WOW_CONFIG      = 21
CMD_WOW_QUERY       = 22
CMD_WOW_FEATURE     = 24
CMD_CARRIER_DETECT_OP = 28
CMD_RADOR_DETECT_OP = 29
CMD_SWITCH_CHANNEL_OP = 30
CMD_CALIBRATION_OP  = 31
CMD_BEACON_OP       = 32
CMD_ANTENNA_OP      = 33

# MCU calibrate types — [SRC] mt76x0/mcu.h:22-37 (enum mcu_calibrate).
MCU_CAL_R               = 1
MCU_CAL_RXDCOC          = 2
MCU_CAL_LC              = 3
MCU_CAL_LOFT            = 4
MCU_CAL_TXIQ            = 5
MCU_CAL_BW              = 6
MCU_CAL_DPD             = 7
MCU_CAL_RXIQ            = 8
MCU_CAL_TXDCOC          = 9
MCU_CAL_RX_GROUP_DELAY  = 10
MCU_CAL_TX_GROUP_DELAY  = 11
MCU_CAL_VCO             = 12
MCU_CAL_NO_SIGNAL       = 0xFE
MCU_CAL_FULL            = 0xFF

# MT_BBP(AGC, 8) GAIN bit-field — [SRC] mt76x02_regs.h:636.
MT_BBP_AGC_GAIN_MASK    = 0x7F00       # GENMASK(14, 8)
MT_BBP_AGC_GAIN_SHIFT   = 8

# MT_TX_ALC_CFG_0 already in M3a — needed here for phy_calibrate's save/restore.

# CMD_FUN_SET_OP sub-functions — [SRC] mt76x02_mcu.h:62-72 (enum mcu_function)
Q_SELECT          = 1
BW_SETTING        = 2
USB2_SW_DISCONNECT = 2
USB3_SW_DISCONNECT = 3
LOG_FW_DEBUG_MSG  = 4
GET_FW_VERSION    = 5

# MCU response URB buffer size — [SRC] mt76.h:661
MCU_RESP_URB_SIZE = 1024
MCU_RESP_TIMEOUT_MS = 300       # [SRC] mt76x02_usb_mcu.c:46
MCU_RESP_MAX_RETRY = 5          # [SRC] mt76x02_usb_mcu.c:44
MCU_SEND_TIMEOUT_MS = 500       # [SRC] mt76x02_usb_mcu.c:95

# Base address used by every MCU register access. Kernel `mt76x02u_mcu_wr_rp`
# / `_rd_rp` send `base + reg` on the wire (e.g. reg 0x1000 → wire 0x00411000).
# [SRC] mt76x02_mcu.h:19, [SRC] mt76x0/init.c:84 (RANDOM_WRITE macro).
# [WIRE] capture-2.pcap:427 — every addr in the payload is 0x00411xxx.
MT_MCU_MEMMAP_WLAN = 0x410000

# Max payload bytes per CMD_RANDOM_WRITE / CMD_RANDOM_READ. [SRC] mt76x02_mcu.h:18.
# Random-write helpers chunk pairs at MT_INBAND_PACKET_MAX_LEN / 8 = 24 pairs.
MT_INBAND_PACKET_MAX_LEN = 192
MT_MCU_REG_PAIRS_PER_CMD = MT_INBAND_PACKET_MAX_LEN // 8   # = 24

# RF register MCU base. [SRC] mt76x0/mcu.h:20.
# rf_wr/rr go through CMD_RANDOM_WRITE/READ with base=MT_MCU_MEMMAP_RF
# instead of MT_MCU_MEMMAP_WLAN. The "offset" within the RF space is
# `MT_RF(bank, reg) = (bank << 16) | reg`.
MT_MCU_MEMMAP_RF = 0x80000000


def MT_RF(bank: int, reg: int) -> int:
    """[SRC] mt76x0/phy.h:21 — `MT_RF(bank, reg) = (bank << 16) | reg`."""
    return (bank << 16) | reg


# RF register bit-field masks used by mt76x0_phy_set_chan_rf_params.
# [SRC] mt76x0/phy.h:32-40
MT_RF_PLL_DEN_MASK                = 0x1F       # GENMASK(4, 0)
MT_RF_PLL_K_MASK                  = 0x1F       # GENMASK(4, 0)
MT_RF_SDM_RESET_MASK              = 0x80       # BIT(7)
MT_RF_SDM_MASH_PRBS_MASK          = 0x7C       # GENMASK(6, 2)
MT_RF_SDM_BP_MASK                 = 0x02       # BIT(1)
MT_RF_ISI_ISO_MASK                = 0xC0       # GENMASK(7, 6)
MT_RF_PFD_DLY_MASK                = 0x30       # GENMASK(5, 4)
MT_RF_CLK_SEL_MASK                = 0x0C       # GENMASK(3, 2)
MT_RF_XO_DIV_MASK                 = 0x03       # GENMASK(1, 0)

# ============================================================
# FW upload constants — [SRC] mt76x0/mcu.h:14-15 + usb_mcu.c:13-14
# ============================================================
MT_MCU_IVB_SIZE              = 0x40          # bytes — first 0x40 of FW body is IVB
MT_MCU_DLM_OFFSET            = 0x80000       # DLM upload base address
MCU_FW_URB_MAX_PAYLOAD       = 0x38f8        # 14584 — total URB size cap
MCU_FW_CHUNK_DATA_MAX        = 14584 - 8     # 14576 — info(4)+pad(4) deducted

# mt76x02_fw_header structure (32 bytes). [SRC] mt76x02_mcu.h:71-78
#   __le32 ilm_len
#   __le32 dlm_len
#   __le16 build_ver
#   __le16 fw_ver
#   u8     pad[4]
#   char   build_time[16]
MT76X02_FW_HEADER_SIZE       = 32

# ============================================================
# EFUSE — [SRC] mt76x02_regs.h:18-28 + mt76x02_eeprom.h:14-95
# ============================================================
MT_EFUSE_CTRL                = 0x0024
MT_EFUSE_DATA_BASE           = 0x0028   # MT_EFUSE_DATA(n) = base + 4*n, n=0..3

MT_EFUSE_CTRL_AOUT_MASK      = 0x3F           # GENMASK(5, 0)
MT_EFUSE_CTRL_MODE_SHIFT     = 6              # GENMASK(7, 6)
MT_EFUSE_CTRL_MODE_MASK      = 0x3 << 6
MT_EFUSE_CTRL_AIN_SHIFT      = 16             # GENMASK(25, 16)
MT_EFUSE_CTRL_AIN_MASK       = 0x3FF << 16
MT_EFUSE_CTRL_KICK           = 1 << 30        # BIT(30)
MT_EFUSE_CTRL_SEL            = 1 << 31        # BIT(31) — set means EFUSE present

# EFUSE read modes — [SRC] mt76x02_eeprom.h:121-124
MT_EE_READ           = 0   # logical (with fallback to defaults if unburned)
MT_EE_PHYSICAL_READ  = 1   # raw EFUSE without fallback

# EFUSE logical-field offsets — [SRC] mt76x02_eeprom.h:14-95 (enum mt76x02_eeprom_field)
MT_EE_CHIP_ID                = 0x000
MT_EE_VERSION                = 0x002
MT_EE_MAC_ADDR               = 0x004
MT_EE_PCI_ID                 = 0x00A
MT_EE_ANTENNA                = 0x022
MT_EE_NIC_CONF_0             = 0x034
MT_EE_NIC_CONF_1             = 0x036
MT_EE_COUNTRY_REGION_5GHZ    = 0x038
MT_EE_COUNTRY_REGION_2GHZ    = 0x039
MT_EE_FREQ_OFFSET            = 0x03A
MT_EE_NIC_CONF_2             = 0x042
# RX LNA gain (per-band / per-5GHz-subband). [SRC] mt76x02_eeprom.h:29-35.
# Read as u16 words; the gains are the high bytes (lna_2g is the 0x044 low byte).
MT_EE_LNA_GAIN               = 0x044   # lo=lna_2g, hi=lna_5g[0]
MT_EE_RSSI_OFFSET_2G_1       = 0x048   # hi byte = lna_5g[1] (== MT_EE_LNA_GAIN_5GHZ_1 0x049)
MT_EE_RSSI_OFFSET_5G_1       = 0x04C   # hi byte = lna_5g[2] (== MT_EE_LNA_GAIN_5GHZ_2 0x04d)
MT_EE_USAGE_MAP_START        = 0x1E0
MT_EE_USAGE_MAP_END          = 0x1FC
MT_EFUSE_USAGE_MAP_SIZE      = MT_EE_USAGE_MAP_END - MT_EE_USAGE_MAP_START + 1

# NIC_CONF_0 bit fields — [SRC] mt76x02_eeprom.h:100-106
MT_EE_NIC_CONF_0_RX_PATH_MASK     = 0x000F     # GENMASK(3, 0)
MT_EE_NIC_CONF_0_TX_PATH_MASK     = 0x00F0     # GENMASK(7, 4)
MT_EE_NIC_CONF_0_TX_PATH_SHIFT    = 4
MT_EE_NIC_CONF_0_BOARD_TYPE_MASK  = 0x3000     # GENMASK(13, 12)
MT_EE_NIC_CONF_0_BOARD_TYPE_SHIFT = 12

# BOARD_TYPE values — [SRC] mt76x02_eeprom.c:76-82
BOARD_TYPE_2GHZ = 1
BOARD_TYPE_5GHZ = 2

# ============================================================
# Bring-up timing (from usb_mcu.c kernel comments).
# ============================================================
POST_FW_RESET_SLEEP_MS       = 6     # kernel usleep_range(5000, 6000)
INTER_CHUNK_SLEEP_MS         = 10    # kernel usleep_range(5000, 10000)
FW_READY_POLL_INTERVAL_MS    = 1     # kernel mt76_poll_msec interval
FW_READY_POLL_TIMEOUT_MS     = 1000  # kernel mt76_poll_msec timeout
