"""RTL8822BU MAC bring-up (modern non-8051 path).

Mirrors the kernel sequence:

    rtw_mac_power_on(rtwdev):
      rtw_mac_pre_system_cfg(rtwdev)      # mac.c:62..137
      rtw_mac_power_switch(rtwdev, true)  # mac.c:268..328 (runs pwr_seq tables)
      rtw_mac_init_system_cfg(rtwdev)     # mac.c:330..353

Source citations are in-line. The 8822b is NOT an 8051 wlan-CPU chip, so
we take the `__rtw_mac_init_system_cfg` branch (mac.c:330..353), not the
`_legacy` one (mac.c:355..368).
"""

from __future__ import annotations

import logging

from wifit3.chips.rtw88_base.power_seq import CUT_ALL
from wifit3.chips.rtw88_base.registers import (
    BIT_BOOT_FSPI_EN,
    BIT_DDMA_EN,
    BIT_FEN_BB_GLB_RST,
    BIT_FEN_BB_RSTB,
    BIT_FSPI_EN,
    BIT_FW_DW_RDY,
    BIT_FW_INIT_RDY,
    BIT_LNAON_SEL_EN,
    BIT_LNAON_WLBT_SEL,
    BIT_MACRXEN,
    BIT_MACTXEN,
    BIT_PAPE_SEL_EN,
    BIT_PAPE_WLBT_SEL,
    BIT_RF_EN,
    BIT_RF_RSTB,
    BIT_RF_SDM_RSTB,
    BIT_WL_PLATFORM_RST,
    BIT_WLRF1_BBRF_EN,
    BIT_WLRFE_4_5_EN,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_CR_EXT,
    REG_GPIO_MUXCFG,
    REG_LED_CFG,
    REG_MCUFW_CTRL,
    REG_PAD_CTRL1,
    REG_RF_CTRL,
    REG_RSV_CTRL,
    REG_SYS_FUNC_EN,
    REG_SYS_STATUS1,
    REG_WLRF1,
)

from .power_seq import card_enable_flow_8822b
from .transport import RTL8822BUTransport

logger = logging.getLogger(__name__)


# rtw_chip_info.sys_func_en for 8822b — from rtw8822b.c:2551.
SYS_FUNC_EN_8822B = 0xDC


def cut_mask_from_sys_cfg1(chip_version: int) -> int:
    """Mirror `cut_version_to_mask` (mac.h:9): `0x1 << (cut + 1)`.

    `chip_version` is the full REG_SYS_CFG1 read. The cut nibble lives in
    bits 12..15.
    """
    cut = (chip_version >> 12) & 0xF
    return 0x1 << (cut + 1)


def rtw_mac_pre_system_cfg(transport: RTL8822BUTransport) -> None:
    """mac.c:62..137 — the non-8051, USB-only path."""
    logger.debug("rtw_mac_pre_system_cfg (modern, USB)")
    transport.write8(REG_RSV_CTRL, 0)

    # USB branch in mac.c:104 is empty — no SDIO/PCIE-specific writes for us.

    # config PIN Mux (mac.c:111..121)
    v = transport.read32(REG_PAD_CTRL1)
    v |= BIT_PAPE_WLBT_SEL | BIT_LNAON_WLBT_SEL
    transport.write32(REG_PAD_CTRL1, v)

    v = transport.read32(REG_LED_CFG)
    v &= ~(BIT_PAPE_SEL_EN | BIT_LNAON_SEL_EN)
    transport.write32(REG_LED_CFG, v & 0xFFFFFFFF)

    v = transport.read32(REG_GPIO_MUXCFG)
    v |= BIT_WLRFE_4_5_EN
    transport.write32(REG_GPIO_MUXCFG, v)

    # disable BB/RF (mac.c:124..134)
    v8 = transport.read8(REG_SYS_FUNC_EN)
    v8 &= ~(BIT_FEN_BB_RSTB | BIT_FEN_BB_GLB_RST)
    transport.write8(REG_SYS_FUNC_EN, v8 & 0xFF)

    v8 = transport.read8(REG_RF_CTRL)
    v8 &= ~(BIT_RF_SDM_RSTB | BIT_RF_RSTB | BIT_RF_EN)
    transport.write8(REG_RF_CTRL, v8 & 0xFF)

    v = transport.read32(REG_WLRF1)
    v &= ~BIT_WLRF1_BBRF_EN
    transport.write32(REG_WLRF1, v & 0xFFFFFFFF)


def rtw_mac_power_switch(transport: RTL8822BUTransport, pwr_on: bool,
                         *, cut_mask: int = CUT_ALL) -> int:
    """mac.c:268..328.

    Returns 0 on success, -EALREADY (constant: -114) if the chip is already
    in the requested power state. The caller (rtw_mac_power_on) handles
    EALREADY by cycling power_switch(false) then power_switch(true).
    """
    EALREADY = -114

    if transport.read8(REG_CR) == 0xEA:
        cur_pwr = False
    elif transport.read8(REG_SYS_STATUS1 + 1) & 0x01:
        # USB + 8822b: this bit indicates "card already powered"
        cur_pwr = False
    else:
        cur_pwr = True

    logger.debug("rtw_mac_power_switch: cur_pwr=%s req=%s", cur_pwr, pwr_on)

    if pwr_on == cur_pwr:
        return EALREADY

    if pwr_on:
        card_enable_flow_8822b(transport, cut_mask=cut_mask)
        # mac.c:314..319 — for 8822b/c/8821c on USB after pwr_on, clear
        # REG_SYS_STATUS1+1 bit 0.
        transport.write8_clr(REG_SYS_STATUS1 + 1, 0x01)
    else:
        from .power_seq import card_disable_flow_8822b
        card_disable_flow_8822b(transport, cut_mask=cut_mask)

    return 0


def rtw_mac_init_system_cfg(transport: RTL8822BUTransport) -> None:
    """mac.c:330..353 (non-8051 / non-legacy)."""
    logger.debug("rtw_mac_init_system_cfg (modern)")

    v = transport.read32(REG_CPU_DMEM_CON)
    v |= BIT_WL_PLATFORM_RST | BIT_DDMA_EN
    transport.write32(REG_CPU_DMEM_CON, v)

    # sys_func_en = 0xDC for 8822b (rtw8822b.c:2551)
    transport.write8_set(REG_SYS_FUNC_EN + 1, SYS_FUNC_EN_8822B)

    v8 = (transport.read8(REG_CR_EXT + 3) & 0xF0) | 0x0C
    transport.write8(REG_CR_EXT + 3, v8 & 0xFF)

    # disable boot-from-flash for driver's FW download
    tmp = transport.read32(REG_MCUFW_CTRL)
    if tmp & BIT_BOOT_FSPI_EN:
        transport.write32(REG_MCUFW_CTRL, tmp & ~BIT_BOOT_FSPI_EN & 0xFFFFFFFF)
        v = transport.read32(REG_GPIO_MUXCFG) & ~BIT_FSPI_EN
        transport.write32(REG_GPIO_MUXCFG, v & 0xFFFFFFFF)


def mac_power_on(transport: RTL8822BUTransport,
                 *, cut_mask: int | None = None) -> None:
    """Full power-on flow (mac.c:378..406)."""
    if cut_mask is None:
        from .constants import REG_SYS_CFG1
        chip_version = transport.read32(REG_SYS_CFG1)
        cut_mask = cut_mask_from_sys_cfg1(chip_version)
        logger.debug("rtw_mac_power_on: chip_version=0x%08x cut_mask=0x%02x",
                    chip_version, cut_mask)

    rtw_mac_pre_system_cfg(transport)

    ret = rtw_mac_power_switch(transport, True, cut_mask=cut_mask)
    if ret == -114:  # -EALREADY
        # Cycle: off, then on (mac.c:387..396).
        logger.debug("rtw_mac_power_switch returned -EALREADY; cycling off→on")
        rtw_mac_power_switch(transport, False, cut_mask=cut_mask)
        rtw_mac_pre_system_cfg(transport)
        ret = rtw_mac_power_switch(transport, True, cut_mask=cut_mask)
        if ret != 0:
            raise IOError(f"rtw_mac_power_switch(on) after cycle failed: {ret}")
    elif ret != 0:
        raise IOError(f"rtw_mac_power_switch(on) failed: {ret}")

    rtw_mac_init_system_cfg(transport)


def mac_init_for_rx(transport: RTL8822BUTransport) -> None:
    """Minimum MAC init to enable RX delivery, for 8822b on USB (3 bulk-OUTs).

    Mirrors the RX-relevant subset of `rtw_mac_init`:
      - txdma_queue_mapping  (REG_TXDMA_PQ_MAP + REG_CR=MAC_TRX_ENABLE)
      - rtw8822b_mac_init RX filter writes (REG_RXFLTMAP*, REG_RCR)
      - rtw_drv_info_cfg     (REG_RX_DRVINFO_SZ, REG_RCR |= BIT_APP_PHYSTS,
                              REG_WMAC_OPTION_FUNCTION+4 clear bits 8|9)
      - rtw_usb_init_burst_pkt_len (REG_RXDMA_MODE, REG_TXDMA_OFFSET_CHK)

    Skips the EDCA/AMPDU/beacon/RTS/CCA settings — those affect TX timing
    but not RX delivery.
    """
    from wifit3.chips.rtw88_base.registers import (
        BIT_HCI_RXDMA_EN,
        BIT_HCI_TXDMA_EN,
        BIT_MACRXEN,
        BIT_MACTXEN,
        BIT_RXDMA_EN,
        BIT_TXDMA_EN,
        REG_RXDMA_MODE,
        REG_TXDMA_OFFSET_CHK,
    )

    REG_RXFLTMAP0 = 0x06A0
    REG_RXFLTMAP2 = 0x06A4
    REG_RCR = 0x0608
    BIT_APP_PHYSTS = 1 << 28
    REG_RX_DRVINFO_SZ = 0x060F
    PHY_STATUS_SIZE = 4
    REG_WMAC_OPTION_FUNCTION = 0x07D0
    REG_TXDMA_PQ_MAP = 0x010C
    BIT_RXDMA_ARBBW_EN = 1 << 0
    REG_CR = 0x0100
    # rtw88 protocol bits in REG_CR (low byte only — RX needs these)
    BIT_PROTOCOL_EN = 1 << 4
    BIT_SCHEDULE_EN = 1 << 5
    MAC_TRX_ENABLE_BYTE0 = (
        BIT_HCI_TXDMA_EN | BIT_HCI_RXDMA_EN | BIT_TXDMA_EN | BIT_RXDMA_EN
        | BIT_PROTOCOL_EN | BIT_SCHEDULE_EN | BIT_MACTXEN | BIT_MACRXEN
    )

    # rqpn_table_8822b[3] (3 bulk-OUTs) — dma_map[be,bk,vi,vo,mg,hi] =
    # NORMAL, NORMAL, LOW, LOW, HIGH, HIGH = 2,2,1,1,3,3
    # REG_TXDMA_PQ_MAP layout:
    #   VOQ bits 5..4, VIQ bits 7..6, BEQ bits 9..8, BKQ bits 11..10,
    #   MGQ bits 13..12, HIQ bits 15..14
    txdma_pq_map = (
        (1 << 4)        # VOQ = LOW
        | (1 << 6)      # VIQ = LOW
        | (2 << 8)      # BEQ = NORMAL
        | (2 << 10)     # BKQ = NORMAL
        | (3 << 12)     # MGQ = HIGH
        | (3 << 14)     # HIQ = HIGH
    )
    transport.write16(REG_TXDMA_PQ_MAP, txdma_pq_map)

    transport.write8(REG_CR, 0)
    transport.write8(REG_CR, MAC_TRX_ENABLE_BYTE0 & 0xFF)
    transport.write8_set(REG_TXDMA_PQ_MAP, BIT_RXDMA_ARBBW_EN)

    # RX filter + RCR (rtw8822b_mac_init pieces)
    transport.write32(REG_RXFLTMAP0, 0x0FFFFFFF)
    transport.write16(REG_RXFLTMAP2, 0xFFFF)
    transport.write32(REG_RCR, 0xE400220E)

    # drv_info_cfg
    transport.write8(REG_RX_DRVINFO_SZ, PHY_STATUS_SIZE)
    transport.write32_set(REG_RCR, BIT_APP_PHYSTS)
    transport.write32_clr(REG_WMAC_OPTION_FUNCTION + 4, (1 << 8) | (1 << 9))

    # rtw_usb_init_burst_pkt_len — USB-HS uses BURST_SIZE_512 (=1).
    # rxdma = BIT_DMA_BURST_CNT | BIT_DMA_MODE; merge in burst_size at bits 5:4.
    BIT_DMA_MODE = 1 << 1
    BIT_DMA_BURST_CNT = (1 << 2) | (1 << 3)
    BIT_DMA_BURST_SIZE_512 = 1
    rxdma = BIT_DMA_BURST_CNT | BIT_DMA_MODE
    rxdma |= (BIT_DMA_BURST_SIZE_512 << 4) & 0x30
    transport.write8(REG_RXDMA_MODE, rxdma & 0xFF)
    BIT_DROP_DATA_EN = 1 << 9
    transport.write16(REG_TXDMA_OFFSET_CHK,
                      transport.read16(REG_TXDMA_OFFSET_CHK) | BIT_DROP_DATA_EN)


# Exact REG_RCR airmon-ng writes for monitor [WIRE captures_rtw88_8822bu/
# capture-1 frames 19191-19205] — identical to rtl8821au's monitor value:
# AAP|APM|AM|AB (promiscuous) with CBSSID_BCN/CBSSID_DATA cleared.
RCR_MONITOR = 0xF410400F


def apply_monitor_rx_filter(transport: RTL8822BUTransport) -> None:
    """Force the monitor RX filter. Called on BOTH cold + warm attach.

    mac_init_for_rx writes the kernel's STA-mode RCR 0xE400220E (AAP/bit0 CLEAR)
    — not promiscuous — so client→AP (ToDS) traffic, incl. the M2/M4 EAPOL a
    4-way needs, is dropped (PMKID still works via M1, which is AP→client).
    The kernel overwrites RCR to 0xf410400f for monitor; we never did. The warm
    path also skips mac_init_for_rx, so applying it here covers both. Same fix
    as rtl8821au. [WIRE 8822bu frames 19191-19205; SRC rtw88 reg.h:502-534]
    """
    REG_RCR = 0x0608
    transport.write32(REG_RCR, RCR_MONITOR)
    rcr = transport.read32(REG_RCR)
    logger.debug(
        "RX filter readback: RCR=0x%08x (AAP=%d CBSSID_DATA=%d)",
        rcr, 1 if rcr & 0x1 else 0, 1 if rcr & (1 << 6) else 0,
    )


# ACK is control subtype 13; bit N of RXFLTMAP1 gates control subtype N. mac_init_for_rx
# leaves RXFLTMAP1=0x0FFF (bit13 clear), so the AP's ACKs to our injects are dropped by
# default; TX-ACK detection opens only bit13.
REG_RXFLTMAP1 = 0x06A2
RXFLTMAP1_ACK = 1 << 13


def admit_ack_frames(transport: RTL8822BUTransport) -> None:
    """RXFLTMAP1 |= BIT(13): let RX see the AP's ACKs to our injects. Off by default."""
    transport.write16(REG_RXFLTMAP1,
                      transport.read16(REG_RXFLTMAP1) | RXFLTMAP1_ACK)


def drop_ack_frames(transport: RTL8822BUTransport) -> None:
    """Clear RXFLTMAP1 BIT(13) — restore the default monitor ctrl filter."""
    transport.write16(REG_RXFLTMAP1,
                      transport.read16(REG_RXFLTMAP1) & ~RXFLTMAP1_ACK)


def init_priority_queue_8822b(transport: RTL8822BUTransport) -> None:
    """Port of mac.c:1192..1230 `__priority_queue_cfg` for 8822b on USB.

    Sets up FIFO page tables + auto LLT init. Required before MGMT/data TX
    works on the chip — otherwise the MGMT queue has no pages allocated and
    bulk-OUT to the MGMT lane stalls.

    Constants:
      txff_size = 262144 (rtw8822b.c:2532)
      page_size = TX_PAGE_SIZE = 128 (main.h:34)
      → txff_pg_num = 262144 / 128 = 2048

      rsvd_pg_num (non-8051) = rsvd_drv(8) + EXTRAINFO(24) + STATICINFO(8)
                              + H2CQ(8) + CPU_INSTR(0) + FW_TXBUF(4) + csi(0)
                            = 52
      acq_pg_num = 2048 - 52 = 1996
      rsvd_boundary = 1996

      page_table[3] (3 bulk-OUTs) = {hq=64, lq=64, nq=64, exq=0, gapq=1}
      pubq_num = 1996 - 64 - 64 - 64 - 0 - 1 = 1803
    """
    REG_FIFOPAGE_INFO_1 = 0x0230
    REG_FIFOPAGE_INFO_2 = 0x0234
    REG_FIFOPAGE_INFO_3 = 0x0238
    REG_FIFOPAGE_INFO_4 = 0x023C
    REG_FIFOPAGE_INFO_5 = 0x0240
    REG_RQPN_CTRL_2 = 0x022C
    REG_FIFOPAGE_CTRL_2 = 0x0204
    REG_FWHW_TXQ_CTRL = 0x0420
    REG_BCNQ_BDNY_V1 = 0x0424
    REG_BCNQ1_BDNY_V1 = 0x0456
    REG_RXFF_BNDY = 0x011C
    REG_AUTO_LLT_V1 = 0x0208
    REG_TXDMA_OFFSET_CHK = 0x020C
    REG_CR = 0x0100
    BIT_LD_RQPN = 1 << 31
    BIT_EN_WR_FREE_TAIL = 1 << 20
    BIT_MASK_BLK_DESC_NUM = 0xF << 4
    BIT_AUTO_INIT_LLT_V1 = 1 << 0
    C2H_PKT_BUF = 256
    USB_TX_AGG_DESC_NUM = 3            # rtw8822b.c:2544
    RXFF_SIZE = 24576

    rsvd_boundary = 1996               # see docstring math

    transport.write16(REG_FIFOPAGE_INFO_1, 64)   # hq_num
    transport.write16(REG_FIFOPAGE_INFO_2, 64)   # lq_num
    transport.write16(REG_FIFOPAGE_INFO_3, 64)   # nq_num
    transport.write16(REG_FIFOPAGE_INFO_4, 0)    # exq_num
    transport.write16(REG_FIFOPAGE_INFO_5, 1803) # pubq_num

    transport.write32_set(REG_RQPN_CTRL_2, BIT_LD_RQPN)

    transport.write16(REG_FIFOPAGE_CTRL_2, rsvd_boundary)
    # BIT_EN_WR_FREE_TAIL = BIT(20) → bit 4 of REG_FWHW_TXQ_CTRL+2
    transport.write8_set(REG_FWHW_TXQ_CTRL + 2, (BIT_EN_WR_FREE_TAIL >> 16) & 0xFF)

    transport.write16(REG_BCNQ_BDNY_V1, rsvd_boundary)
    transport.write16(REG_FIFOPAGE_CTRL_2 + 2, rsvd_boundary)
    transport.write16(REG_BCNQ1_BDNY_V1, rsvd_boundary)
    transport.write32(REG_RXFF_BNDY, RXFF_SIZE - C2H_PKT_BUF - 1)

    # USB-specific
    # rtw_write8_mask(REG_AUTO_LLT_V1, BIT_MASK_BLK_DESC_NUM, usb_tx_agg_desc_num)
    cur = transport.read8(REG_AUTO_LLT_V1)
    cur = (cur & ~BIT_MASK_BLK_DESC_NUM) | ((USB_TX_AGG_DESC_NUM << 4) & BIT_MASK_BLK_DESC_NUM)
    transport.write8(REG_AUTO_LLT_V1, cur & 0xFF)

    transport.write8(REG_AUTO_LLT_V1 + 3, USB_TX_AGG_DESC_NUM)
    transport.write8_set(REG_TXDMA_OFFSET_CHK + 1, 1 << 1)

    # Trigger auto LLT init + poll for completion
    transport.write8_set(REG_AUTO_LLT_V1, BIT_AUTO_INIT_LLT_V1)
    for _ in range(200):
        if (transport.read8(REG_AUTO_LLT_V1) & BIT_AUTO_INIT_LLT_V1) == 0:
            break
        import time as _t
        _t.sleep(0.001)
    else:
        raise IOError("BIT_AUTO_INIT_LLT_V1 didn't clear — LLT init failed")

    transport.write8(REG_CR + 3, 0)


def is_chip_warm(transport: RTL8822BUTransport) -> bool:
    """Detect whether the chip is already running FW from a prior session.

    Mirrors the 8821au pattern: a warm chip has FW_READY bits set in
    REG_MCUFW_CTRL and MACTXEN|MACRXEN set in REG_CR. For the modern
    iDDMA-path FW we look at BIT_FW_INIT_RDY | BIT_FW_DW_RDY, which are
    only set after `download_firmware_end_flow` + `wlan_cpu_enable(true)`.
    """
    try:
        mcuctrl = transport.read32(REG_MCUFW_CTRL)
        cr = transport.read32(REG_CR)
    except Exception:
        return False
    fw_ready = (mcuctrl & (BIT_FW_INIT_RDY | BIT_FW_DW_RDY)) == (
        BIT_FW_INIT_RDY | BIT_FW_DW_RDY
    )
    mac_enabled = (cr & (BIT_MACTXEN | BIT_MACRXEN)) == (BIT_MACTXEN | BIT_MACRXEN)
    logger.debug(
        "is_chip_warm: MCUFW_CTRL=0x%08x CR=0x%08x fw_ready=%s mac_en=%s",
        mcuctrl, cr, fw_ready, mac_enabled,
    )
    return fw_ready and mac_enabled
