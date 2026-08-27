"""RTL8821AU BB/RF init + band switch (M4c).

Direct port of `rtw88xxa_phy_bb_config`, `rtw88xxa_phy_rf_config`,
`rtw88xxa_switch_band` (2.4 GHz / 20 MHz) and the surrounding inline
pokes in `rtw88xxa_power_on` lines 1083..1217.

Reference (rtw88-source-v6.18):
    rtw88xxa.c:572   rtw88xxa_phy_bb_config
    rtw88xxa.c:602   rtw88xxa_phy_rf_config
    rtw88xxa.c:766   rtw8821a_set_ext_band_switch
    rtw88xxa.c:779   rtw8821a_phy_set_rfe_reg_24g
    rtw88xxa.c:927   rtw88xxa_switch_band
    phy.c:1029       rtw_phy_write_rf_reg_sipi
    phy.c:1817       rtw_phy_cfg_bb (with delay-magic addrs)

Without EFUSE the runtime cond is plumbed via :class:`EfuseDefaults` —
sensible "blank EFUSE" assumptions: rfe=0, no ext LNA/PA, no btcoex,
crystal_cap=0, tx_bb_swing=0 (swing2setting=0x200). M4d will read EFUSE
and override.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .assets import agc_tbl, bb_tbl, mac_tbl, rf_a_tbl
from .constants import (
    BASIC_RATES_2G,
    BB_SWING_2G_DEFAULT,
    BB_SWING_MASK,
    BIT_CCK_RPT_FORMAT,
    BIT_CHECK_CCK_EN,
    BIT_DPDT_SEL_EN,
    BIT_DPDT_WL_SEL,
    BIT_FEN_BB_GLB_RST,
    BIT_FEN_BB_RSTB,
    BIT_FEN_USBA,
    BIT_RF_EN,
    BIT_RF_RSTB,
    BIT_RF_SDM_RSTB,
    BIT_RX_PSEL_RST,
    REG_ACLK_MON,
    REG_AFE_CTRL3,
    REG_BAR_MODE_CTRL,
    REG_CCK_CHECK,
    REG_CCK_RPT_FORMAT,
    REG_CCK_RX,
    REG_EARLY_MODE_CONTROL,
    REG_FWHW_TXQ_CTRL,
    REG_GPIO_MUXCFG,
    REG_HWSEQ_CTRL,
    REG_LED_CFG,
    REG_LSSI_WRITE_A,
    REG_NAV_CTRL,
    REG_QUEUE_CTRL,
    REG_RF_B_CTRL,
    REG_RF_CTRL,
    REG_RFE_INV_A,
    REG_RFE_PINMUX_A,
    REG_RRSR,
    REG_RXPSEL,
    REG_SYS_FUNC_EN,
    REG_SYS_SDIO_CTRL,
    REG_TX_RPT_TIME,
    REG_TXPSEL,
    REG_TXSCALE_A,
    REG_TXSCALE_B,
    REG_USB_HRPWM,
    RFREG_MASK,
    RTW_SEC_CMD_REG,
)
from .phy_cond import (
    INTF_USB,
    RTW_CHIP_TYPE_8821A,
    DeviceCond,
    PhyCond2,
    parse_tbl_phy_cond,
)
from .transport import RTL8821AUTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EfuseDefaults:
    """Best-known stand-ins until EFUSE is actually read (M4d).

    Mirrors the fields of `rtw_efuse` consumed by power_on/switch_band:
        cut             → assumed unknown → 15
        rfe_option      → 0
        btcoex          → False
        ant_div_cfg     → 0
        ext_lna_2g/5g   → 0
        ext_pa_2g/5g    → 0
        crystal_cap     → 0
        tx_bb_swing_2g  → 0 (index 0 → swing 0x200)
    """
    cut: int = 15
    rfe_option: int = 0
    btcoex: bool = False
    ant_div_cfg: int = 0
    ext_lna_2g: int = 0
    ext_pa_2g: int = 0
    ext_lna_5g: int = 0
    ext_pa_5g: int = 0
    crystal_cap: int = 0
    tx_bb_swing_2g: int = 0


def device_cond(efuse: EfuseDefaults) -> DeviceCond:
    """Build the runtime cond used by the phy_cond walker.

    Mirrors `rtw_phy_setup_phy_cond` (phy.c:1103) for 8821A.
    """
    rfe = 0
    rfe |= efuse.ext_lna_2g
    rfe |= efuse.ext_pa_2g << 1
    rfe |= efuse.ext_lna_5g << 2
    rfe |= efuse.ext_pa_5g << 3
    rfe |= (1 if efuse.btcoex else 0) << 4
    return DeviceCond(
        cut=efuse.cut, pkg=15, intf=INTF_USB, rfe=rfe, cond2=PhyCond2()
    )


# ---------------------------------------------------------------------------
# rtw_phy_cfg_* dispatchers — what `do_cfg` does for each table type.
# ---------------------------------------------------------------------------

def _cfg_mac(transport: RTL8821AUTransport, addr: int, data: int) -> None:
    transport.write8(addr, data & 0xFF)


def _cfg_bb(transport: RTL8821AUTransport, addr: int, data: int) -> None:
    """Mirrors rtw_phy_cfg_bb (phy.c:1817) — handles delay-magic addrs."""
    if addr == 0xFE:
        time.sleep(0.050)
    elif addr == 0xFD:
        time.sleep(0.005)
    elif addr == 0xFC:
        time.sleep(0.001)
    elif addr == 0xFB:
        time.sleep(50e-6)
    elif addr == 0xFA:
        time.sleep(5e-6)
    elif addr == 0xF9:
        time.sleep(1e-6)
    else:
        transport.write32(addr, data)


def _cfg_agc(transport: RTL8821AUTransport, addr: int, data: int) -> None:
    transport.write32(addr, data)


def _cfg_rf(transport: RTL8821AUTransport, addr: int, data: int) -> None:
    """Mirrors rtw_phy_cfg_rf (phy.c:1837) for path A using SIPI."""
    if addr == 0xFFE:
        time.sleep(0.050)
    elif addr == 0xFE:
        time.sleep(100e-6)
    else:
        # rtw_phy_write_rf_reg_sipi (phy.c:1029), mask = RFREG_MASK case:
        #   data_and_addr = ((addr << 20) | (data & 0xFFFFF)) & 0x0FFFFFFF
        #   write32(REG_LSSI_WRITE_A, data_and_addr); udelay(13)
        data_and_addr = (((addr & 0xFF) << 20) | (data & RFREG_MASK)) & 0x0FFFFFFF
        transport.write32(REG_LSSI_WRITE_A, data_and_addr)
        time.sleep(13e-6)


# ---------------------------------------------------------------------------
# Table loaders.
# ---------------------------------------------------------------------------

def load_mac_table(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> int:
    """Load `rtw8821a_mac` — 98 write8s of MAC-side defaults."""
    n = parse_tbl_phy_cond(
        mac_tbl.TABLE, device_cond(efuse),
        lambda a, d: _cfg_mac(transport, a, d),
        chip_id=RTW_CHIP_TYPE_8821A,
    )
    logger.debug("mac_tbl: dispatched %d write8 ops", n)
    return n


def load_bb_table(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> int:
    n = parse_tbl_phy_cond(
        bb_tbl.TABLE, device_cond(efuse),
        lambda a, d: _cfg_bb(transport, a, d),
        chip_id=RTW_CHIP_TYPE_8821A,
    )
    logger.debug("bb_tbl: dispatched %d write32 ops", n)
    return n


def load_agc_table(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> int:
    n = parse_tbl_phy_cond(
        agc_tbl.TABLE, device_cond(efuse),
        lambda a, d: _cfg_agc(transport, a, d),
        chip_id=RTW_CHIP_TYPE_8821A,
    )
    logger.debug("agc_tbl: dispatched %d write32 ops", n)
    return n


def load_rf_a_table(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> int:
    n = parse_tbl_phy_cond(
        rf_a_tbl.TABLE, device_cond(efuse),
        lambda a, d: _cfg_rf(transport, a, d),
        chip_id=RTW_CHIP_TYPE_8821A,
    )
    logger.debug("rf_a_tbl: dispatched %d RF SIPI writes", n)
    return n


# ---------------------------------------------------------------------------
# rtw88xxa_phy_bb_config / phy_rf_config / switch_band
# ---------------------------------------------------------------------------

def phy_bb_config(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> None:
    """Mirrors rtw88xxa_phy_bb_config (rtw88xxa.c:572)."""
    val8 = transport.read8(REG_SYS_FUNC_EN)
    val8 |= BIT_FEN_USBA
    transport.write8(REG_SYS_FUNC_EN, val8)
    val8 |= BIT_FEN_BB_RSTB | BIT_FEN_BB_GLB_RST
    transport.write8(REG_SYS_FUNC_EN, val8)

    transport.write8(REG_RF_CTRL, BIT_RF_EN | BIT_RF_RSTB | BIT_RF_SDM_RSTB)
    transport.write8(REG_RF_B_CTRL, BIT_RF_EN | BIT_RF_RSTB | BIT_RF_SDM_RSTB)

    load_bb_table(transport, efuse)
    load_agc_table(transport, efuse)

    crystal_cap = efuse.crystal_cap & 0x3F
    # 8821A path: mask = 0x00FFF000
    transport.write32_mask(
        REG_AFE_CTRL3, 0x00FFF000, crystal_cap | (crystal_cap << 6)
    )


def phy_rf_config(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> None:
    """Mirrors rtw88xxa_phy_rf_config (rtw88xxa.c:602). 8821A has 1 RF path."""
    load_rf_a_table(transport, efuse)


# ---------------------------------------------------------------------------
# 2.4 GHz band switch
# ---------------------------------------------------------------------------

def _set_ext_band_switch_2g(transport: RTL8821AUTransport) -> None:
    """Mirrors rtw8821a_set_ext_band_switch (rtw88xxa.c:766) for 2G."""
    transport.write32_mask(REG_LED_CFG, BIT_DPDT_SEL_EN, 0)
    transport.write32_mask(REG_LED_CFG, BIT_DPDT_WL_SEL, 1)
    transport.write32_mask(REG_RFE_INV_A, 0x0F, 7)
    transport.write32_mask(REG_RFE_INV_A, 0xF0, 7)
    transport.write32_mask(REG_RFE_INV_A, (1 << 29) | (1 << 28), 1)


def _phy_set_rfe_reg_24g(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> None:
    """Mirrors rtw8821a_phy_set_rfe_reg_24g (rtw88xxa.c:779)."""
    transport.write32_mask(REG_RFE_PINMUX_A, 0xF000, 0x7)
    transport.write32_mask(REG_RFE_PINMUX_A, 0xF0, 0x7)
    if efuse.ext_lna_2g:
        transport.write32_mask(REG_RFE_INV_A, 1 << 20, 1)
        transport.write32_mask(REG_RFE_INV_A, 1 << 22, 0)
        transport.write32_mask(REG_RFE_PINMUX_A, 0x07, 0x2)
        transport.write32_mask(REG_RFE_PINMUX_A, 0x0700, 0x2)
    else:
        transport.write32_mask(REG_RFE_INV_A, 1 << 20, 0)
        transport.write32_mask(REG_RFE_INV_A, 1 << 22, 0)
        transport.write32_mask(REG_RFE_PINMUX_A, 0x07, 0x7)
        transport.write32_mask(REG_RFE_PINMUX_A, 0x0700, 0x7)


def _phy_set_rfe_reg_5g(transport: RTL8821AUTransport) -> None:
    """Mirrors rtw8821a_phy_set_rfe_reg_5g (rtw88xxa.c:805).

    Turn ON the 5G RF PA and LNA paths; bypass the 2G external LNA.
    """
    transport.write32_mask(REG_RFE_PINMUX_A, 0xF000, 0x5)
    transport.write32_mask(REG_RFE_PINMUX_A, 0xF0,   0x4)
    transport.write32_mask(REG_RFE_INV_A, 1 << 20, 0)
    transport.write32_mask(REG_RFE_INV_A, 1 << 22, 0)
    transport.write32_mask(REG_RFE_PINMUX_A, 0x07,   0x7)
    transport.write32_mask(REG_RFE_PINMUX_A, 0x0700, 0x7)


def _set_ext_band_switch_5g(transport: RTL8821AUTransport) -> None:
    """5 GHz branch of rtw8821a_set_ext_band_switch (rtw88xxa.c:766).

    Same prelude as 2G; differs in REG_RFE_INV_A BIT(29)|BIT(28) = 2.
    """
    transport.write32_mask(REG_LED_CFG, BIT_DPDT_SEL_EN, 0)
    transport.write32_mask(REG_LED_CFG, BIT_DPDT_WL_SEL, 1)
    transport.write32_mask(REG_RFE_INV_A, 0x0F, 7)
    transport.write32_mask(REG_RFE_INV_A, 0xF0, 7)
    transport.write32_mask(REG_RFE_INV_A, (1 << 29) | (1 << 28), 2)


def _set_channel_bb_swing(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> None:
    """Mirrors rtw88xxa_set_channel_bb_swing (rtw88xxa.c:757).

    Same code on both bands — the only band-specific bit is which EFUSE
    swing setting is read, but with rfe=0 defaults both bands collapse to
    BB_SWING_2G_DEFAULT (0x200). pwrtrack_init is deferred to M4d.
    """
    swing = BB_SWING_2G_DEFAULT
    transport.write32_mask(REG_TXSCALE_A, BB_SWING_MASK, swing)
    transport.write32_mask(REG_TXSCALE_B, BB_SWING_MASK, swing)


def switch_band_2g_20mhz(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> None:
    """Mirrors rtw88xxa_switch_band(2G, 20MHz) for 8821A (rtw88xxa.c:927)."""
    if (not efuse.btcoex) and efuse.ant_div_cfg == 0:
        _set_ext_band_switch_2g(transport)

    transport.write32_set(REG_RXPSEL, BIT_RX_PSEL_RST)
    _phy_set_rfe_reg_24g(transport, efuse)
    transport.write32_mask(REG_TXSCALE_A, 0xF00, 0)

    transport.write32_mask(REG_TXPSEL, 0xF0, 0x1)
    transport.write32_mask(REG_CCK_RX, 0x0F000000, 0x1)
    transport.write32_mask(REG_RRSR, 0xFFFFF, BASIC_RATES_2G)
    transport.write8_clr(REG_CCK_CHECK, BIT_CHECK_CCK_EN)

    _set_channel_bb_swing(transport, efuse)


def switch_band_5g_20mhz(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> None:
    """Mirrors rtw88xxa_switch_band(5G, 20MHz) for 8821A (rtw88xxa.c:972).

    Differs from the 2G path in:
      * ext_band_switch writes REG_RFE_INV_A bits[29:28]=2 (vs 1)
      * REG_CCK_CHECK gets BIT_CHECK_CCK_EN *set* (vs clear)
      * Polls REG_TXPKT_EMPTY for TX-drained state (50us * 50 attempts)
      * REG_TXSCALE_A bits[11:8] = 1 (vs 0)
      * REG_TXPSEL bits[7:4] = 0 (vs 1)
      * REG_CCK_RX bits[27:24] = 0xF (vs 1)
      * basic_rates: only OFDM 6/12/24 (CCK not on 5 GHz)
    """
    if (not efuse.btcoex) and efuse.ant_div_cfg == 0:
        _set_ext_band_switch_5g(transport)

    _phy_set_rfe_reg_5g(transport)

    transport.write8_set(REG_CCK_CHECK, BIT_CHECK_CCK_EN)
    _poll_txpkt_empty(transport)

    transport.write32_set(REG_RXPSEL, BIT_RX_PSEL_RST)
    transport.write32_mask(REG_TXSCALE_A, 0xF00, 1)

    transport.write32_mask(REG_TXPSEL, 0xF0, 0)
    transport.write32_mask(REG_CCK_RX, 0x0F000000, 0xF)
    # OFDM 6M | 12M | 24M
    basic_rates_5g = (1 << 4) | (1 << 6) | (1 << 8)
    transport.write32_mask(REG_RRSR, 0xFFFFF, basic_rates_5g)

    _set_channel_bb_swing(transport, efuse)


REG_TXPKT_EMPTY = 0x041A   # reg.h:386


def _poll_txpkt_empty(transport: RTL8821AUTransport,
                      max_attempts: int = 50, interval_s: float = 50e-6) -> None:
    """Wait for REG_TXPKT_EMPTY bits[5:4] = 0x3 (both HI/MGT queues drained).

    Mirrors the read_poll_timeout_atomic in rtw88xxa.c:978. 50 attempts at
    ~50us each = 2.5 ms budget. The kernel uses ``(reg & 0x30) == 0x30``.
    """
    for _ in range(max_attempts):
        if transport.read16(REG_TXPKT_EMPTY) & 0x30 == 0x30:
            return
        time.sleep(interval_s)
    logger.warning("TXPKT_EMPTY poll timed out before band switch (continuing)")


# ---------------------------------------------------------------------------
# The orchestrator: lines 1083..1217 of rtw88xxa_power_on (MAC tbl → MACTXEN
# was M4b; this picks up at mac_tbl load and continues through the inline
# pokes at the bottom).
# ---------------------------------------------------------------------------

def post_mac_init_phy(transport: RTL8821AUTransport, efuse: EfuseDefaults) -> None:
    """Run the BB/RF/band-switch chunk after M4b's post_fw_mac_init.

    Covers:
      * line 1083 — mac_tbl load (deferred from M4b)
      * lines 1177-1183 — phy_bb_config, phy_rf_config, switch_band(2G, 20MHz)
      * lines 1185-1191 — security/sequencing/BAR/NAV/GPIO inline pokes
      * lines 1200-1217 — queue/early-mode/USB inline pokes

    Skips (deferred to M4d):
      * rtw_phy_init (DIG calibration loop init)
      * rtw88xxa_pwrtrack_init (pure software state)
    """
    # 1083 — mac_tbl load
    load_mac_table(transport, efuse)

    # 1177-1178 — BB + RF tables
    phy_bb_config(transport, efuse)
    phy_rf_config(transport, efuse)

    # 1183 — switch to 2.4 GHz, 20 MHz
    switch_band_2g_20mhz(transport, efuse)

    # 1185-1191 — inline pokes
    transport.write32(RTW_SEC_CMD_REG, (1 << 31) | (1 << 30))
    transport.write8(REG_HWSEQ_CTRL, 0xFF)
    transport.write32(REG_BAR_MODE_CTRL, 0x0201FFFF)
    transport.write8(REG_NAV_CTRL + 2, 0)

    cur = transport.read8(REG_GPIO_MUXCFG)
    transport.write8(REG_GPIO_MUXCFG, cur & ~(1 << 5) & 0xFF)

    # 1193 — rtw_phy_init: SKIPPED (M4d)
    # 1195 — pwrtrack_init: SKIPPED (M4d)

    # 1200 — REG_QUEUE_CTRL clr BIT(3)
    cur = transport.read8(REG_QUEUE_CTRL)
    transport.write8(REG_QUEUE_CTRL, cur & ~(1 << 3) & 0xFF)

    # 1203 — REG_FWHW_TXQ_CTRL + 1 = 0x0f
    transport.write8(REG_FWHW_TXQ_CTRL + 1, 0x0F)
    # 1206 — REG_EARLY_MODE_CONTROL + 3 = 0x01
    transport.write8(REG_EARLY_MODE_CONTROL + 3, 0x01)
    # 1208 — REG_TX_RPT_TIME = 0x3df0
    transport.write16(REG_TX_RPT_TIME, 0x3DF0)
    # 1211-1212 — reset USB-mode-switch leftovers
    transport.write8(REG_SYS_SDIO_CTRL, 0)
    transport.write8(REG_ACLK_MON, 0)
    # 1214 — REG_USB_HRPWM = 0
    transport.write8(REG_USB_HRPWM, 0)
    # 1217 — REG_FWHW_TXQ_CTRL set BIT(12) (byte 1, BIT(4))
    transport.write8_set(REG_FWHW_TXQ_CTRL + 1, 1 << 4)

    # 1219 — read cck_high_power (informational, mac80211 uses it later).
    val = transport.read32(REG_CCK_RPT_FORMAT)
    logger.debug(
        "cck_high_power flag = %d (REG_CCK_RPT_FORMAT=0x%08x)",
        1 if val & BIT_CCK_RPT_FORMAT else 0, val,
    )
