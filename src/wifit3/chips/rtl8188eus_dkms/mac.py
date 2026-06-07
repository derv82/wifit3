"""RTL8188EUS MAC register configuration (M2a).

``PHY_MACConfig8188E`` [SRC] rtl8188e_phycfg.c:758 loads the MAC register table
through the phydm walker (``odm_config_mac_8188e`` = 8-bit writes), then sets the
AMPDU aggregation number. [WIRE] cap1 ops 797..end-of-table.
"""
from __future__ import annotations

from . import phy_cond
from .constants import REG_MAX_AGGR_NUM
from .mac_reg_tbl import MAC_REG

MAX_AGGR_NUM = 0x07  # [SRC] include/Hal8188EPhyCfg.h (USB build; 0x0B is PCI-only)


def phy_mac_config(t) -> None:
    """Apply ``array_mp_8188e_mac_reg`` (each taken row is an 8-bit write), then the
    AMPDU aggregation number to REG_MAX_AGGR_NUM."""
    phy_cond.walk_table(MAC_REG, lambda addr, val: t.write8(addr, val & 0xFF))
    val = (MAX_AGGR_NUM << 8) | MAX_AGGR_NUM
    t.write16(REG_MAX_AGGR_NUM, val)
