"""RTL8814AU MAC power-on (modern WCPU_3081 / non-8051 path).

Mirrors the kernel sequence:

    rtw_mac_power_on(rtwdev):
      rtw_mac_pre_system_cfg(rtwdev)      # mac.c:62..137
      rtw_mac_power_switch(rtwdev, true)  # mac.c:272..328 (runs pwr_seq tables)
      rtw_mac_init_system_cfg(rtwdev)     # mac.c:330..353

The 8814A is a WCPU_3081 chip (like the 8822b), so pre_system_cfg and
init_system_cfg are byte-identical to the 8822bu port. The ONLY divergence is
in `rtw_mac_power_switch`, which carves the 8814A out of two USB branches
(mac.c:293-295 and mac.c:314-318) — see the comments below.

M1 covers power-on only. FIFO/queue config (priority_queue) and RX-side MAC
init land in M2.
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
    REG_SYS_CFG1,
    REG_SYS_FUNC_EN,
    REG_WLRF1,
)

from .constants import SYS_FUNC_EN_8814A
from .power_seq import card_disable_flow_8814a, card_enable_flow_8814a
from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)

_EALREADY = -114


def cut_mask_from_sys_cfg1(chip_version: int) -> int:
    """Mirror `cut_version_to_mask` (mac.h:9): `0x1 << (cut + 1)`.

    `chip_version` is the full REG_SYS_CFG1 read; the cut nibble is bits 12..15.
    """
    cut = (chip_version >> 12) & 0xF
    return 0x1 << (cut + 1)


def rtw_mac_pre_system_cfg(transport: RTL8814AUTransport) -> None:
    """mac.c:62..137 — the non-8051, USB-only path (identical to 8822b)."""
    logger.debug("rtw_mac_pre_system_cfg (modern, USB)")
    transport.write8(REG_RSV_CTRL, 0)

    # USB branch (mac.c:104) is empty — no SDIO/PCIE writes.

    # config PIN Mux (mac.c:110..121)
    v = transport.read32(REG_PAD_CTRL1)
    v |= BIT_PAPE_WLBT_SEL | BIT_LNAON_WLBT_SEL
    transport.write32(REG_PAD_CTRL1, v)

    v = transport.read32(REG_LED_CFG)
    v &= ~(BIT_PAPE_SEL_EN | BIT_LNAON_SEL_EN)
    transport.write32(REG_LED_CFG, v & 0xFFFFFFFF)

    v = transport.read32(REG_GPIO_MUXCFG)
    v |= BIT_WLRFE_4_5_EN
    transport.write32(REG_GPIO_MUXCFG, v)

    # disable BB/RF (mac.c:123..134)
    v8 = transport.read8(REG_SYS_FUNC_EN)
    v8 &= ~(BIT_FEN_BB_RSTB | BIT_FEN_BB_GLB_RST)
    transport.write8(REG_SYS_FUNC_EN, v8 & 0xFF)

    v8 = transport.read8(REG_RF_CTRL)
    v8 &= ~(BIT_RF_SDM_RSTB | BIT_RF_RSTB | BIT_RF_EN)
    transport.write8(REG_RF_CTRL, v8 & 0xFF)

    v = transport.read32(REG_WLRF1)
    v &= ~BIT_WLRF1_BBRF_EN
    transport.write32(REG_WLRF1, v & 0xFFFFFFFF)


def rtw_mac_power_switch(transport: RTL8814AUTransport, pwr_on: bool,
                         *, cut_mask: int = CUT_ALL) -> int:
    """mac.c:272..328, 8814A variant.

    Returns 0 on success, -EALREADY (-114) if already in the requested state.

    8814A deltas vs the 8822b/c/8821c USB path:
      - mac.c:293-295: the `REG_SYS_STATUS1+1 & BIT(0)` "already powered" probe
        is explicitly skipped for the 8814A (`chip->id != RTW_CHIP_TYPE_8814A`),
        so cur_pwr is decided by REG_CR==0xEA alone.
      - mac.c:314-318: the post-power-on `REG_SYS_STATUS1+1` bit-0 clear is only
        for 8822C/8822B/8821C — NOT the 8814A. We skip it.
    """
    if transport.read8(REG_CR) == 0xEA:
        cur_pwr = False
    else:
        cur_pwr = True

    logger.debug("rtw_mac_power_switch: cur_pwr=%s req=%s", cur_pwr, pwr_on)

    if pwr_on == cur_pwr:
        return _EALREADY

    if pwr_on:
        card_enable_flow_8814a(transport, cut_mask=cut_mask)
    else:
        card_disable_flow_8814a(transport, cut_mask=cut_mask)

    return 0


def rtw_mac_init_system_cfg(transport: RTL8814AUTransport) -> None:
    """mac.c:330..353 (non-8051). sys_func_en = 0xDC for 8814a."""
    logger.debug("rtw_mac_init_system_cfg (modern)")

    v = transport.read32(REG_CPU_DMEM_CON)
    v |= BIT_WL_PLATFORM_RST | BIT_DDMA_EN
    transport.write32(REG_CPU_DMEM_CON, v)

    transport.write8_set(REG_SYS_FUNC_EN + 1, SYS_FUNC_EN_8814A)

    v8 = (transport.read8(REG_CR_EXT + 3) & 0xF0) | 0x0C
    transport.write8(REG_CR_EXT + 3, v8 & 0xFF)

    # disable boot-from-flash for driver's FW download
    tmp = transport.read32(REG_MCUFW_CTRL)
    if tmp & BIT_BOOT_FSPI_EN:
        transport.write32(REG_MCUFW_CTRL, tmp & ~BIT_BOOT_FSPI_EN & 0xFFFFFFFF)
        v = transport.read32(REG_GPIO_MUXCFG) & ~BIT_FSPI_EN
        transport.write32(REG_GPIO_MUXCFG, v & 0xFFFFFFFF)


def mac_power_on(transport: RTL8814AUTransport,
                 *, cut_mask: int | None = None) -> None:
    """Full power-on flow (mac.c:378..406)."""
    if cut_mask is None:
        chip_version = transport.read32(REG_SYS_CFG1)
        cut_mask = cut_mask_from_sys_cfg1(chip_version)
        logger.debug("mac_power_on: chip_version=0x%08x cut_mask=0x%02x",
                    chip_version, cut_mask)

    rtw_mac_pre_system_cfg(transport)

    ret = rtw_mac_power_switch(transport, True, cut_mask=cut_mask)
    if ret == _EALREADY:
        logger.info("rtw_mac_power_switch returned -EALREADY; cycling off->on")
        rtw_mac_power_switch(transport, False, cut_mask=cut_mask)
        rtw_mac_pre_system_cfg(transport)
        ret = rtw_mac_power_switch(transport, True, cut_mask=cut_mask)
        if ret != 0:
            raise IOError(f"rtw_mac_power_switch(on) after cycle failed: {ret}")
    elif ret != 0:
        raise IOError(f"rtw_mac_power_switch(on) failed: {ret}")

    rtw_mac_init_system_cfg(transport)


def is_chip_warm(transport: RTL8814AUTransport) -> bool:
    """Detect whether the chip is already running FW from a prior session.

    Modern iDDMA-path FW: FW_INIT_RDY | FW_DW_RDY in REG_MCUFW_CTRL are only
    set after download_firmware_end_flow + wlan_cpu_enable(true), and the MAC
    TX/RX enable bits in REG_CR indicate a live session.
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
