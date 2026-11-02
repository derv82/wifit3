"""RTL8187 RFKILL GPIO poll.

Port of ``rtl8187_is_radio_enabled`` (``rtl8187/rfkill.c``). The kernel registers this as
a polling rfkill at probe (``rtl8187_rfkill_init``) and the rfkill core then re-runs it
every ~1.3-1.8 s (``rtl8187_rfkill_poll``). On the wire it is a fixed 3-op signature —
read GPIO0, clear the rfkill bit on GPIO0, read GPIO1 — so the acceptance gate dispatches
it as a periodic async producer interleaved with the bring-up + channel hops.

[SRC] ``driver_sources/rtl818x-source-v6.18/rtl8187/rfkill.c`` + ``rtl8187.h`` (the mask).
"""
from __future__ import annotations

from .constants import REG_GPIO0, REG_GPIO1
from .transport import RTL8187Transport


def is_radio_enabled(t: RTL8187Transport, mask: int) -> bool:
    """True iff the hardware RF switch is on; ``mask`` is priv->rfkill_mask, picked in probe."""
    gpio = t.read8(REG_GPIO0)
    t.write8(REG_GPIO0, gpio & ~mask & 0xFF)
    gpio = t.read8(REG_GPIO1)
    return bool(gpio & mask)
