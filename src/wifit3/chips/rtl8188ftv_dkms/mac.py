"""RTL8188FTV DKMS antenna selection + MAC config (M5a).

``init_antenna_selection`` ported from ``PHY_InitAntennaSelection8188F``
(hal/rtl8188f/usb/usb_halinit.c:799-813); ``mac_config`` walks the extracted
MAC table (halhwimg8188f_mac.c ``Array_MP_8188F_MAC_REG`` via
``ODM_ReadAndConfig_MP_8188F_MAC_REG``) emitting ``write8`` per taken row
(``odm_ConfigMAC_8188F`` → ``rtw_write8``).
"""
from __future__ import annotations

from . import bb, mac_reg_tbl, phy_cond


def BIT(n: int) -> int:
    return 1 << n


def init_antenna_selection(t) -> None:
    bb.set_mac_reg(t, 0x64, BIT(20), 0x0)
    bb.set_mac_reg(t, 0x64, BIT(24), 0x0)
    bb.set_mac_reg(t, 0x40, BIT(4), 0x0)
    bb.set_mac_reg(t, 0x40, BIT(3), 0x1)
    bb.set_mac_reg(t, 0x4C, BIT(24), 0x1)
    bb.set_mac_reg(t, 0x4C, BIT(23), 0x0)
    bb.set_bb_reg(t, 0x944, BIT(1) | BIT(0), 0x3)
    bb.set_bb_reg(t, 0x930, 0xFF, 0x77)
    bb.set_mac_reg(t, 0x38, BIT(11), 0x1)


def mac_config(t) -> None:
    phy_cond.walk_table(mac_reg_tbl.TABLE, lambda addr, value: t.write8(addr, value))
