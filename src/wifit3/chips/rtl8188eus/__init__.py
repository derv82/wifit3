"""RTL8188EUS driver — TP-Link TL-WN722N v2/v3, etc.

Cleanroom port of the kernel `rtl8xxxu` driver (specifically the 8188e
fileops vector), not to be confused with the unrelated `rtw88` family.

Full RTL8188EUS VID:PID set from the vendor 8188eus table (``.driver_info = RTL8188E``);
kept in lockstep with the DKMS sibling ``chips/rtl8188eus_dkms``. All one silicon.
"""
from wifit3.models.device_id import DeviceID

_IDS = (
    (0x2357, 0x010C, "RTL8188EUS", None, "TP-Link TL-WN722N v2/v3"),
    (0x0BDA, 0x8179, "RTL8188EUS", None, None),
    (0x0BDA, 0x0179, "RTL8188EUS", None, None),
    (0x07B8, 0x8179, "RTL8188EUS", None, "TP-Link"),
    (0x0DF6, 0x0076, "RTL8188EUS", None, "Sitecom N150 v2"),
    (0x2001, 0x330F, "RTL8188EUS", None, "D-Link DWA-125 REV D1"),
    (0x2001, 0x3310, "RTL8188EUS", None, "D-Link DWA-123 REV D1"),
    (0x2001, 0x3311, "RTL8188EUS", None, "D-Link GO-USB-N150 REV B1"),
    (0x2001, 0x331B, "RTL8188EUS", None, "D-Link DWA-121 REV B1"),
    (0x056E, 0x4008, "RTL8188EUS", None, "Elecom WDC-150SU2M"),
    (0x7392, 0xB811, "RTL8188EUS", None, "Edimax EW-7811UN v2"),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8188EUSDriver
    return RTL8188EUSDriver
