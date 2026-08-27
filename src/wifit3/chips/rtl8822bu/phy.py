"""RTL8822BU PHY init — minimal port of `rtw8822b_phy_set_param`.

Skips the rfe_option-dependent and EFUSE-dependent code paths
(`config_trx_mode`, `phy_rfe_init`, `pwrtrack_init`, `phy_bf_init`,
`rtw_phy_init` DIG, crystal_cap) — those affect TX power tuning, BT coex,
and DIG gain, not raw beacon RX. The 2.4 GHz monitor-mode RX path needs
only the BB/RF enable + init table load.

References:
    rtw8822b.c:152..186   rtw8822b_phy_set_param
    rtw8822b_table.c       mac_tbl / bb_tbl / agc_tbl / rf_a_tbl / rf_b_tbl
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
    BIT_RX_PSEL_RST,
    BIT_WLRF1_BBRF_EN,
    REG_RF_CTRL,
    REG_RXPSEL,
    REG_SYS_FUNC_EN,
    REG_WLRF1,
)
from wifit3.chips.rtw88_base.rf_sipi import write_rf_masked

from .assets.agc_tbl import TABLE as AGC_TABLE
from .assets.bb_tbl import TABLE as BB_TABLE
from .assets.mac_tbl import TABLE as MAC_TABLE
from .assets.rf_a_tbl import TABLE as RF_A_TABLE
from .assets.rf_b_tbl import TABLE as RF_B_TABLE
from .transport import RTL8822BUTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EfuseDefaults:
    """Sensible defaults when we haven't actually read EFUSE.

    For 8822b: `rfe_option` is the index into rtw8822b_rfe_info[]. Valid
    entries are {2, 3, 5}; 0 is uninitialized and would crash the kernel
    flow. We default to 3 (IFEM with ext) which covers TP-Link / D-Link
    / Edimax / most retail dongles per rtw8822bu.c's id_table.
    """
    cut: int = 3              # T3U capture-1 was CUT_D = 3
    pkg: int = 15             # default per `rtw_phy_setup_phy_cond`
    intf: int = INTF_USB
    rfe_option: int = 3       # IFEM-ext path (T-class dongles)
    btcoex: bool = False
    crystal_cap: int = 0
    antenna_tx_paths: int = 0b11  # BB_PATH_AB = 2T2R
    antenna_rx_paths: int = 0b11


# do_cfg callbacks for each table type (mirrors rtw_phy_cfg_mac/bb/agc/rf
# in phy.c:1803..1849). The "sleep code" addresses (0xfe, 0xfd, ...) act
# as DELAY pseudo-ops within the table stream.
SLEEP_CODES_BB = {
    0xfe: 0.050,
    0xfd: 0.005,
    0xfc: 0.001,
    0xfb: 50e-6,
    0xfa: 5e-6,
    0xf9: 1e-6,
}
SLEEP_CODES_RF = {
    0xffe: 0.050,
    0xfe: 100e-6,
}


def _do_cfg_mac(transport: RTL8822BUTransport, addr: int, data: int) -> None:
    transport.write8(addr, data & 0xFF)


def _do_cfg_agc(transport: RTL8822BUTransport, addr: int, data: int) -> None:
    transport.write32(addr, data & 0xFFFFFFFF)


def _do_cfg_bb(transport: RTL8822BUTransport, addr: int, data: int) -> None:
    delay = SLEEP_CODES_BB.get(addr)
    if delay is not None:
        time.sleep(delay)
    else:
        transport.write32(addr, data & 0xFFFFFFFF)


def _do_cfg_rf(transport: RTL8822BUTransport, addr: int, data: int,
               *, path: str) -> None:
    delay = SLEEP_CODES_RF.get(addr)
    if delay is not None:
        time.sleep(delay)
    else:
        write_rf_masked(transport, addr, 0xFFFFF, data, path=path,
                        udelay_us=1.0)


def _device_cond_for(efuse: EfuseDefaults) -> DeviceCond:
    return DeviceCond(
        cut=efuse.cut,
        pkg=efuse.pkg,
        intf=efuse.intf,
        rfe=efuse.rfe_option,
        cond2=PhyCond2(),
    )


def load_init_tables(transport: RTL8822BUTransport, efuse: EfuseDefaults) -> None:
    """Run all four (or five) init tables through the phy_cond walker.

    Mirrors rtw_phy_load_tables (phy.c:1870..1888). For 8822b 2T2R both
    RF A and RF B tables are loaded.
    """
    dev = _device_cond_for(efuse)
    chip_id = RTW_CHIP_TYPE_OTHER  # 8822b uses scalar-rfe check_positive

    n = parse_tbl_phy_cond(
        MAC_TABLE, dev, lambda a, d: _do_cfg_mac(transport, a, d),
        chip_id=chip_id,
    )
    logger.debug("loaded MAC table: %d writes", n)

    n = parse_tbl_phy_cond(
        BB_TABLE, dev, lambda a, d: _do_cfg_bb(transport, a, d),
        chip_id=chip_id,
    )
    logger.debug("loaded BB  table: %d writes (incl delays)", n)

    n = parse_tbl_phy_cond(
        AGC_TABLE, dev, lambda a, d: _do_cfg_agc(transport, a, d),
        chip_id=chip_id,
    )
    logger.debug("loaded AGC table: %d writes", n)

    n = parse_tbl_phy_cond(
        RF_A_TABLE, dev,
        lambda a, d: _do_cfg_rf(transport, a, d, path="a"),
        chip_id=chip_id,
    )
    logger.debug("loaded RF_A table: %d writes (incl delays)", n)

    if efuse.antenna_tx_paths > 1 or efuse.antenna_rx_paths > 1:
        n = parse_tbl_phy_cond(
            RF_B_TABLE, dev,
            lambda a, d: _do_cfg_rf(transport, a, d, path="b"),
            chip_id=chip_id,
        )
        logger.debug("loaded RF_B table: %d writes (incl delays)", n)


def phy_set_param(transport: RTL8822BUTransport,
                  efuse: EfuseDefaults | None = None) -> None:
    """Port of rtw8822b_phy_set_param (rtw8822b.c:152..186), simplified.

    Skips: crystal_cap (needs EFUSE), config_trx_mode (rfe-dependent),
    rtw_phy_init (DIG), phy_rfe_init, pwrtrack_init, phy_bf_init.
    Keeps: BB/RF enable + table loads + RXPSEL_RST clear/set bracket.
    """
    if efuse is None:
        efuse = EfuseDefaults()

    # power on BB/RF domain (rtw8822b.c:159..163)
    transport.write8_set(REG_SYS_FUNC_EN,
                         (BIT_FEN_BB_RSTB | BIT_FEN_BB_GLB_RST) & 0xFF)
    transport.write8_set(REG_RF_CTRL,
                         (BIT_RF_EN | BIT_RF_RSTB | BIT_RF_SDM_RSTB) & 0xFF)
    transport.write32_set(REG_WLRF1, BIT_WLRF1_BBRF_EN)

    # pre init before tables (rtw8822b.c:166)
    transport.write32_clr(REG_RXPSEL, BIT_RX_PSEL_RST)

    load_init_tables(transport, efuse)

    # post init after tables (rtw8822b.c:175)
    transport.write32_set(REG_RXPSEL, BIT_RX_PSEL_RST)
