"""RTL8822BU BB (baseband) init — PHYDM phy_reg + AGC tables.

rtl8822b_phy_bb_config -> _init_bb_reg [SRC] rtl8822b_phy.c:92 applies the PHYDM BB phy-reg
table then the AGC table, each via odm_config_bb_phy_8822b (a 32-bit write per row; the
0xFA-0xFE rows are us/ms delays, absent from these tables on this card). A short RX/TX-path
pre-amble sets REG 0x808 byte0 before the phy-reg table.
"""
from __future__ import annotations

from .bb_phy_reg_tbl import BB_PHY_REG_TBL

REG_OFDM0_TRX_PATH = 0x0808           # byte0 = (rx_path<<4)|rx_path (RX/TX path enable)


def phy_bb_config(t, rx_path: int) -> None:
    """Apply the BB phy-reg table after the RX/TX-path pre-amble (2T2R => 0x808 byte0 = 0x33)."""
    v = t.read32(REG_OFDM0_TRX_PATH)
    t.write32(REG_OFDM0_TRX_PATH, (v & ~0xFF) | ((rx_path << 4) | rx_path))
    for addr, val in BB_PHY_REG_TBL:
        t.write32(addr, val)
