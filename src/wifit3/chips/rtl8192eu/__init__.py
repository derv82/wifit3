"""RTL8192EU driver — TP-Link TL-WN823N v2/v3, TL-WN822N v4, etc.

Cleanroom port of the kernel `rtl8xxxu` driver's 8192e fileops vector
(`8192e.c:1721` `rtl8192eu_fops`), not to be confused with the unrelated
`rtw88` family or the 8192CU/8188EU silicon.

Every VID:PID whose `.driver_info` is `rtl8192eu_fops` in `core.c:7948-8222` (kernel v6.19).
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import DLink, Mercusys, Planex, Realtek, TPLink

_IDS = (
    (0x0BDA, 0x818B, "RTL8192EU", None, Realtek._818B),
    (0x2357, 0x0108, "RTL8192EU", None, TPLink.TL_WN822N_V4),
    (0x2001, 0x3319, "RTL8192EU", None, DLink.DWA_131_REV_E1),
    (0x2357, 0x0109, "RTL8192EU", None, TPLink.TL_WN823N_V2_V3),
    (0x2357, 0x0107, "RTL8192EU", None, TPLink._0107),
    (0x2019, 0xAB33, "RTL8192EU", None, Planex._AB33),
    (0x0BDA, 0x818C, "RTL8192EU", None, Realtek._818C),
    (0x2001, 0x3312, "RTL8192EU", None, DLink.DWA_131_REV_C1),
    (0x2357, 0x0126, "RTL8192EU", None, TPLink.TL_WN8200ND_V2),
    (0x2C4E, 0x0100, "RTL8192EU", None, Mercusys.MW300UM),
    (0x2C4E, 0x0104, "RTL8192EU", None, Mercusys.MW300UH),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8192EUDriver
    return RTL8192EUDriver
