"""RTL8188FTV DKMS thermal-meter trigger (M5h).

Ported from ``odm_TXPowerTrackingCheckCE`` first-call branch
(hal/phydm/phydm_powertracking_ce.c): when ``TM_Trigger`` is clear the
driver pokes ``RF_T_METER_NEW`` BIT17|BIT16 and returns; the full thermal
callback runs on the next watchdog tick.
"""
from __future__ import annotations

from . import rf


def BIT(n: int) -> int:
    return 1 << n


def thermal_trigger(t) -> None:
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x42, BIT(17) | BIT(16), 0x03)


def thermal_read(t) -> int:
    return rf.query_rf_reg(t, rf.RF_PATH_A, 0x42, 0xFC00)


def kfree_gain_offset(t, offset: int = 0) -> None:
    write_value = abs(offset) | (BIT(5) if offset > 0 else 0)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x55, 0x0FC000, write_value)
