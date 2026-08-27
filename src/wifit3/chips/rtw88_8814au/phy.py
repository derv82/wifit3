"""RTL8814AU PHY init — table loaders + phy_cond do_cfg callbacks.

M3.a scope: the table-replay machinery (do_cfg callbacks for mac/agc/bb/rf and
the phy_cond walker glue) plus `load_mac_table`, which replays the
unconditional MAC table — the end-to-end smoke test for "extraction + walker +
register-write path all work". The MAC table has zero phy_cond conditionals,
so it loads regardless of cut/rfe.

The full `phy_set_param` (BB/RF domain enable + conditional BB/AGC/RF table
loads on all 4 paths + RX-PSEL bracket) lands in M3.b, where rfe_option
selection actually matters.

References:
    rtw8814a.c:rtw8814a_phy_set_param
    rtw8814a_table.c   mac / agc / bb / rf_a..rf_d tables
    phy.c:1803         rtw_phy_cfg_{mac,agc,bb,rf}
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from wifit3.chips.rtw88_base.phy_cond import (
    INTF_USB,
    RTW_CHIP_TYPE_OTHER,
    DeviceCond,
    PhyCond2,
    parse_tbl_phy_cond,
)

from wifit3.chips.rtw88_base.registers import (
    BIT_FEN_BB_GLB_RST,
    BIT_FEN_BB_RSTB,
    BIT_RF_EN,
    BIT_RF_RSTB,
    BIT_RF_SDM_RSTB,
    REG_RF_CTRL,
    REG_SYS_FUNC_EN,
)

from . import constants as C
from . import rf
from .assets.agc_tbl import TABLE as AGC_TABLE
from .assets.bb_tbl import TABLE as BB_TABLE
from .assets.mac_tbl import TABLE as MAC_TABLE
from .assets.rf_a_tbl import TABLE as RF_A_TABLE
from .assets.rf_b_tbl import TABLE as RF_B_TABLE
from .assets.rf_c_tbl import TABLE as RF_C_TABLE
from .assets.rf_d_tbl import TABLE as RF_D_TABLE
from .transport import RTL8814AUTransport

_RF_TABLES = (RF_A_TABLE, RF_B_TABLE, RF_C_TABLE, RF_D_TABLE)

logger = logging.getLogger(__name__)

# 8814a uses the scalar-rfe check_positive branch (NOT the 8812a/8821a bitfield).
_CHIP_ID = RTW_CHIP_TYPE_OTHER


@dataclass(frozen=True)
class EfuseDefaults:
    """Defaults used until EFUSE is read (M4).

    For 8814a the MAC table has no conditionals, so cut/rfe are irrelevant to
    M3.a. They start to matter for the conditional AGC/BB/RF tables in M3.b;
    `rfe_option` will be refined from EFUSE then.
    """
    cut: int = 15                 # overridden at runtime from REG_SYS_CFG1
    pkg: int = 15
    intf: int = INTF_USB
    rfe_option: int = 1           # placeholder until EFUSE read (M4)
    crystal_cap: int = 0x20       # xtal_k; EFUSE default when unset
    antenna_tx_paths: int = 0b1111  # 4T4R
    antenna_rx_paths: int = 0b1111


# Sleep pseudo-ops embedded in BB/RF table addresses (phy.c rtw_phy_cfg_*).
SLEEP_CODES_BB = {0xfe: 0.050, 0xfd: 0.005, 0xfc: 0.001,
                  0xfb: 50e-6, 0xfa: 5e-6, 0xf9: 1e-6}
SLEEP_CODES_RF = {0xffe: 0.050, 0xfe: 100e-6}


def _do_cfg_mac(transport: RTL8814AUTransport, addr: int, data: int) -> None:
    transport.write8(addr, data & 0xFF)


def _do_cfg_agc(transport: RTL8814AUTransport, addr: int, data: int) -> None:
    transport.write32(addr, data & 0xFFFFFFFF)


def _do_cfg_bb(transport: RTL8814AUTransport, addr: int, data: int) -> None:
    delay = SLEEP_CODES_BB.get(addr)
    if delay is not None:
        time.sleep(delay)
    else:
        transport.write32(addr, data & 0xFFFFFFFF)


def _do_cfg_rf(transport: RTL8814AUTransport, addr: int, data: int,
               *, path: int) -> None:
    delay = SLEEP_CODES_RF.get(addr)
    if delay is not None:
        time.sleep(delay)
    else:
        rf.write_rf(transport, path, addr, rf.RFREG_MASK, data, udelay_us=1.0)


def defaults_from_efuse(er, cut: int) -> EfuseDefaults:
    """Build PHY EfuseDefaults from a real EFUSE read (efuse.EfuseRead) + cut."""
    return EfuseDefaults(cut=cut, rfe_option=er.rfe_option,
                         crystal_cap=er.crystal_cap)


def device_cond_for(efuse: EfuseDefaults) -> DeviceCond:
    return DeviceCond(
        cut=efuse.cut,
        pkg=efuse.pkg,
        intf=efuse.intf,
        rfe=efuse.rfe_option,
        cond2=PhyCond2(),
    )


def load_mac_table(transport: RTL8814AUTransport,
                   efuse: EfuseDefaults | None = None) -> int:
    """Replay the MAC init table (unconditional). Returns the write count."""
    if efuse is None:
        efuse = EfuseDefaults()
    dev = device_cond_for(efuse)
    n = parse_tbl_phy_cond(
        MAC_TABLE, dev, lambda a, d: _do_cfg_mac(transport, a, d),
        chip_id=_CHIP_ID,
    )
    logger.debug("loaded MAC table: %d writes", n)
    return n


def load_init_tables(transport: RTL8814AUTransport, efuse: EfuseDefaults) -> None:
    """Load BB, AGC, then all four RF (A..D) tables through the walker.

    The conditional tables (bb/agc/rf) gate on cut + rfe_option. `cut` is the
    real silicon cut (read into the EfuseDefaults at runtime); `rfe_option` is
    still a placeholder until M4 — the RF read-back gate validates whether it
    brought RF up sanely.
    """
    dev = device_cond_for(efuse)

    n = parse_tbl_phy_cond(
        BB_TABLE, dev, lambda a, d: _do_cfg_bb(transport, a, d), chip_id=_CHIP_ID)
    logger.debug("loaded BB  table: %d writes (incl delays)", n)

    n = parse_tbl_phy_cond(
        AGC_TABLE, dev, lambda a, d: _do_cfg_agc(transport, a, d), chip_id=_CHIP_ID)
    logger.debug("loaded AGC table: %d writes", n)

    # crystal_cap (rtw8814a.c, between agc + rf loads): two 6-bit xtal_k fields
    # into REG_AFE_CTRL3. Trims the reference clock — omitting it shifts the
    # reference and makes RX demod lock intermittently.
    xcap = efuse.crystal_cap & 0x3F
    xcap |= xcap << 6
    transport.write32_mask(C.REG_AFE_CTRL3, C.AFE_CTRL3_XCAP_MASK, xcap)
    logger.debug("crystal_cap: xtal_k=0x%02x -> AFE_CTRL3 field 0x%03x",
                efuse.crystal_cap & 0x3F, xcap)

    for path, table in enumerate(_RF_TABLES):
        n = parse_tbl_phy_cond(
            table, dev,
            lambda a, d, p=path: _do_cfg_rf(transport, a, d, path=p),
            chip_id=_CHIP_ID)
        logger.debug("loaded RF_%s table: %d writes (incl delays)",
                    "ABCD"[path], n)


def phy_set_param(transport: RTL8814AUTransport,
                  efuse: EfuseDefaults | None = None) -> None:
    """Port of rtw8814a_phy_set_param (rtw8814a.c).

    Keeps: BB/RF domain enable (4 paths), MAC + BB + AGC + RF(A..D) table loads,
    crystal_cap trim, config_trx_path (CCK RX path B), the A->B/C/D RCK copy, and
    the RX-PSEL reset.

    Still deferred (TX/protocol, land with TX or never needed for monitor):
    pwrtrack_init, the HWSEQ/BAR/MISC/NAV/QUEUE/FWHW_TXQ mac regs, init_rfe_reg
    (done via the first `chan.set_channel(force_band=True)`). `rtw_phy_init`'s
    DIG seed is now handled by the DIG watchdog (`dynamic.dig_init`), started by
    the driver after RX comes online; the rest of rtw_phy_init is software-only
    bookkeeping with no hardware writes (verified against phy.c).
    """
    if efuse is None:
        efuse = EfuseDefaults()

    rf_on = BIT_RF_EN | BIT_RF_RSTB | BIT_RF_SDM_RSTB

    # power on BB/RF domain (USB) + all 4 RF paths
    transport.write8_set(REG_SYS_FUNC_EN, C.BIT_FEN_USBA)
    transport.write8_set(C.REG_SYS_CFG3_8814A + 2,
                         (BIT_FEN_BB_GLB_RST | BIT_FEN_BB_RSTB) & 0xFF)
    transport.write8(REG_RF_CTRL, rf_on & 0xFF)
    transport.write8(C.REG_RF_CTRL1, rf_on & 0xFF)
    transport.write8(C.REG_RF_CTRL2, rf_on & 0xFF)
    transport.write8(C.REG_RF_CTRL3, rf_on & 0xFF)

    load_mac_table(transport, efuse)
    load_init_tables(transport, efuse)

    # crystal_cap trim -> AFE_CTRL3 (rtw8814a.c:307). 6-bit value duplicated into
    # both crystal-cap fields ([5:0] | [5:0]<<6), masked into AFE_CTRL3[26:15].
    crystal_cap = efuse.crystal_cap & 0x3F
    crystal_cap |= crystal_cap << 6
    transport.write32_mask(C.REG_AFE_CTRL3, 0x07FF8000, crystal_cap)

    # config_trx_path (rtw8814a.c:311) — CCK RX on path B + disable 2R CCA.
    config_trx_path(transport)

    # RCK: copy path-A RF_RCK1_V1 to B/C/D (rtw8814a.c).
    rck = rf.read_rf(transport, 0, C.RF_RCK1_V1, rf.RFREG_MASK)
    for path in (1, 2, 3):
        rf.write_rf(transport, path, C.RF_RCK1_V1, rf.RFREG_MASK, rck)
    logger.debug("RCK path-A=0x%05x copied to paths B/C/D", rck)

    # RX-PSEL reset (post-table)
    transport.write32_set(C.REG_RXPSEL, C.BIT_RX_PSEL_RST)


def config_trx_path(transport: RTL8814AUTransport) -> None:
    """rtw8814a_config_trx_path (rtw8814a.c:311): CCK RX disable 2R CCA, then
    path-B TX on / path-B RX. The kernel runs this in phy_set_param before the
    RF tables; switch_band later re-asserts the path-B RX bit per band."""
    transport.write32_clr(C.REG_CCK0_FAREPORT, C.BIT_CCK0_2RX | C.BIT_CCK0_MRC)
    transport.write32_mask(C.REG_CCK_RX, 0xF0000000, 0x4)   # path-B TX on
    transport.write32_mask(C.REG_CCK_RX, 0x0F000000, 0x5)   # path-B RX
