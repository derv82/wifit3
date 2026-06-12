"""RTL8187 RFKILL GPIO poll.

Port of ``rtl8187_is_radio_enabled`` (``rtl8187/rfkill.c``). The kernel registers this as
a polling rfkill at probe (``rtl8187_rfkill_init``) and the rfkill core then re-runs it
every ~1.3-1.8 s (``rtl8187_rfkill_poll``). On the wire it is a fixed 3-op signature —
read GPIO0, clear the rfkill bit on GPIO0, read GPIO1 — so the acceptance gate dispatches
it as a periodic async producer interleaved with the bring-up + channel hops.

[SRC] ``data_dumps/rtl818x-source-v6.18/rtl8187/rfkill.c`` + ``rtl8187.h`` (the mask).
"""
from __future__ import annotations

from .constants import REG_GPIO0, REG_GPIO1
from .transport import RTL8187Transport

# [SRC] rtl8187.h:34 — RFKILL_MASK_8187_89_97 (the 0x8187/0x8189/0x8197 mask). The
# 0x8198 variant (mask 0x4) is selected from EEPROM only for product_id 0x8197/0x8198,
# which the AWUS036H (0x8187) is not.
RFKILL_MASK_8187 = 0x2


def is_radio_enabled(t: RTL8187Transport, mask: int = RFKILL_MASK_8187) -> bool:
    """True iff the hardware RF switch is on. [SRC] rtl8187_is_radio_enabled."""
    gpio = t.read8(REG_GPIO0)
    t.write8(REG_GPIO0, gpio & ~mask & 0xFF)
    gpio = t.read8(REG_GPIO1)
    return bool(gpio & mask)
