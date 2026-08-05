"""RTL8821AU (mainline-derived) — VID:PID declaration readable without importing driver.py.

Full RTL8821AU VID:PID set from the aircrack-rtl8812au table (``.driver_info = RTL8821``);
kept in lockstep with the DKMS sibling ``chips/rtl8821au_dkms``. All one silicon.
"""
from wifit3.models.device_id import DeviceID

_IDS = (
    (0x0BDA, 0x0811, "RTL8821AU", None, "ALFA AWUS036ACS"),
    (0x0BDA, 0x0821, "RTL8821AU", None, None),
    (0x0BDA, 0x8822, "RTL8821AU", None, None),
    (0x0BDA, 0xA811, "RTL8821AU", None, None),
    (0x0BDA, 0x0820, "RTL8821AU", None, None),
    (0x0BDA, 0x0823, "RTL8821AU", None, None),
    (0x0411, 0x0242, "RTL8821AU", None, "ELECOM WDC-433DU2H"),
    (0x0411, 0x029B, "RTL8821AU", None, "Buffalo WI-U2-433DHP"),
    (0x04BB, 0x0953, "RTL8821AU", None, "I-O DATA Edimax"),
    (0x056E, 0x4007, "RTL8821AU", None, "Elecom WDC-433DU2HBK"),
    (0x056E, 0x400E, "RTL8821AU", None, "ELECOM"),
    (0x056E, 0x400F, "RTL8821AU", None, "ELECOM"),
    (0x056E, 0x4010, "RTL8821AU", None, "ELECOM"),
    (0x0846, 0x9052, "RTL8821AU", None, "Netgear A6100"),
    (0x0E66, 0x0023, "RTL8821AU", None, "HAWKING Edimax"),
    (0x2001, 0x3314, "RTL8821AU", None, "D-Link Cameo"),
    (0x2001, 0x3318, "RTL8821AU", None, "D-Link Cameo"),
    (0x2019, 0xAB32, "RTL8821AU", None, "Planex GW-450S"),
    (0x2357, 0x011E, "RTL8821AU", None, "TP-Link T2U Nano"),
    (0x2357, 0x011F, "RTL8821AU", None, "TP-Link Archer AC600 T2U Nano"),
    (0x2357, 0x0120, "RTL8821AU", None, "TP-Link T2U Plus"),
    (0x3823, 0x6249, "RTL8821AU", None, "Obihai OBiWiFi"),
    (0x7392, 0xA811, "RTL8821AU", None, "Edimax"),
    (0x7392, 0xA812, "RTL8821AU", None, "Edimax EW-7811UTC"),
    (0x7392, 0xA813, "RTL8821AU", None, "Edimax EW-7811UAC"),
    (0x7392, 0xB611, "RTL8821AU", None, "Edimax EW-7811UCB"),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8821AUDriver
    return RTL8821AUDriver
