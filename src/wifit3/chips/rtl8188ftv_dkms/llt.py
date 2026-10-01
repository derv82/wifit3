"""RTL8188FTV DKMS LLT + TX-report prologue (M4, hal_init order).

Ported from ``rtl8188f_InitLLTTable`` (hal/rtl8188f/rtl8188f_hal_init.c:3496+)
and the MISC01 TX-report block (hal/rtl8188f/usb/usb_halinit.c:1179-1190).
``bRDGEnable`` is never assigned in the source (zero-initialized FALSE), so
``_InitRDGSetting`` is not on the graph; the ``CONFIG_TX_EARLY_MODE`` block
compiles out (include/autoconf.h:292).
"""
from __future__ import annotations

import time

from . import constants as C


def init_llt(t) -> bool:
    value32 = t.read32(C.REG_AUTO_LLT)
    t.write32(C.REG_AUTO_LLT, value32 | C.BIT_AUTO_INIT_LLT)
    start = time.monotonic()
    while True:
        value32 = t.read32(C.REG_AUTO_LLT)
        if not value32 & C.BIT_AUTO_INIT_LLT:
            return True
        if (time.monotonic() - start) * 1000 > 1000:
            return False
        time.sleep(2e-6)


def enable_tx_report(t) -> None:
    value8 = t.read8(C.REG_TX_RPT_CTRL)
    t.write8(C.REG_TX_RPT_CTRL, value8 | 0x02)
    t.write8(C.REG_TX_RPT_CTRL + 1, 2)
    t.write16(C.REG_TX_RPT_TIME, 0xCDF0)
