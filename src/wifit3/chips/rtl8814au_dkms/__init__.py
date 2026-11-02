"""RTL8814AU vendor (morrownr DKMS) cleanroom port. See RTL8814AU_DKMS.md.

VID:PIDs are the vendor table (``os_dep/linux/usb_intf.c:237`` under ``CONFIG_RTL8814A``),
which is the same 14 the mainline sibling ``chips/rtw88_8814au`` claims.
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import ALFA, ASUS, DLink, Edimax, Elecom, Hawking, Netgear, TPLink, TRENDnet

_IDS = (
    (0x0BDA, 0x8813, "RTL8814AU", None, ALFA.AWUS1900),
    (0x056E, 0x400B, "RTL8814AU", None, Elecom.WDC_1300SU2),
    (0x056E, 0x400D, "RTL8814AU", None, Elecom.WDC_1300SU3),
    (0x0846, 0x9054, "RTL8814AU", None, Netgear.A7000),
    (0x0B05, 0x1817, "RTL8814AU", None, ASUS.USB_AC68),
    (0x0B05, 0x1852, "RTL8814AU", None, ASUS.USB_AC68),
    (0x0B05, 0x1853, "RTL8814AU", None, ASUS.USB_AC68),
    (0x0E66, 0x0026, "RTL8814AU", None, Hawking.HW17ACU),
    (0x2001, 0x331A, "RTL8814AU", None, DLink.DWA_192),
    (0x20F4, 0x809A, "RTL8814AU", None, TRENDnet.TEW_809UB),
    (0x20F4, 0x809B, "RTL8814AU", None, TRENDnet.TEW_809UB),
    (0x2357, 0x0106, "RTL8814AU", None, TPLink.ARCHER_T9UH),
    (0x7392, 0xA834, "RTL8814AU", None, Edimax.EW_7833UAC),
    (0x7392, 0xA833, "RTL8814AU", None, Edimax.EW_7833UAC),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import Rtl8814auDkmsDriver
    return Rtl8814auDkmsDriver
