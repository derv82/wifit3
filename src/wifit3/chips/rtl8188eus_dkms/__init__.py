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
from wifit3.chips.products import AboCom, DLink, Edimax, Elecom, Realtek, Sitecom, TPLink

_IDS = (
    (0x2357, 0x010C, "RTL8188EUS", None, TPLink.TL_WN722N_V2_V3),
    (0x0BDA, 0x8179, "RTL8188EUS", None, TPLink.TL_WN723N_V2_3_4),
    (0x0BDA, 0x0179, "RTL8188EUS", None, Realtek._8818EUS),
    (0x07B8, 0x8179, "RTL8188EUS", None, AboCom.BGN_MINI),
    (0x0DF6, 0x0076, "RTL8188EUS", None, Sitecom.N150_V2),
    (0x2001, 0x330F, "RTL8188EUS", None, DLink.DWA_125_REV_D1),
    (0x2001, 0x3310, "RTL8188EUS", None, DLink.DWA_123_REV_D1),
    (0x2001, 0x3311, "RTL8188EUS", None, DLink.GO_N150_REV_B1),
    (0x2001, 0x331B, "RTL8188EUS", None, DLink.DWA_121B1),
    (0x056E, 0x4008, "RTL8188EUS", None, Elecom.WDC_150SU2M),
    (0x7392, 0xB811, "RTL8188EUS", None, Edimax.EW_7811UN_V2),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import Rtl8188eusDkmsDriver
    return Rtl8188eusDkmsDriver
