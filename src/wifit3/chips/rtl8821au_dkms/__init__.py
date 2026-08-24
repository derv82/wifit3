"""RTL8821AU (RTL8811AU) — vendor/DKMS port (Realtek PHYDM/ODM stack).

Cleanroom re-port from the Lucid-Duck ``8821au-20210708`` 5.12.5.2 vendor source
(the DKMS-distributed out-of-tree rtl88xxau driver), NOT mainline ``rtw88``. Lives
beside the mainline-derived ``chips/rtl8821au/`` as a sibling; both register for
0bda:0811 and are ordered by ``$WIFIT3_RTL8821`` (DKMS default, ``=mainline``
falls back). See ``RTL8821AU_DKMS.md`` for the per-milestone ground truth.

VID:PID set kept in lockstep with the mainline sibling ``chips/rtl8821au`` (all one silicon).
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import ALFA, Buffalo, DLink, Edimax, Elecom, Hawking, IODATA, Netgear, Obihai, Planex, TPLink

_IDS = (
    (0x0BDA, 0x0811, "RTL8821AU", None, ALFA.AWUS036ACS),
    (0x0BDA, 0x0821, "RTL8821AU", None, None),  # (Realtek) RTL8821A Bluetooth     https://linux-hardware.org/?id=usb:0bda-0821
    (0x0BDA, 0x8822, "RTL8821AU", None, None),  # (Realtek) RTL8821AU 802.11ac     http://linux-hardware.org/?id=usb:0bda-8822
    (0x0BDA, 0xA811, "RTL8821AU", None, None),  # (Realtek) RTL8811AU 802.11abgnac https://linux-hardware.org/?id=usb:0bda-a811
    (0x0BDA, 0x0820, "RTL8821AU", None, None),  # (Realtek) RTL8821AU 802.11ac     http://linux-hardware.org/?id=usb:0bda-0820
    (0x0BDA, 0x0823, "RTL8821AU", None, None),  # (Realtek/I-O DATA) 802.11ac      http://linux-hardware.org/?id=usb:0bda-0823
    (0x0411, 0x0242, "RTL8821AU", None, Buffalo.WI_U2_433DM),
    (0x0411, 0x029B, "RTL8821AU", None, Buffalo.WI_U2_433DHP),
    (0x04BB, 0x0953, "RTL8821AU", None, IODATA.WN_AC867U),
    (0x056E, 0x4007, "RTL8821AU", None, Elecom.WDC_433DU2HBK),
    (0x056E, 0x400E, "RTL8821AU", None, Elecom.WDC_433SU2M2),
    (0x056E, 0x400F, "RTL8821AU", None, Elecom.WDB_433SU2M2),
    (0x056E, 0x4010, "RTL8821AU", None, Elecom.LD_USB20),
    (0x0846, 0x9052, "RTL8821AU", None, Netgear.A6100),
    (0x0E66, 0x0023, "RTL8821AU", None, Hawking.HD65U_23),
    (0x2001, 0x3314, "RTL8821AU", None, DLink.DWA_171_REV_A1),
    (0x2001, 0x3318, "RTL8821AU", None, DLink.DWA_172),
    (0x2019, 0xAB32, "RTL8821AU", None, Planex.GW_450S),
    (0x2357, 0x011E, "RTL8821AU", None, TPLink.ARCHER_T2U_NANO),
    (0x2357, 0x011F, "RTL8821AU", None, TPLink.ARCHER_T2U_V3),
    (0x2357, 0x0120, "RTL8821AU", None, TPLink.ARCHER_T2U_PLUS),
    (0x3823, 0x6249, "RTL8821AU", None, Obihai.OBIWIFI),
    (0x7392, 0xA811, "RTL8821AU", None, Edimax.EW_7811UTC),
    (0x7392, 0xA812, "RTL8821AU", None, Edimax.EW_7811UTC_AC),
    (0x7392, 0xA813, "RTL8821AU", None, Edimax.EW_7811UAC),
    (0x7392, 0xB611, "RTL8821AU", None, Edimax.EW_7811UCB),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import Rtl8821auDkmsDriver
    return Rtl8821auDkmsDriver
