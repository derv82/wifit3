"""RTL8812AU M3 part 2: RF config — both radios (RADIO_A path A + RADIO_B path B).

Ports ``phy_RF6052_Config_ParaFile_8812`` (rtl8812a_rf6052.c:68): the 2T2R 8812 has
``rf_reg_path_num == 2``, so the loop programs path A from array_mp_8812a_radioa and
path B from array_mp_8812a_radiob, each a full-mask SIPI write per taken row (delay
pseudo-addrs 0xfe..0xf9 emit no write). This is the new 2T2R surface the 1T1R 8821
never exercised — path B is a real radio here, driven through the same proven base
SIPI primitive with the path-B 3-wire/readback addresses. The TX-power-tracking table
load that follows in the vendor is a driver-struct seed for the pwtrack watchdog (not
run) and is skipped, as on the 8821.
"""
from __future__ import annotations

from ..rtl88xxau_base.phy_cond import JaguarParams, apply_table
from ..rtl88xxau_base.sipi import RF_PATH_A, RF_PATH_B, RFREG_WRITE_MASK, is_rf_delay_addr, set_rf_reg
from .rf_radioa_tbl import RF_RADIOA
from .rf_radiob_tbl import RF_RADIOB


def _radio_write(t, path: int, addr: int, data: int) -> None:
    if is_rf_delay_addr(addr):
        return                                              # delay pseudo-addr — no write
    set_rf_reg(t, path, addr, RFREG_WRITE_MASK, data)


def phy_rf_config(t, params: JaguarParams | None = None) -> None:
    """M3 part 2: RADIO_A (path A) then RADIO_B (path B), both via SIPI."""
    # phy_RFSerialRead brackets each RF SIPI read with a rCCAonSec (0x838[3]) toggle ONLY on
    # non-C-cut 8812a: `Offset != 0 && !(IS_VENDOR_8812A_C_CUT || 8821)` [SRC]
    # rtl8812a_phycfg.c:107/132. The AWUS036ACH is C-cut -- REG_SYS_CFG[15:12]=1 and 8812
    # adds +1 -> CUTVersion=2=C_CUT -- so the toggle is skipped (morrownr's pcap shows the
    # RF reads go straight to the 0xC00 PI-mode probe, no 0x838 writes).
    t._rf_read_cca_off = False
    p = params or JaguarParams()
    apply_table(RF_RADIOA, lambda a, d: _radio_write(t, RF_PATH_A, a, d), p)
    apply_table(RF_RADIOB, lambda a, d: _radio_write(t, RF_PATH_B, a, d), p)
