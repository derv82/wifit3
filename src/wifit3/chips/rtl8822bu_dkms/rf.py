"""RTL8822BU RF (radio) init — PHYDM radioa/radiob tables.

rtl8822b_phy_init -> init_rf_reg -> _init_rf_reg `[SRC] rtl8822b_phy.c:185` configures RF path A
then path B from the PHYDM radio tables (odm_config_rf_with_header_file(CONFIG_RF_RADIO, path)).
Both tables carry cut/rfe conditionals, so they run through phy_cond.walk; each in-branch row is a
single masked RF write.

[SRC] config_phydm_write_rf_reg_8822b (phydm_hal_api8822b.c:993): an RF write with the full
RFREGOFFSETMASK skips the read-back and is a plain W32 to 0xC90 (path A) / 0xE90 (path B) of
((addr & 0xFF) << 20 | data[19:0]) & 0x0FFFFFFF. addr 0xFE/0xFFE rows are us/ms delays
(odm_config_rf_reg_8822b) with no register write.
"""
from __future__ import annotations

from . import phy_cond
from .rf_radioa_tbl import RADIOA_TBL
from .rf_radiob_tbl import RADIOB_TBL

REG_RF_WRITE = {0: 0x0C90, 1: 0x0E90}   # offset_write_rf[path] — path A / path B
RF_DELAY_ADDRS = (0xFE, 0xFFE)          # ODM_delay_us(100) / ODM_delay_ms(50) — no write


def _rf_apply(t, port: int, addr: int, data: int) -> None:
    if addr in RF_DELAY_ADDRS:
        return                          # delay row — replay settles instantly, no register write
    t.write32(port, ((addr & 0xFF) << 20 | (data & 0x000FFFFF)) & 0x0FFFFFFF)


def phy_rf_config(t, cfg: phy_cond.PhyCondConfig) -> None:
    """Apply the path-A then path-B RF radio tables (cut/rfe-resolved by phy_cond.walk)."""
    phy_cond.walk(RADIOA_TBL, cfg, lambda addr, data: _rf_apply(t, REG_RF_WRITE[0], addr, data))
    phy_cond.walk(RADIOB_TBL, cfg, lambda addr, data: _rf_apply(t, REG_RF_WRITE[1], addr, data))
