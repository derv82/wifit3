"""RTL8821CU (Realtek 8821c, 1T1R 802.11ac, USB 0bda:c820) — vendor/DKMS cleanroom port.

Self-contained by design: this package shares no code with the other Realtek drivers
(anti-DRY — a fix in a shared base would force re-verification across every card). The
register sequences are re-ported here verbatim from the vendor `rtl8821cu-5.12.0.4` tree.

Full RTL8821CU/8811CU VID:PID set from the vendor rtl8821cu table (``.driver_info = RTL8821C``).
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import Auscoumer, DLink, Edimax, TOTOLINK

_IDS = (
    (0x0BDA, 0xC820, "RTL8821CU", None, Auscoumer._600),
    (0x0BDA, 0xB82B, "RTL8821CU", None, None),
    (0x0BDA, 0xB820, "RTL8821CU", None, None),
    (0x0BDA, 0xC821, "RTL8821CU", None, None),
    (0x0BDA, 0xC82A, "RTL8821CU", None, None),
    (0x0BDA, 0xC82B, "RTL8821CU", None, None),
    (0x0BDA, 0xC811, "RTL8821CU", None, None),
    (0x0BDA, 0x8811, "RTL8821CU", None, None),
    (0x0BDA, 0x2006, "RTL8821CU", None, TOTOLINK.A650UA_V3),
    (0x0BDA, 0x8731, "RTL8821CU", None, None),
    (0x0BDA, 0xC80C, "RTL8821CU", None, None),
    (0x7392, 0xC811, "RTL8821CU", None, Edimax._8811CU),
    (0x7392, 0xD811, "RTL8821CU", None, Edimax._8811CU),
    (0x2001, 0x331D, "RTL8821CU", None, DLink.DWA_171C),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import Rtl8821cuDkmsDriver
    return Rtl8821cuDkmsDriver
