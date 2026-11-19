"""RTL8192EU mainline rtl8xxxu port. See RTL8192EU.md.

VID:PIDs from rtl8xxxu core.c's device table (``.driver_info = &rtl8192eu_fops``).
Ported from v6.6 source; see RTL8192EU.md for the hardware-verification status.
"""
from wifit3.chips.products import DLink, TPLink
from wifit3.models.device_id import DeviceID

_IDS = (
    (0x0BDA, 0x818B, "RTL8192EU", None, None),
    (0x2357, 0x0108, "RTL8192EU", "TP-Link", TPLink.TL_WN822N_V4),
    (0x2001, 0x3319, "RTL8192EU", "D-Link", DLink.DWA_131),
    (0x2357, 0x0109, "RTL8192EU", None, None),
    # found in the rtl8192eu vendor driver (CONFIG_RTL8XXXU_UNTESTED):
    (0x2357, 0x0107, "RTL8192EU", None, None),
    (0x2019, 0xAB33, "RTL8192EU", None, None),
    (0x0BDA, 0x818C, "RTL8192EU", None, None),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8192EUDriver
    return RTL8192EUDriver
