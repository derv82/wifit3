"""RTL8822BU BB (baseband) init — PHYDM phy_reg + AGC tables.

rtl8822b_phy_bb_config -> _init_bb_reg [SRC] rtl8822b_phy.c:92 applies the PHYDM BB phy-reg
table then the AGC table, each via odm_config_bb_phy_8822b / odm_config_bb_agc_8822b (a 32-bit
write per row; the 0xFA-0xFE rows are us/ms delays, absent from these tables on this card). A
short RX/TX-path pre-amble sets REG 0x808 byte0 before the phy-reg table. The AGC table carries
cut/rfe conditionals, so it runs through phy_cond.walk rather than a flat loop.
"""
from __future__ import annotations

from . import phy_cond
from .bb_agc_tbl import AGC_TAB
from .bb_phy_reg_tbl import BB_PHY_REG_TBL

REG_OFDM0_TRX_PATH = 0x0808           # byte0 = (rx_path<<4)|rx_path (RX/TX path enable)


def phy_bb_config(t, rx_path: int) -> None:
    """Apply the BB phy-reg table after the RX/TX-path pre-amble (2T2R => 0x808 byte0 = 0x33)."""
    v = t.read32(REG_OFDM0_TRX_PATH)
    t.write32(REG_OFDM0_TRX_PATH, (v & ~0xFF) | ((rx_path << 4) | rx_path))
    for addr, val in BB_PHY_REG_TBL:
        t.write32(addr, val)


def phy_agc_config(t, cfg: phy_cond.PhyCondConfig) -> None:
    """Apply the BB AGC table; phy_cond.walk selects the rows for this cut/rfe (W32 each).

    odm_config_bb_agc_8822b also feeds each 0x81C row to odm_update_agc_big_jump_lmt (software
    DIG state, no register write), so the wire is a plain run of W32s.
    """
    phy_cond.walk(AGC_TAB, cfg, lambda addr, val: t.write32(addr, val))
