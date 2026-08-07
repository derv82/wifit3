"""Minimal RTL8822C MAC power-on configuration for USB firmware loading."""
from __future__ import annotations

import time

from wifit3.chips.rtw88_base.registers import (
    BIT_BOOT_FSPI_EN,
    BIT_DDMA_EN,
    BIT_FEN_BB_GLB_RST,
    BIT_FEN_BB_RSTB,
    BIT_FSPI_EN,
    BIT_LNAON_SEL_EN,
    BIT_LNAON_WLBT_SEL,
    BIT_PAPE_SEL_EN,
    BIT_PAPE_WLBT_SEL,
    BIT_RF_EN,
    BIT_RF_RSTB,
    BIT_RF_SDM_RSTB,
    BIT_WL_PLATFORM_RST,
    BIT_WLRF1_BBRF_EN,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_CR_EXT,
    REG_GPIO_MUXCFG,
    REG_LED_CFG,
    REG_MCUFW_CTRL,
    REG_PAD_CTRL1,
    REG_RF_CTRL,
    REG_RSV_CTRL,
    REG_SYS_CFG1,
    REG_SYS_CFG2,
    REG_SYS_FUNC_EN,
    REG_SYS_STATUS1,
    REG_WLRF1,
)

from .constants import REG_MACID
from .power_seq import card_disable_flow_8822c, card_enable_flow_8822c
from .transport import RTL8822CUTransport

SYS_FUNC_EN_8822C = 0xD8


def cut_mask_from_sys_cfg1(chip_version: int) -> int:
    return 1 << (((chip_version >> 12) & 0xF) + 1)


def pre_init_system_cfg(transport: RTL8822CUTransport) -> None:
    """HALMAC ``pre_init_system_cfg_8822c`` USB branch."""
    transport.write8(REG_RSV_CTRL, 0)
    if transport.read8(REG_SYS_CFG2 + 3) == 0x20:
        transport.write8_set(0xFE5B, 1 << 4)

    value = transport.read32(REG_PAD_CTRL1)
    transport.write32(REG_PAD_CTRL1, value | BIT_PAPE_WLBT_SEL | BIT_LNAON_WLBT_SEL)
    transport.write32(REG_LED_CFG, transport.read32(REG_LED_CFG) & ~(BIT_PAPE_SEL_EN | BIT_LNAON_SEL_EN))
    transport.write32(REG_GPIO_MUXCFG, transport.read32(REG_GPIO_MUXCFG) | 0x04)

    transport.write8(REG_SYS_FUNC_EN, transport.read8(REG_SYS_FUNC_EN) & ~(BIT_FEN_BB_RSTB | BIT_FEN_BB_GLB_RST))
    transport.write8(REG_RF_CTRL, transport.read8(REG_RF_CTRL) & ~(BIT_RF_SDM_RSTB | BIT_RF_RSTB | BIT_RF_EN))
    transport.write32(REG_WLRF1, transport.read32(REG_WLRF1) & ~BIT_WLRF1_BBRF_EN)
    if transport.read8(REG_SYS_CFG1 + 2) & 0x10:
        raise IOError("RTL8822CU is in test mode")


def _power_switch(transport: RTL8822CUTransport, on: bool, cut_mask: int) -> int:
    """Return -EALREADY for an unchanged state, like the HALMAC API."""
    is_off = transport.read8(REG_CR) == 0xEA or bool(transport.read8(REG_SYS_STATUS1 + 1) & 1)
    if on == (not is_off):
        return -114
    if on:
        card_enable_flow_8822c(transport, cut_mask=cut_mask)
        transport.write8_clr(REG_SYS_STATUS1 + 1, 1)
    else:
        card_disable_flow_8822c(transport, cut_mask=cut_mask)
    return 0


def init_system_cfg(transport: RTL8822CUTransport) -> None:
    """HALMAC ``init_system_cfg_8822c`` for 20 MHz USB operation."""
    transport.write32_set(REG_CPU_DMEM_CON, BIT_WL_PLATFORM_RST | BIT_DDMA_EN)
    transport.write8_set(REG_SYS_FUNC_EN + 1, SYS_FUNC_EN_8822C)
    transport.write8(REG_CR_EXT + 3, (transport.read8(REG_CR_EXT + 3) & 0xF0) | 0x0C)
    fw_ctrl = transport.read32(REG_MCUFW_CTRL)
    if fw_ctrl & BIT_BOOT_FSPI_EN:
        transport.write32(REG_MCUFW_CTRL, fw_ctrl & ~BIT_BOOT_FSPI_EN)
        transport.write32(REG_GPIO_MUXCFG, transport.read32(REG_GPIO_MUXCFG) & ~BIT_FSPI_EN)


def mac_power_on(transport: RTL8822CUTransport, *, cut_mask: int | None = None) -> None:
    if cut_mask is None:
        cut_mask = cut_mask_from_sys_cfg1(transport.read32(REG_SYS_CFG1))
    pre_init_system_cfg(transport)
    if _power_switch(transport, True, cut_mask) == -114:
        _power_switch(transport, False, cut_mask)
        pre_init_system_cfg(transport)
        if _power_switch(transport, True, cut_mask) != 0:
            raise IOError("RTL8822CU power-cycle failed")
    init_system_cfg(transport)


# HALMAC's 8822C normal-mode MAC RX configuration.  These values differ from
# 8822B (notably WLAN_RCR_CFG) and are intentionally kept local to this chip.
RCR_MONITOR = 0xF410400F


def enter_monitor_mode(transport: RTL8822CUTransport) -> None:
    """Apply the RTL8822C vendor driver's monitor-mode RX settings.

    ``cfg_drv_info_8822c(HALMAC_DRV_INFO_PHY_SNIFFER)`` is essential here:
    it asks the firmware/MAC to deliver malformed frames too.  Merely setting
    a promiscuous RCR still leaves the normal RX header/FCS checks enabled.
    """
    REG_MSR = 0x0102
    REG_RCR = 0x0608
    REG_RX_DRVINFO_SZ = 0x060F
    REG_RXFLTMAP0 = 0x06A0
    REG_RXFLTMAP1 = 0x06A2
    REG_RXFLTMAP2 = 0x06A4
    REG_WMAC_OPTION_FUNCTION = 0x07D0

    # hw_var_set_opmode(..., _HW_STATE_MONITOR_) first selects NOLINK.
    transport.write8(REG_MSR, 0)
    # cfg_drv_info_8822c(HALMAC_DRV_INFO_PHY_SNIFFER).
    transport.write8(REG_RX_DRVINFO_SZ, 5)
    transport.write32_set(REG_WMAC_OPTION_FUNCTION + 4, 1 << 9)
    # set_opmode_monitor(): bit 7 enables the raw sniffer report format.
    transport.write8_set(REG_RX_DRVINFO_SZ, 0x80)
    transport.write32(REG_RCR, RCR_MONITOR)
    transport.write16(REG_RXFLTMAP0, 0xFFFF)
    transport.write16(REG_RXFLTMAP1, 0xFFFF)
    transport.write16(REG_RXFLTMAP2, 0xFFFF)


def enable_bb_rf(transport: RTL8822CUTransport) -> None:
    """HALMAC ``enable_bb_rf_88xx(..., 1)`` before writing PHY tables."""
    transport.write8_set(REG_SYS_FUNC_EN, BIT_FEN_BB_RSTB | BIT_FEN_BB_GLB_RST)
    transport.write8_set(REG_RF_CTRL, BIT_RF_SDM_RSTB | BIT_RF_RSTB | BIT_RF_EN)
    transport.write32_set(REG_WLRF1, BIT_WLRF1_BBRF_EN)


def set_mac_addr(transport: RTL8822CUTransport, mac6: bytes) -> None:
    """Program the interface MAC into REG_MACID (low 4 bytes as a dword, high 2 as a word).

    Mirrors ``cfg_mac_addr_88xx`` (halmac_cfg_wmac_88xx.c). The MAC hardware HW-ACKs
    frames addressed to this MAC even while in monitor mode, which is what
    ``enter_active_monitor`` uses to impersonate a client STA.
    """
    if len(mac6) != 6:
        raise ValueError(f"RTL8822CU: malformed MAC address {mac6!r}")
    transport.write32(REG_MACID, int.from_bytes(mac6[0:4], "little"))
    transport.write16(REG_MACID + 4, int.from_bytes(mac6[4:6], "little"))


def init_rx_mac(transport: RTL8822CUTransport) -> None:
    """Initialize the RTL8822C three-bulk-out FIFO and promiscuous RX path."""
    from wifit3.chips.rtw88_base.registers import (
        REG_CR,
        REG_FIFOPAGE_CTRL_2,
        REG_FIFOPAGE_INFO_1,
        REG_FIFOPAGE_INFO_2,
        REG_FIFOPAGE_INFO_3,
        REG_FIFOPAGE_INFO_4,
        REG_FIFOPAGE_INFO_5,
        REG_FWHW_TXQ_CTRL,
        REG_H2CQ_CSR,
        REG_RQPN_CTRL_2,
        REG_RXDMA_MODE,
        REG_TXDMA_OFFSET_CHK,
        REG_TXDMA_PQ_MAP,
    )

    # HALMAC_RQPN_3BULKOUT_8822C normal: VO/VI=NQ, BE/BK=LQ, MG/HI=HQ.
    queue_map = (2 << 4) | (2 << 6) | (1 << 8) | (1 << 10) | (3 << 12) | (3 << 14)
    transport.write16(REG_TXDMA_PQ_MAP, queue_map)
    transport.write8(REG_CR, 0)
    transport.write8(REG_CR, 0xFF)  # MAC_TRX_ENABLE for 20 MHz normal mode.
    transport.write32(REG_H2CQ_CSR, 1 << 31)

    # TX FIFO: 262144 / 128 = 2048 pages.  52 reserved pages leave 1996.
    for reg, pages in ((REG_FIFOPAGE_INFO_1, 64), (REG_FIFOPAGE_INFO_2, 64),
                       (REG_FIFOPAGE_INFO_3, 64), (REG_FIFOPAGE_INFO_4, 0),
                       (REG_FIFOPAGE_INFO_5, 1803)):
        transport.write16(reg, pages)
    transport.write32_set(REG_RQPN_CTRL_2, 1 << 31)
    transport.write16(REG_FIFOPAGE_CTRL_2, 1996)
    transport.write8_set(REG_FWHW_TXQ_CTRL + 2, 1 << 4)
    transport.write16(0x0424, 1996)  # REG_BCNQ_BDNY_V1
    transport.write16(REG_FIFOPAGE_CTRL_2 + 2, 1996)
    transport.write16(0x0456, 1996)  # REG_BCNQ1_BDNY_V1
    transport.write32(0x011C, 24576 - 256 - 1)  # REG_RXFF_BNDY
    auto_llt = 0x0208
    transport.write8(auto_llt, (transport.read8(auto_llt) & ~(0xF << 4)) | (3 << 4))
    transport.write8(auto_llt + 3, 3)
    transport.write8_set(REG_TXDMA_OFFSET_CHK + 1, 1 << 1)
    transport.write8_set(auto_llt, 1)
    for _ in range(1000):
        if not transport.read8(auto_llt) & 1:
            break
        time.sleep(0.001)
    else:
        raise IOError("RTL8822CU auto LLT initialization timed out")
    transport.write8(REG_CR + 3, 0)

    # init_wmac_cfg_8822c, narrowed to monitor capture requirements.
    transport.write32(0x0620, 0xFFFFFFFF)  # MAR low
    transport.write32(0x0624, 0xFFFFFFFF)  # MAR high
    transport.write32(0x06A0, 0xFFFFFFFF)  # RXFLTMAP0
    transport.write16(0x06A2, 0xFFFF)      # RXFLTMAP1
    transport.write16(0x06A4, 0xFFFF)      # RXFLTMAP2
    transport.write8(0x060F, 4)            # 32-byte PHY status report
    transport.write32(0x0608, RCR_MONITOR)
    # USB burst size 512: DMA mode + burst count (rtw_usb_init_burst_pkt_len).
    transport.write8(REG_RXDMA_MODE, 0x1E)
    # cfg_usb_rx_agg_88xx: USB aggregation, 5 pages, 0x20 timeout for
    # USB2.  Without this the RXDMA FIFO never submits frames to bulk-IN.
    rxagg = 0x0280
    transport.write8_set(REG_TXDMA_PQ_MAP, 1 << 2)  # BIT_RXDMA_AGG_EN
    transport.write32_set(rxagg, 1 << 29)           # BIT_EN_PRE_CALC
    transport.write8_clr(rxagg + 3, 1 << 7)         # USB rather than DMA mode
    transport.write16(rxagg, 0x05 | (0x20 << 8))
