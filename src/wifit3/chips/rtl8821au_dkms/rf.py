"""RTL8821AU (DKMS) M3 part 2: PHY_RFConfig8812 — RadioA register table.

Ported from PHY_RF6052_Config_8812 (8821a, path A only — rf_reg_path_num=1, RadioB
is #if 0). Each taken row is an RF SIPI write: the 20-bit data is written to
rA_LSSIWrite_Jaguar (0xC90) as (addr & 0xFF) << 20 | data & 0xFFFFF. Delay
pseudo-addrs (0xffe/0xfe/0xfd/0xfc/0xfb/0xfa/0xf9) emit no write.
[SRC] phydm_regconfig8821a.c:21-57, rtl8812a_phycfg.c:141-172.

# TODO(8812au): 8812 has rf_reg_path_num=2 -> a RadioB table written to 0xE90.
"""
from __future__ import annotations

from .phy_cond import JaguarParams, apply_table
from .rf_radioa_tbl import RF_RADIOA

REG_LSSI_WRITE_A = 0x0C90                        # rA_LSSIWrite_Jaguar
_RF_DELAY_ADDRS = {0xFE, 0xFFE, 0xFD, 0xFC, 0xFB, 0xFA, 0xF9}


def _rf_write(t, addr, data):
    if addr in _RF_DELAY_ADDRS:
        return                                   # delay pseudo-addr — no register write
    t.write32(REG_LSSI_WRITE_A, ((addr & 0xFF) << 20) | (data & 0xFFFFF))


def phy_rf_config(t, params: JaguarParams | None = None) -> None:
    params = params or JaguarParams()
    apply_table(RF_RADIOA, lambda a, d: _rf_write(t, a, d), params)
