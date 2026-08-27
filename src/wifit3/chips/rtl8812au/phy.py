"""RTL8812AU BB/RF init + band switch (M2-d).

Direct port of `rtw88xxa_phy_bb_config`, `rtw88xxa_phy_rf_config`,
`rtw8812a_phy_set_rfe_reg_24g`, and the 8812A path of
`rtw88xxa_switch_band` (2.4 GHz / 20 MHz) plus the surrounding inline
pokes in `rtw88xxa_power_on` lines 1083..1217.

Reference (rtw88-source-v6.18):
    rtw88xxa.c:572   rtw88xxa_phy_bb_config        (8812a crystal_cap mask differs)
    rtw88xxa.c:602   rtw88xxa_phy_rf_config        (2T2R: load rf_a + rf_b)
    rtw88xxa.c:821   rtw8812a_phy_set_rfe_reg_24g  (case 0 = rfe-defaults)
    rtw88xxa.c:927   rtw88xxa_switch_band          (8812A else-branch)
    phy.c:1029       rtw_phy_write_rf_reg_sipi
    phy.c:1817       rtw_phy_cfg_bb (delay-magic addrs)

8812a is 2T2R (vs 8821a's 1T1R), so phy_rf_config loads BOTH the rf_a and
rf_b tables. The SIPI write target switches between REG_LSSI_WRITE_A and
REG_LSSI_WRITE_B based on which table is being walked.

Without EFUSE the runtime cond uses :class:`EfuseDefaults` (rfe_option=0,
no ext LNA/PA, no btcoex, crystal_cap=0, tx_bb_swing=0). M-LATER will
read EFUSE for accurate values.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from wifit3.chips.rtw88_base.phy_cond import (
    INTF_USB,
    RTW_CHIP_TYPE_8812A,
    DeviceCond,
    PhyCond2,
    parse_tbl_phy_cond,
)

from .assets import agc_tbl, bb_tbl, mac_tbl, rf_a_tbl, rf_b_tbl
from .constants import (
    BASIC_RATES_2G,
    BASIC_RATES_5G,
    BB_SWING_2G_DEFAULT,
    BB_SWING_MASK,
    BIT_CCK_RPT_FORMAT,
    BIT_CHECK_CCK_EN,
    BIT_FEN_BB_GLB_RST,
    BIT_FEN_BB_RSTB,
    BIT_FEN_USBA,
    BIT_RF_EN,
    BIT_RF_RSTB,
    BIT_RF_SDM_RSTB,
    BIT_RX_PSEL_RST,
    REG_ACLK_MON,
    REG_AFE_CTRL3,
    REG_ANTSEL_SW,
    REG_BAR_MODE_CTRL,
    REG_BWINDICATION,
    REG_CCASEL,
    REG_CCK_CHECK,
    REG_CCK_RPT_FORMAT,
    REG_CCK_RX,
    REG_EARLY_MODE_CONTROL,
    REG_FWHW_TXQ_CTRL,
    REG_GPIO_MUXCFG,
    REG_HWSEQ_CTRL,
    REG_LSSI_WRITE_A,
    REG_LSSI_WRITE_B,
    REG_NAV_CTRL,
    REG_PDMFTH,
    REG_QUEUE_CTRL,
    REG_RF_B_CTRL,
    REG_RF_CTRL,
    REG_RFE_INV_A,
    REG_RFE_INV_B,
    REG_RFE_PINMUX_A,
    REG_RFE_PINMUX_B,
    REG_RRSR,
    REG_RXPSEL,
    REG_SYS_FUNC_EN,
    REG_SYS_SDIO_CTRL,
    REG_TXPKT_EMPTY,
    REG_TXPSEL,
    REG_TXSCALE_A,
    REG_TXSCALE_B,
    REG_TX_RPT_TIME,
    REG_USB_HRPWM,
    RFE_INV_MASK,
    RFREG_MASK,
    RTW_SEC_CMD_REG,
)
from .transport import RTL8812AUTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EfuseDefaults:
    """Best-known stand-ins until EFUSE is actually read.

    rfe_option=0 lands in the case-0 branch of `rtw8812a_phy_set_rfe_reg_*`
    which is the "no special routing" path: REG_RFE_PINMUX_{A,B}=0x77777777,
    REG_RFE_INV_{A,B}=0. That's enough to RX beacons.
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
    rf_path_num: int = 2  # 8812A is 2T2R by default


def device_cond(efuse: EfuseDefaults) -> DeviceCond:
    """Mirror of `rtw_phy_setup_phy_cond` (phy.c:1103) for 8812A.

    Same bitfield-rfe encoding as 8821A.
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

def _cfg_mac(transport: RTL8812AUTransport, addr: int, data: int) -> None:
    transport.write8(addr, data & 0xFF)


def _cfg_bb(transport: RTL8812AUTransport, addr: int, data: int) -> None:
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


def _cfg_agc(transport: RTL8812AUTransport, addr: int, data: int) -> None:
    transport.write32(addr, data)


def _make_cfg_rf(transport: RTL8812AUTransport, sipi_reg: int):
    """Factory for path-specific RF SIPI writers.

    Mirrors rtw_phy_cfg_rf (phy.c:1837); the only path-specific piece is
    which LSSI_WRITE register receives the encoded addr+data word.
    """
    def _cfg(addr: int, data: int) -> None:
        if addr == 0xFFE:
            time.sleep(0.050)
        elif addr == 0xFE:
            time.sleep(100e-6)
        else:
            # data_and_addr = ((addr << 20) | (data & 0xFFFFF)) & 0x0FFFFFFF
            data_and_addr = (((addr & 0xFF) << 20) | (data & RFREG_MASK)) & 0x0FFFFFFF
            transport.write32(sipi_reg, data_and_addr)
            time.sleep(13e-6)
    return _cfg


# ---------------------------------------------------------------------------
# Table loaders.
# ---------------------------------------------------------------------------

def load_mac_table(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> int:
    n = parse_tbl_phy_cond(
        mac_tbl.TABLE, device_cond(efuse),
        lambda a, d: _cfg_mac(transport, a, d),
        chip_id=RTW_CHIP_TYPE_8812A,
    )
    logger.debug("mac_tbl: dispatched %d write8 ops", n)
    return n


def load_bb_table(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> int:
    n = parse_tbl_phy_cond(
        bb_tbl.TABLE, device_cond(efuse),
        lambda a, d: _cfg_bb(transport, a, d),
        chip_id=RTW_CHIP_TYPE_8812A,
    )
    logger.debug("bb_tbl: dispatched %d write32 ops", n)
    return n


def load_agc_table(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> int:
    n = parse_tbl_phy_cond(
        agc_tbl.TABLE, device_cond(efuse),
        lambda a, d: _cfg_agc(transport, a, d),
        chip_id=RTW_CHIP_TYPE_8812A,
    )
    logger.debug("agc_tbl: dispatched %d write32 ops", n)
    return n


def load_rf_a_table(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> int:
    cfg = _make_cfg_rf(transport, REG_LSSI_WRITE_A)
    n = parse_tbl_phy_cond(
        rf_a_tbl.TABLE, device_cond(efuse), cfg,
        chip_id=RTW_CHIP_TYPE_8812A,
    )
    logger.debug("rf_a_tbl: dispatched %d RF SIPI writes (path A)", n)
    return n


def load_rf_b_table(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> int:
    cfg = _make_cfg_rf(transport, REG_LSSI_WRITE_B)
    n = parse_tbl_phy_cond(
        rf_b_tbl.TABLE, device_cond(efuse), cfg,
        chip_id=RTW_CHIP_TYPE_8812A,
    )
    logger.debug("rf_b_tbl: dispatched %d RF SIPI writes (path B)", n)
    return n


# ---------------------------------------------------------------------------
# rtw88xxa_phy_bb_config / phy_rf_config (rtw88xxa.c:572, 602)
# ---------------------------------------------------------------------------

def phy_bb_config(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> None:
    """Port of rtw88xxa_phy_bb_config (rtw88xxa.c:572).

    8812A-specific: crystal_cap mask is `0x7FF80000` (vs 8821A's
    `0x00FFF000`).
    """
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
    # 8812A branch: mask = 0x7FF80000 (rtw88xxa.c:594-595)
    transport.write32_mask(
        REG_AFE_CTRL3, 0x7FF80000, crystal_cap | (crystal_cap << 6)
    )


def phy_rf_config(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> None:
    """Port of rtw88xxa_phy_rf_config (rtw88xxa.c:602).

    Loops over rf_path_num (=2 for 8812A 2T2R), loading rf_a then rf_b.
    The 1T1R branch (`rtw8812a_config_1t`) is NOT exercised — we always
    bring up both paths.
    """
    load_rf_a_table(transport, efuse)
    if efuse.rf_path_num >= 2:
        load_rf_b_table(transport, efuse)


# ---------------------------------------------------------------------------
# 2.4 GHz band switch (rtw88xxa.c:927 — 8812A else-branch)
# ---------------------------------------------------------------------------

def _phy_set_rfe_reg_24g_8812a(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> None:
    """Port of rtw8812a_phy_set_rfe_reg_24g (rtw88xxa.c:821..872).

    All 6 rfe_option cases. Critical for cards like AWUS036ACH which
    burn rfe_option=3 (IFEM-ext, PINMUX=0x54337770 with antenna switch)
    in EFUSE — using the wrong PINMUX leaves the chip's RF front-end
    routed for an internal-LNA-only card and kills 2.4 GHz sensitivity.
    """
    rfe = efuse.rfe_option
    if rfe in (0, 2):
        transport.write32(REG_RFE_PINMUX_A, 0x77777777)
        transport.write32(REG_RFE_PINMUX_B, 0x77777777)
        transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x000)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x000)
    elif rfe == 1:
        if efuse.btcoex:
            transport.write32_mask(REG_RFE_PINMUX_A, 0xFFFFFF, 0x777777)
            transport.write32(REG_RFE_PINMUX_B, 0x77777777)
            transport.write32_mask(REG_RFE_INV_A, 0x33F00000, 0x000)
            transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x000)
        else:
            transport.write32(REG_RFE_PINMUX_A, 0x77777777)
            transport.write32(REG_RFE_PINMUX_B, 0x77777777)
            transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x000)
            transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x000)
    elif rfe == 3:
        transport.write32(REG_RFE_PINMUX_A, 0x54337770)
        transport.write32(REG_RFE_PINMUX_B, 0x54337770)
        transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x010)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x010)
        transport.write32_mask(REG_ANTSEL_SW, 0x00000303, 0x1)
    elif rfe == 4:
        transport.write32(REG_RFE_PINMUX_A, 0x77777777)
        transport.write32(REG_RFE_PINMUX_B, 0x77777777)
        transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x001)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x001)
    elif rfe == 5:
        transport.write8(REG_RFE_PINMUX_A + 2, 0x77)
        transport.write32(REG_RFE_PINMUX_B, 0x77777777)
        transport.write8_clr(REG_RFE_INV_A + 3, 1 << 0)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x000)
    elif rfe == 6:
        transport.write32(REG_RFE_PINMUX_A, 0x07772770)
        transport.write32(REG_RFE_PINMUX_B, 0x07772770)
        transport.write32(REG_RFE_INV_A, 0x00000077)
        transport.write32(REG_RFE_INV_B, 0x00000077)
    else:
        logger.warning("rfe_option=%d not handled for 8812a 2G; falling "
                       "through to case-0 defaults", rfe)
        transport.write32(REG_RFE_PINMUX_A, 0x77777777)
        transport.write32(REG_RFE_PINMUX_B, 0x77777777)
        transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x000)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x000)


def _set_channel_bb_swing(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> None:
    """Mirrors rtw88xxa_set_channel_bb_swing (rtw88xxa.c:757).

    Writes TXSCALE on both paths (8812A is 2T2R; both real). With
    tx_bb_swing_2g=0 the swing reduces to BB_SWING_2G_DEFAULT (0x200).
    """
    swing = BB_SWING_2G_DEFAULT
    transport.write32_mask(REG_TXSCALE_A, BB_SWING_MASK, swing)
    transport.write32_mask(REG_TXSCALE_B, BB_SWING_MASK, swing)


def switch_band_2g_20mhz(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> None:
    """Port of rtw88xxa_switch_band(2G, 20MHz) for 8812A (rtw88xxa.c:927).

    Skips the 8821A-only `rtw8821a_set_ext_band_switch`. Adds the 8812A-
    specific BWINDICATION / PDMFTH / CCASEL pre-writes (lines 947..957).
    """
    transport.write32_set(REG_RXPSEL, BIT_RX_PSEL_RST)

    # 8812A else-branch (rtw88xxa.c:947..957)
    transport.write32_mask(REG_BWINDICATION, 0x3, 0x1)
    transport.write32_mask(REG_PDMFTH, 0x3E000, 0x17)  # GENMASK(17,13) = 0x3E000
    # rf_path_num=2 (we're 2T2R), so always take the `else` branch:
    transport.write32_mask(REG_PDMFTH, 0x0E, 0x04)     # GENMASK(3,1)  = 0x0E
    transport.write32_mask(REG_CCASEL, 0x3, 0)
    _phy_set_rfe_reg_24g_8812a(transport, efuse)

    transport.write32_mask(REG_TXPSEL, 0xF0, 0x1)
    transport.write32_mask(REG_CCK_RX, 0x0F000000, 0x1)
    transport.write32_mask(REG_RRSR, 0xFFFFF, BASIC_RATES_2G)
    transport.write8_clr(REG_CCK_CHECK, BIT_CHECK_CCK_EN)

    _set_channel_bb_swing(transport, efuse)


# ---------------------------------------------------------------------------
# 5 GHz band switch (rtw88xxa.c:972..1003 — 8812A else-branch)
# ---------------------------------------------------------------------------

def _phy_set_rfe_reg_5g_8812a(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> None:
    """Port of rtw8812a_phy_set_rfe_reg_5g (rtw88xxa.c:874..924).

    All 6 rfe_option cases. Common default is case 0 (PINMUX=0x77337717,
    INV-mask 0x3FF00000 = 0x010 on both paths).
    """
    rfe = efuse.rfe_option
    if rfe == 0:
        transport.write32(REG_RFE_PINMUX_A, 0x77337717)
        transport.write32(REG_RFE_PINMUX_B, 0x77337717)
        transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x010)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x010)
    elif rfe == 1:
        if efuse.btcoex:
            transport.write32_mask(REG_RFE_PINMUX_A, 0xFFFFFF, 0x337717)
            transport.write32(REG_RFE_PINMUX_B, 0x77337717)
            transport.write32_mask(REG_RFE_INV_A, 0x33F00000, 0x000)
            transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x000)
        else:
            transport.write32(REG_RFE_PINMUX_A, 0x77337717)
            transport.write32(REG_RFE_PINMUX_B, 0x77337717)
            transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x000)
            transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x000)
    elif rfe in (2, 4):
        transport.write32(REG_RFE_PINMUX_A, 0x77337777)
        transport.write32(REG_RFE_PINMUX_B, 0x77337777)
        transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x010)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x010)
    elif rfe == 3:
        transport.write32(REG_RFE_PINMUX_A, 0x54337717)
        transport.write32(REG_RFE_PINMUX_B, 0x54337717)
        transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x010)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x010)
        transport.write32_mask(REG_ANTSEL_SW, 0x00000303, 0x1)
    elif rfe == 5:
        transport.write8(REG_RFE_PINMUX_A + 2, 0x33)
        transport.write32(REG_RFE_PINMUX_B, 0x77337777)
        transport.write8_set(REG_RFE_INV_A + 3, 1 << 0)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x010)
    elif rfe == 6:
        transport.write32(REG_RFE_PINMUX_A, 0x07737717)
        transport.write32(REG_RFE_PINMUX_B, 0x07737717)
        transport.write32(REG_RFE_INV_A, 0x00000077)
        transport.write32(REG_RFE_INV_B, 0x00000077)
    else:
        logger.warning("rfe_option=%d not handled for 8812a 5G; falling through to case 0",
                       rfe)
        transport.write32(REG_RFE_PINMUX_A, 0x77337717)
        transport.write32(REG_RFE_PINMUX_B, 0x77337717)
        transport.write32_mask(REG_RFE_INV_A, RFE_INV_MASK, 0x010)
        transport.write32_mask(REG_RFE_INV_B, RFE_INV_MASK, 0x010)


def _poll_txpkt_empty(transport: RTL8812AUTransport,
                      max_attempts: int = 50, interval_s: float = 50e-6) -> None:
    """Wait for REG_TXPKT_EMPTY bits[5:4] == 0x3 (HI + MGT queues drained).

    Mirrors the read_poll_timeout_atomic in rtw88xxa.c:978 (50 attempts ×
    ~50us = 2.5ms budget). The kernel uses `(reg & 0x30) == 0x30`.
    """
    import time as _t
    for _ in range(max_attempts):
        if transport.read16(REG_TXPKT_EMPTY) & 0x30 == 0x30:
            return
        _t.sleep(interval_s)
    logger.warning("TXPKT_EMPTY poll timed out before band switch (continuing)")


def switch_band_5g_20mhz(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> None:
    """Port of rtw88xxa_switch_band(5G, 20MHz) for 8812A (rtw88xxa.c:972..1003).

    8812A 5G order differs from 2G:
      1. write CCK_CHECK BIT_CHECK_CCK_EN (5G doesn't use CCK; gates BB)
      2. poll TXPKT_EMPTY (wait for HI/MGT queues to drain)
      3. RXPSEL reset
      4. BWINDICATION = 2, PDMFTH 17:13 = 0x15, PDMFTH 3:1 = 0x04, CCASEL = 1
      5. rtw8812a_phy_set_rfe_reg_5g (different pinmux from 2G)
      6. TXPSEL bits[7:4] = 0, CCK_RX bits[27:24] = 0xF
      7. basic_rates = OFDM 6/12/24 only

    Skips the 8821A-only `rtw8821a_set_ext_band_switch`.
    """
    transport.write8_set(REG_CCK_CHECK, BIT_CHECK_CCK_EN)
    _poll_txpkt_empty(transport)

    transport.write32_set(REG_RXPSEL, BIT_RX_PSEL_RST)

    # 8812A else-branch (rtw88xxa.c:986..993)
    transport.write32_mask(REG_BWINDICATION, 0x3, 0x2)
    transport.write32_mask(REG_PDMFTH, 0x3E000, 0x15)
    transport.write32_mask(REG_PDMFTH, 0x0E, 0x04)
    transport.write32_mask(REG_CCASEL, 0x3, 1)
    _phy_set_rfe_reg_5g_8812a(transport, efuse)

    transport.write32_mask(REG_TXPSEL, 0xF0, 0)
    transport.write32_mask(REG_CCK_RX, 0x0F000000, 0xF)
    transport.write32_mask(REG_RRSR, 0xFFFFF, BASIC_RATES_5G)

    _set_channel_bb_swing(transport, efuse)


# ---------------------------------------------------------------------------
# Orchestrator (rtw88xxa.c:1083..1217 minus what M2-b already did)
# ---------------------------------------------------------------------------

def _log_queue_state(transport: RTL8812AUTransport, label: str) -> None:
    """Diagnostic: log REG_RQPN + REG_TXDMA_PQ_MAP for MX-a bisection.

    These two registers get silently cleared between post_fw_mac_init and
    TX attempts on 8812au, requiring the _arm_tx_queues workaround in
    driver.py. To find the offending step, call this after each major
    write in post_mac_init_phy.
    """
    from .constants import REG_RQPN, REG_TXDMA_PQ_MAP
    # DEBUG-level so this only fires when the user explicitly passes --debug
    # to the test harness. Two control-transfer reads per call; cheap but
    # noisy if it printed every cold boot.
    if not logger.isEnabledFor(logging.DEBUG):
        return
    rqpn = transport.read32(REG_RQPN)
    pq_map = transport.read16(REG_TXDMA_PQ_MAP)
    logger.debug("[Q-bisect] %-32s RQPN=0x%08x  PQ_MAP=0x%04x",
                 label, rqpn, pq_map)


def post_mac_init_phy(transport: RTL8812AUTransport, efuse: EfuseDefaults) -> None:
    """Run the BB/RF/band-switch chunk after M2-b's post_fw_mac_init.

    Covers:
      * line 1083 — mac_tbl load (deferred from M2-b)
      * lines 1177-1183 — phy_bb_config, phy_rf_config, switch_band(2G, 20MHz)
      * lines 1185-1191 — security/sequencing/BAR/NAV/GPIO inline pokes
      * lines 1200-1217 — queue/early-mode/USB inline pokes

    Skips (deferred to M-LATER):
      * rtw_phy_init (DIG calibration loop init)
      * rtw88xxa_pwrtrack_init (pure software state)
    """
    _log_queue_state(transport, "pre-post_mac_init_phy")

    # 1083 — mac_tbl
    load_mac_table(transport, efuse)
    _log_queue_state(transport, "post mac_tbl")

    # 1177-1178 — BB + RF tables
    phy_bb_config(transport, efuse)
    _log_queue_state(transport, "post phy_bb_config")

    phy_rf_config(transport, efuse)
    _log_queue_state(transport, "post phy_rf_config")

    # 1180-1181 — 8812a 1T config: skipped (we're 2T2R).

    # 1183 — switch to 2.4 GHz, 20 MHz
    switch_band_2g_20mhz(transport, efuse)
    _log_queue_state(transport, "post switch_band_2g_20mhz")

    # 1185-1191
    transport.write32(RTW_SEC_CMD_REG, (1 << 31) | (1 << 30))
    transport.write8(REG_HWSEQ_CTRL, 0xFF)
    transport.write32(REG_BAR_MODE_CTRL, 0x0201FFFF)
    transport.write8(REG_NAV_CTRL + 2, 0)

    cur = transport.read8(REG_GPIO_MUXCFG)
    transport.write8(REG_GPIO_MUXCFG, cur & ~(1 << 5) & 0xFF)

    # 1193 — rtw_phy_init: SKIPPED (M-LATER)
    # 1195 — pwrtrack_init: SKIPPED (M-LATER)

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
    _log_queue_state(transport, "post inline-pokes")

    # 1219 — read cck_high_power (informational, RX side uses it later).
    val = transport.read32(REG_CCK_RPT_FORMAT)
    logger.debug(
        "cck_high_power flag = %d (REG_CCK_RPT_FORMAT=0x%08x)",
        1 if val & BIT_CCK_RPT_FORMAT else 0, val,
    )

    # Port of `rtw_drv_info_cfg` (mac.c:1373..1389) — applied AFTER mac_tbl.
    # mac_tbl writes 0x608 = 0x0E and 0x609 = 0x2A which programs the low
    # 16 bits of REG_RCR. We then set BIT_APP_PHYSTS (bit 28) to enable
    # phy_status appending, and write REG_RX_DRVINFO_SZ to define the
    # size. Both must be set together.
    from .constants import (
        BIT_APP_PHYSTS as _BIT_APP_PHYSTS,
        PHY_STATUS_SIZE as _PHY_STATUS_SIZE,
        REG_RCR as _REG_RCR,
        REG_RX_DRVINFO_SZ as _REG_RX_DRVINFO_SZ,
    )
    transport.write32_set(_REG_RCR, _BIT_APP_PHYSTS)
    transport.write8(_REG_RX_DRVINFO_SZ, _PHY_STATUS_SIZE)
    logger.debug(
        "drv_info_cfg: REG_RCR |= APP_PHYSTS, REG_RX_DRVINFO_SZ=%d → readback=0x%08x / 0x%02x",
        _PHY_STATUS_SIZE,
        transport.read32(_REG_RCR),
        transport.read8(_REG_RX_DRVINFO_SZ),
    )
    _log_queue_state(transport, "post drv_info_cfg (end of post_mac_init_phy)")
