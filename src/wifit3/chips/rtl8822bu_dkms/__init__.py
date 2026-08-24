"""RTL8822BU vendor/DKMS driver package: its VID:PIDs, readable without importing driver.py.

VID:PID set kept in lockstep with the mainline sibling ``chips/rtl8822bu`` (all one silicon).
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import ASUS, Buffalo, CCandC, DLink, Edimax, Elecom, Hawking, Linksys, LiteOn, Mercusys, Netgear, TPLink, TRENDnet

_IDS = (
    (0x2357, 0x0138, "RTL8822BU", None, TPLink.ARCHER_T3U_PLUS),
    (0x2357, 0x012D, "RTL8822BU", None, TPLink.ARCHER_T3U),
    # 2357:0115 could be one of: T4U Plus, T4U v3, T4U v3.2 https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U_v3
    (0x2357, 0x0115, "RTL8822BU", None, TPLink.ARCHER_T4U_V3),  # Chosen by dice roll.
    (0x2357, 0x012E, "RTL8822BU", None, TPLink.ARCHER_T3U_NANO),
    (0x2357, 0x0116, "RTL8822BU", None, None),  # (TP-Link) Wireless USB Adapter https://linux-hardware.org/?id=usb:2357-0116
    (0x2357, 0x0117, "RTL8822BU", None, None),  # (TP-Link) High Power Wireless USB Adapter https://linux-hardware.org/?id=usb:2357-0117
    (0x0BDA, 0xB812, "RTL8822BU", None, None),  # (Realtek) RTL88x2bu [AC1200 Techkey] https://linux-hardware.org/?id=usb:0BDA-B812
    (0x0BDA, 0xB82C, "RTL8822BU", None, None),  # (Realtek) 802.11ac NIC https://linux-hardware.org/?id=usb:0BDA-B82C
    (0x0BDA, 0xB81A, "RTL8822BU", None, None),  # (Realtek) 8812BU Wireless LAN 802.11ac USB NIC https://linux-hardware.org/?id=usb:0BDA-B81A
    (0x0B05, 0x1841, "RTL8822BU", None, ASUS.USB_AC55_B1),
    (0x0B05, 0x184C, "RTL8822BU", None, ASUS.USB_AC53_NANO),
    (0x0B05, 0x19AA, "RTL8822BU", None, ASUS.USB_AC58_A1),
    (0x2001, 0x331E, "RTL8822BU", None, DLink.DWA_181),
    (0x2001, 0x331C, "RTL8822BU", None, DLink.DWA_182_D1),
    (0x13B1, 0x0043, "RTL8822BU", None, Linksys.WUSB6400M),
    (0x13B1, 0x0045, "RTL8822BU", None, Linksys.WUSB6300_V2),
    (0x0846, 0x9055, "RTL8822BU", None, Netgear.A6150),
    (0x7392, 0xB822, "RTL8822BU", None, Edimax.EW_7822ULC),
    (0x7392, 0xC822, "RTL8822BU", None, Edimax.EW_7822UTC),
    (0x7392, 0xD822, "RTL8822BU", None, None),  # (Edimax) Dacota Platinum AC1200 USB 2.0 Wireless Adapter https://linux-hardware.org/?id=usb:7392-D822
    (0x7392, 0xE822, "RTL8822BU", None, None),  # (Edimax) Dacota Platinum AC1200 USB 3.0 Wireless Adapter https://linux-hardware.org/?id=usb:7392-E822
    (0x7392, 0xF822, "RTL8822BU", None, Edimax.EW_7822UAD),
    (0x2C4E, 0x0107, "RTL8822BU", None, Mercusys.MA30H),
    (0x2C4E, 0x010A, "RTL8822BU", None, Mercusys.MA30N),
    (0x0411, 0x03D1, "RTL8822BU", None, Buffalo.WI_U2_866DM),
    (0x0411, 0x03D0, "RTL8822BU", None, Buffalo.WI_U3_866DHP),
    (0x04CA, 0x8602, "RTL8822BU", None, LiteOn.WN8602L),
    (0x056E, 0x4011, "RTL8822BU", None, Elecom.WDB_867DU3S),
    (0x0B05, 0x1870, "RTL8822BU", None, ASUS._8822BU_1870),
    (0x0B05, 0x1874, "RTL8822BU", None, ASUS._8822BU_1874),
    (0x0BDA, 0x2102, "RTL8822BU", None, CCandC._433MBPS),
    (0x0E66, 0x0025, "RTL8822BU", None, Hawking.HW12ACU),
    (0x2001, 0x331F, "RTL8822BU", None, DLink.DWA_183_D),
    (0x2001, 0x3322, "RTL8822BU", None, DLink.DWA_T185_REV_A1),
    (0x20F4, 0x805A, "RTL8822BU", None, TRENDnet.TEW_805UBH),
    (0x20F4, 0x808A, "RTL8822BU", None, TRENDnet.TEW_808UBM),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import Rtl8822buDkmsDriver
    return Rtl8822buDkmsDriver
