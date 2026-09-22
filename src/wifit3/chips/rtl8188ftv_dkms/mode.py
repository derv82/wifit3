"""RTL8188FTV DKMS opmode switch (monitor entry).

Ported from ``HW_VAR_SET_OPMODE`` monitor branch
(hal/rtl8188f/rtl8188f_hal_init.c: ``hw_var_set_opmode`` +
``hw_var_set_monitor`` via ``setopmode_hdl``): ``Set_MSR(NOLINK)`` keeps
the upper MSR bits, then RCR goes all-accept + FCS append and RXFLTMAP2
opens all data frames.
"""
from __future__ import annotations


def BIT(n: int) -> int:
    return 1 << n


_RCR_MONITOR = (BIT(0) | BIT(1) | BIT(2) | BIT(3) | BIT(5) | BIT(11)
                | BIT(12) | BIT(13) | BIT(28) | BIT(31))


def enter_monitor(t) -> None:
    msr = t.read8(0x102) & 0x0C
    t.write8(0x102, msr | 0x00)
    t.write32(0x608, _RCR_MONITOR)
    t.write16(0x6A4, 0xFFFF)
