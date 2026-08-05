"""RTL8822BU vendor/DKMS driver package: its VID:PIDs, readable without importing driver.py.

VID:PID set kept in lockstep with the mainline sibling ``chips/rtl8822bu`` (all one silicon).
"""
from wifit3.models.device_id import DeviceID

_IDS = (
    (0x2357, 0x0138, "RTL8822BU", "TP-Link", "Archer T3U Plus"),
    (0x2357, 0x012D, "RTL8822BU", "TP-Link", "Archer T3U"),
    (0x2357, 0x0115, "RTL8822BU", "TP-Link", "Archer T4U V3"),
    (0x2357, 0x012E, "RTL8822BU", "TP-Link", None),
    (0x2357, 0x0116, "RTL8822BU", "TP-Link", None),
    (0x2357, 0x0117, "RTL8822BU", "TP-Link", None),
    (0x0BDA, 0xB812, "RTL8822BU", None, None),
    (0x0BDA, 0xB82C, "RTL8822BU", None, None),
    (0x0BDA, 0xB81A, "RTL8822BU", None, None),
    (0x0B05, 0x1841, "RTL8822BU", "ASUS", "USB-AC55 B1"),
    (0x0B05, 0x184C, "RTL8822BU", "ASUS", None),
    (0x0B05, 0x19AA, "RTL8822BU", "ASUS", "USB-AC58 rev A1"),
    (0x2001, 0x331E, "RTL8822BU", "D-Link", "DWA-181"),
    (0x2001, 0x331C, "RTL8822BU", "D-Link", "DWA-182 D1"),
    (0x13B1, 0x0043, "RTL8822BU", "Linksys", "WUSB6400M"),
    (0x13B1, 0x0045, "RTL8822BU", "Linksys", "WUSB6300 v2"),
    (0x0846, 0x9055, "RTL8822BU", "Netgear", "A6150"),
    (0x7392, 0xB822, "RTL8822BU", "Edimax", "EW-7822ULC"),
    (0x7392, 0xC822, "RTL8822BU", "Edimax", "EW-7822UTC"),
    (0x7392, 0xD822, "RTL8822BU", "Edimax", None),
    (0x7392, 0xE822, "RTL8822BU", "Edimax", None),
    (0x7392, 0xF822, "RTL8822BU", "Edimax", "EW-7822UAD"),
    (0x2C4E, 0x0107, "RTL8822BU", "Mercusys", "MA30H"),
    (0x2C4E, 0x010A, "RTL8822BU", "Mercusys", "MA30N"),
    (0x0411, 0x03D1, "RTL8822BU", "Buffalo", "WI-U2-866DM"),
    (0x0411, 0x03D0, "RTL8822BU", "Buffalo", "WI-U3-866DHP"),
    (0x04CA, 0x8602, "RTL8822BU", "LiteOn", None),
    (0x056E, 0x4011, "RTL8822BU", "Elecom", None),
    (0x0B05, 0x1870, "RTL8822BU", "ASUS", None),
    (0x0B05, 0x1874, "RTL8822BU", "ASUS", None),
    (0x0BDA, 0x2102, "RTL8822BU", None, None),
    (0x0E66, 0x0025, "RTL8822BU", "Hawking", "HW12ACU"),
    (0x2001, 0x331F, "RTL8822BU", "D-Link", "DWA-183 D"),
    (0x2001, 0x3322, "RTL8822BU", "D-Link", "DWA-T185 rev A1"),
    (0x20F4, 0x805A, "RTL8822BU", "TRENDnet", "TEW-805UBH"),
    (0x20F4, 0x808A, "RTL8822BU", "TRENDnet", "TEW-808UBM"),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import Rtl8822buDkmsDriver
    return Rtl8822buDkmsDriver
