"""Realtek RTL8814AU (rtw88 family, WCPU_3081 / iDDMA FW path) constants.

Verified against `data_dumps/rtw88-source-v6.18/rtw8814a{,u}.{c,h}` +
`usb_dumps/captures_rtw88_8814au/capture-1.pcap`. See RTL8814AU.md for the
provenance of each fact.

The 8814A's firmware/MAC path is the same modern iDDMA path as the 8822B
(both `RTW_WCPU_3081`), so this re-exports the family-shared register surface
from :mod:`wifit3.chips.rtw88_base.registers` and adds the 8814a-specific
values. The chip diverges from the 8822b only in PHY/RF (4T4R), which is M3+.
"""

from __future__ import annotations

# --- Re-export the common rtw88 register surface ---------------------------
from wifit3.chips.rtw88_base.registers import (  # noqa: F401
    BIT_CHECK_SUM_OK,
    BIT_DDMACH0_CHKSUM_CONT,
    BIT_DDMACH0_CHKSUM_EN,
    BIT_DDMACH0_CHKSUM_STS,
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
    BIT_MASK_DDMACH0_DLEN,
    BIT_MCUFWDL_EN,
    BIT_TXDMA_EN,
    BIT_WLMCU_IOIF,
    FW_READY,
    FW_READY_MASK,
    OCPBASE_DMEM_88XX,
    OCPBASE_TXBUF_88XX,
    REG_CR,
    REG_DDMA_CH0CTRL,
    REG_DDMA_CH0DA,
    REG_DDMA_CH0SA,
    REG_H2CQ_CSR,
    REG_MCUFW_CTRL,
    REG_RSV_CTRL,
    REG_SYS_CFG1,
    REG_SYS_FUNC_EN,
    REG_SYS_STATUS1,
    REG_TXDMA_PQ_MAP,
    REG_TXDMA_STATUS,
    TX_DESC_QSEL_BEACON,
)

# --- USB IDs (rtw_8814au_id_table in rtw8814au.c) --------------------------
# The Alfa AWUS1900 enumerates as the Realtek default 0x0bda:0x8813. The rest
# are the kernel's full table (other vendors' 8814AU dongles).
USB_IDS_8814AU: tuple[tuple[int, int, str], ...] = (
    (0x0BDA, 0x8813, "Realtek RTL8814AU (default) / Alfa AWUS1900"),
    (0x056E, 0x400B, "Elecom WDC-1300SU2 (RTL8814AU)"),
    (0x056E, 0x400D, "Elecom (RTL8814AU)"),
    (0x0846, 0x9054, "Netgear A7000 (RTL8814AU)"),
    (0x0B05, 0x1817, "ASUS USB-AC68 (RTL8814AU)"),
    (0x0B05, 0x1852, "ASUS (RTL8814AU)"),
    (0x0B05, 0x1853, "ASUS USB-AC68 (RTL8814AU)"),
    (0x0E66, 0x0026, "Hawking HW12ACU (RTL8814AU)"),
    (0x2001, 0x331A, "D-Link DWA-192 (RTL8814AU)"),
    (0x20F4, 0x809A, "TRENDnet TEW-809UB (RTL8814AU)"),
    (0x20F4, 0x809B, "TRENDnet (RTL8814AU)"),
    (0x2357, 0x0106, "TP-Link Archer T9UH (RTL8814AU)"),
    (0x7392, 0xA834, "Edimax EW-7833UAC (RTL8814AU)"),
    (0x7392, 0xA833, "Edimax EW-7833 (RTL8814AU)"),
)

# --- Chip parameters (rtw8814a_hw_spec, rtw8814a.c:2180) -------------------
TX_PKT_DESC_SZ = 40              # .tx_pkt_desc_sz — 40, NOT the 8822b's 48
RX_PKT_DESC_SZ = 24              # .rx_pkt_desc_sz
PAGE_SIZE = 128                  # .page_size = TX_PAGE_SIZE = 1<<7 (main.h:35)
TXFF_SIZE = (2048 - 10) * 128    # .txff_size
RXFF_SIZE = 23552                # .rxff_size
SYS_FUNC_EN_8814A = 0xDC         # .sys_func_en
MAX_POWER_INDEX = 0x3F           # .max_power_index

# 4T4R RF paths (.rf_base_addr / .rf_sipi_addr). Used from M3 onward.
RF_BASE_ADDR = (0x2800, 0x2C00, 0x3800, 0x3C00)
RF_SIPI_ADDR = (0xC90, 0xE90, 0x1890, 0x1A90)

# --- FW upload (modern iDDMA path; see mac.c:776 __rtw_download_firmware) --
# Sizes/addresses are verified [WIRE] against capture-1 AND the FW header
# (assets/rtw8814a_fw-linux_firmware.bin): see RTL8814AU.md §1.3.1.
FW_HDR_SIZE = 64                 # rtw_fw_hdr (fw.h:316)
FW_HDR_CHKSUM_SIZE = 8           # fw.h:13
DLFW_MAX_CHUNK_SIZE = 0x1000     # 4096 bytes per tx_pkt + iddma cycle

# The actual FW-upload TX descriptor is `chip->tx_pkt_desc_sz` (= 40, drives
# both the descriptor build and the iddma source offset). But `send_firmware_pkt`
# (mac.c:550) computes its ZLP-avoidance `%512` decision against the kernel's
# HARDCODED `#define TX_DESC_SIZE 48` (mac.c:528) — NOT the chip's real desc
# size. For 8822b these coincide (both 48); for the 8814a they differ, so the
# two values are kept separate here.
FW_DLFW_ZLP_TXDESC = 48          # mac.c:528 #define TX_DESC_SIZE (ZLP check only)

DMEM_ADDR = 0x00200000           # header dmem_addr 0x80200000 & ~BIT(31)
DMEM_UPLOAD_SIZE = 5792          # 5784 body + 8 chksum
IMEM_ADDR = 0x00000000           # header imem_addr 0x80000000 & ~BIT(31)
IMEM_UPLOAD_SIZE = 62464         # 62456 body + 8 chksum
EMEM_PRESENT = False             # mem_usage bit 4 clear — no EMEM segment

# FW-upload bulk-OUT endpoint for the AWUS1900 (out_ep[0]) — [WIRE] capture-1.
EP_FW_BULK_OUT = 0x02
