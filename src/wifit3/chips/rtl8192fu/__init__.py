"""RTL8192FU mainline rtl8xxxu port. See RTL8192FU.md.

VID:PID set from rtl8xxxu core.c's device table (``.driver_info = &rtl8192fu_fops``).
"""
from wifit3.chips.products import ASUS, Comfast, DLink, Edimax, TPLink
from wifit3.models.device_id import DeviceID

_IDS = (
    (0x0BDA, 0xF192, "RTL8192FU", None, Comfast.CF_826F),
    (0x0B05, 0x18F1, "RTL8192FU", None, ASUS.USB_N13_C1),
    (0x7392, 0xB722, "RTL8192FU", None, Edimax.EW_7722UTN_V3),
    (0x0BDA, 0x318B, "RTL8192FU", None, None),
    (0x2357, 0x0135, "RTL8192FU", None, TPLink.TL_WN823N_V2),
    (0x2001, 0x3328, "RTL8192FU", "D-Link", DLink.AN3U_REV_A1),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8192FUDriver
    return RTL8192FUDriver
