"""RTL8188EUS DKMS (vendor) port — scaffold.

Sibling vendor port of ``chips/rtl8188eus`` (mainline). Cleanroom port of the
``realtek-rtl8188eus`` 5.3.9 DKMS driver (phydm/ODM RX stack) for hotter, more
stable 2.4 GHz monitor RX. See ``RTL8188EUS_DKMS.md`` for the A/B justification,
coordinates, and per-milestone status.

In progress (bring-up): M1 (power-on + firmware upload + FW-ready) is ported and
pcap-verified. MAC/BB/RF/efuse/calibration/RX/TX milestones follow.

VID:PID set kept in lockstep with the mainline sibling ``chips/rtl8188eus`` (all one silicon).
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
    from .driver import Rtl8188eusDkmsDriver
    return Rtl8188eusDkmsDriver
