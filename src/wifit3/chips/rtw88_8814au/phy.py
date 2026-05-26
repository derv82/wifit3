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

from . import rf
from .assets.mac_tbl import TABLE as MAC_TABLE
from .transport import RTL8814AUTransport

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
    rfe_option: int = 1           # rtw8814a_rfe_defs default entry (placeholder)
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
    logger.info("loaded MAC table: %d writes", n)
    return n
