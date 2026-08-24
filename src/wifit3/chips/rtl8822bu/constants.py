"""Realtek RTL8822BU (rtw88 family, modern iDDMA FW path) protocol constants.

Verified against `driver_sources/rtw88-source-v6.18/rtw8822b{,u}.{c,h}` +
`driver_captures/captures_rtw88_8822bu/capture-1.pcap`. See RTL8822BU.md for
provenance citations on each fact.

Re-exports the family-shared register addresses from
:mod:`wifit3.chips.rtw88_base.registers` and adds the 8822b-specific bits.
"""

from __future__ import annotations

# --- Re-export the common rtw88 register surface ---------------------------
from wifit3.chips.rtw88_base.registers import (  # noqa: F401
    BASIC_RATES_2G,
    BIT_CHECK_SUM_OK,
    BIT_DDMACH0_CHKSUM_CONT,
    BIT_DDMACH0_CHKSUM_EN,
    BIT_DDMACH0_CHKSUM_STS,
    BIT_DDMACH0_DDMA_MODE,
    BIT_DDMACH0_OWN,
    BIT_DDMACH0_RESET_CHKSUM_STS,
    BIT_DIS_TSF_UDT,
    BIT_DMEM_CHKSUM_OK,
    BIT_DMEM_DW_OK,
    BIT_EN_BCN_FUNCTION,
    BIT_FEN_CPUEN,
    BIT_FW_DW_RDY,
    BIT_FW_INIT_RDY,
    BIT_H2CQ_FULL,
    BIT_HCI_TXDMA_EN,
    BIT_IMEM_CHKSUM_OK,
    BIT_IMEM_DW_OK,
    BIT_MACRXEN,
    BIT_MACTXEN,
    BIT_MASK_DDMACH0_DLEN,
    BIT_MCUFWDL_EN,
    BIT_ROM_DLEN,
    BIT_ROM_PGE,
    BIT_TXDMA_EN,
    BIT_WLMCU_IOIF,
    DESC_RATE1M,
    DESC_RATE2M,
    DESC_RATE5_5M,
    DESC_RATE6M,
    DESC_RATE11M,
    DESC_RATE12M,
    DESC_RATE24M,
    OCPBASE_DMEM_88XX,
    OCPBASE_RXBUF_FW_88XX,
    OCPBASE_TXBUF_88XX,
    REG_BCN_CTRL,
    REG_CR,
    REG_DDMA_CH0CTRL,
    REG_DDMA_CH0DA,
    REG_DDMA_CH0SA,
    REG_DWBCN0_CTRL,
    REG_FIFOPAGE_CTRL_2,
    REG_FIFOPAGE_INFO_1,
    REG_FIFOPAGE_INFO_2,
    REG_FIFOPAGE_INFO_3,
    REG_FIFOPAGE_INFO_4,
    REG_FWHW_TXQ_CTRL,
    REG_H2CQ_CSR,
    REG_HIMR0,
    REG_HIMR1,
    REG_LLT_INIT,
    REG_MCUFW_CTRL,
    REG_RQPN,
    REG_RQPN_CTRL_2,
    REG_RSV_CTRL,
    REG_SYS_CFG1,
    REG_SYS_CFG2,
    REG_SYS_CLKR,
    REG_SYS_FUNC_EN,
    REG_TXDMA_PQ_MAP,
    REG_TXDMA_STATUS,
    RTW_CHANNEL_WIDTH_20,
    RTW_DMA_MAPPING_EXTRA,
    RTW_DMA_MAPPING_HIGH,
    RTW_DMA_MAPPING_LOW,
    RTW_DMA_MAPPING_NORMAL,
    TX_DESC_QSEL_BEACON,
    TX_DESC_QSEL_H2C,
    TX_DESC_QSEL_HIGH,
    TX_DESC_QSEL_MGMT,
)
from wifit3.chips.products import ASUS, Buffalo, CCandC, DLink, Edimax, Elecom, Hawking, Linksys, LiteOn, Mercusys, Netgear, TPLink, TRENDnet

# --- USB IDs (rtw_8822bu_id_table in rtw8822bu.c) --------------------------
# Full list is large; we register the popular ones, especially the TP-Link
# T3U / T3U Plus variants. Add more here as users report dongles.
USB_IDS_8822BU: tuple[tuple[int, int, str, str | None, str | None], ...] = (
    # TP-Link Archer T3U Plus v1: the user's lab device
    (0x2357, 0x0138, "RTL8822BU", None, TPLink.ARCHER_T3U_PLUS),
    (0x2357, 0x012D, "RTL8822BU", None, TPLink.ARCHER_T3U),
    # 2357:0115 could be one of: T4U Plus, T4U v3, T4U v3.2 https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U_v3
    (0x2357, 0x0115, "RTL8822BU", None, TPLink.ARCHER_T4U_V3),  # Chosen by dice roll.
    (0x2357, 0x012E, "RTL8822BU", None, TPLink.ARCHER_T3U_NANO),
    (0x2357, 0x0116, "RTL8822BU", None, None),  # (TP-Link) Wireless USB Adapter https://linux-hardware.org/?id=usb:2357-0116
    (0x2357, 0x0117, "RTL8822BU", None, None),  # (TP-Link) High Power Wireless USB Adapter https://linux-hardware.org/?id=usb:2357-0117
    (0x0BDA, 0xB812, "RTL8822BU", None, None),  # (Realtek) RTL88x2bu [AC1200 Techkey] https://linux-hardware.org/?id=usb:0BDA-B812
    (0x0BDA, 0xB82C, "RTL8822BU", None, None),  # (Realtek) 802.11ac NIC https://linux-hardware.org/?id=usb:0BDA-B82C
    (0x0BDA, 0xB81A, "RTL8822BU", None, None),  # (Realtek) 8812BU Wireless LAN 802.11ac USB NIC https://linux-hardware.org/?id=usb:0BDA-B81A
    (0x0B05, 0x1841, "RTL8822BU", None, ASUS.USB_AC55_B1),
    (0x0B05, 0x184C, "RTL8822BU", None, ASUS.USB_AC53_NANO),
    (0x0B05, 0x19AA, "RTL8822BU", None, ASUS.USB_AC58_A1),
    (0x2001, 0x331E, "RTL8822BU", None, DLink.DWA_181),
    (0x2001, 0x331C, "RTL8822BU", None, DLink.DWA_182_D1),
    (0x13B1, 0x0043, "RTL8822BU", None, Linksys.WUSB6400M),
    (0x13B1, 0x0045, "RTL8822BU", None, Linksys.WUSB6300_V2),
    (0x0846, 0x9055, "RTL8822BU", None, Netgear.A6150),
    (0x7392, 0xB822, "RTL8822BU", None, Edimax.EW_7822ULC),
    (0x7392, 0xC822, "RTL8822BU", None, Edimax.EW_7822UTC),
    (0x7392, 0xD822, "RTL8822BU", None, None),  # (Edimax) Dacota Platinum AC1200 USB 2.0 Wireless Adapter https://linux-hardware.org/?id=usb:7392-D822
    (0x7392, 0xE822, "RTL8822BU", None, None),  # (Edimax) Dacota Platinum AC1200 USB 3.0 Wireless Adapter https://linux-hardware.org/?id=usb:7392-E822
    (0x7392, 0xF822, "RTL8822BU", None, Edimax.EW_7822UAD),
    (0x2C4E, 0x0107, "RTL8822BU", None, Mercusys.MA30H),
    (0x2C4E, 0x010A, "RTL8822BU", None, Mercusys.MA30N),
    (0x0411, 0x03D1, "RTL8822BU", None, Buffalo.WI_U2_866DM),
    (0x0411, 0x03D0, "RTL8822BU", None, Buffalo.WI_U3_866DHP),
    (0x04CA, 0x8602, "RTL8822BU", None, LiteOn.WN8602L),
    (0x056E, 0x4011, "RTL8822BU", None, Elecom.WDB_867DU3S),
    (0x0B05, 0x1870, "RTL8822BU", None, ASUS._8822BU_1870),
    (0x0B05, 0x1874, "RTL8822BU", None, ASUS._8822BU_1874),
    (0x0BDA, 0x2102, "RTL8822BU", None, CCandC._433MBPS),
    (0x0E66, 0x0025, "RTL8822BU", None, Hawking.HW12ACU),
    (0x2001, 0x331F, "RTL8822BU", None, DLink.DWA_183_D),
    (0x2001, 0x3322, "RTL8822BU", None, DLink.DWA_T185_REV_A1),
    (0x20F4, 0x805A, "RTL8822BU", None, TRENDnet.TEW_805UBH),
    (0x20F4, 0x808A, "RTL8822BU", None, TRENDnet.TEW_808UBM),
)

# --- Chip parameters (rtw_chip_info from rtw8822b.c:2496..2618) ------------
TX_PKT_DESC_SZ = 48              # rtw8822b.c (.tx_pkt_desc_sz)
RX_PKT_DESC_SZ = 24              # rtw8822b.c (.rx_pkt_desc_sz)
PAGE_SIZE = 128                  # rtw8822b.c (.page_size)
TXFF_SIZE = 65536                # rtw8822b.c (.txff_size)
RXFF_SIZE = 24576                # rtw8822b.c (.rxff_size)

# --- FW upload (modern iDDMA path; see mac.c:776 __rtw_download_firmware) --
FW_HDR_SIZE = 64                 # rtw_fw_hdr (fw.h:316)
FW_HDR_CHKSUM_SIZE = 8           # fw.h:13
DLFW_MAX_CHUNK_SIZE = 0x1000     # 4096 bytes per tx_pkt + iddma cycle

# --- FW_READY for modern path ----------------------------------------------
# `FW_READY` constant from mac.c:751 — used after upload to confirm the
# wlan CPU is alive. It's the BIT_CHECK_SUM_OK group OR'd with the FW init
# ready bit.
FW_READY_MODERN = BIT_CHECK_SUM_OK | BIT_FW_INIT_RDY | BIT_FW_DW_RDY
FW_READY_MASK_MODERN = (
    BIT_CHECK_SUM_OK | BIT_FW_INIT_RDY | BIT_FW_DW_RDY | BIT_MCUFWDL_EN
)
