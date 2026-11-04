"""rt2800 VGC (BBP66) seed and write: rt2800_get_default_vgc (rt2800lib.c:5723) and rt2800_set_vgc (5759).

The periodic rt2x00link_tuner never runs for a monitor-only interface (rt2x00link.c:228).
"""
from __future__ import annotations

from .bbp import bbp_write, bbp_write_with_rx_chain
from .constants import RT_RT3572, RT_RT5592
from .transport import RT5572Transport


def get_default_vgc(
    silicon_id: int, channel: int, lna_gain: int, *, ht40: bool = False
) -> int:
    """Most-sensitive VGC seed for this band/silicon — rt2800_get_default_vgc.

    Every silicon this driver supports (RT3572 / RT5390 / RT5392 / RT5592)
    is in the kernel's ``0x1c + 2*lna_gain`` group for 2.4 GHz; the older
    ``0x2e + lna_gain`` group (RT2860/RT2872-class) is not reachable here.
    [SRC] rt2800lib.c:5723.
    """
    if channel <= 14:                       # 2.4 GHz
        return (0x1C + 2 * lna_gain) & 0xFF
    # 5 GHz
    if silicon_id == RT_RT5592:
        return (0x24 + 2 * lna_gain) & 0xFF
    base = 0x3A if ht40 else 0x32           # RT3572 (and other non-RT3593/5592)
    return (base + (lna_gain * 5) // 3) & 0xFF


def set_vgc(
    t: RT5572Transport, silicon_id: int, vgc: int, *, rx_chain_num: int, rssi: int
) -> None:
    """Write the VGC level to BBP66 per the silicon's path — rt2800_set_vgc.

    RT3572 fans the write across each RX chain; RT5592 additionally nudges
    BBP83 by RSSI; the RT539x parts write BBP66 directly. [SRC] rt2800lib.c:5759.
    """
    if silicon_id == RT_RT3572:
        bbp_write_with_rx_chain(t, 66, vgc, rx_chain_num=rx_chain_num)
    elif silicon_id == RT_RT5592:
        bbp_write(t, 83, 0x4A if rssi > -65 else 0x7A)
        bbp_write_with_rx_chain(t, 66, vgc, rx_chain_num=rx_chain_num)
    else:                                   # RT5390 / RT5392
        bbp_write(t, 66, vgc)
