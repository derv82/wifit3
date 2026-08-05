"""RTL8821CU (Realtek 8821c, 1T1R 802.11ac, USB 0bda:c820) — vendor/DKMS cleanroom port.

Self-contained by design: this package shares no code with the other Realtek drivers
(anti-DRY — a fix in a shared base would force re-verification across every card). The
register sequences are re-ported here verbatim from the vendor `rtl8821cu-5.12.0.4` tree.
"""
from wifit3.models.device_id import DeviceID

from .constants import USB_PID_8821CU, USB_VID_REALTEK

SUPPORTED_IDS = [
    DeviceID(USB_VID_REALTEK, USB_PID_8821CU, "RTL8821CU", product_name="Auscoumer 600 Mbps"),
]


def import_driver():
    from .driver import Rtl8821cuDkmsDriver
    return Rtl8821cuDkmsDriver
