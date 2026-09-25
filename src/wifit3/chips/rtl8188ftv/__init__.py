"""RTL8188FTV driver — no-name 0bda:f179 dongles, etc.

Cleanroom port of the kernel `rtl8xxxu` mainline driver (the 8188f fileops
vector), not to be confused with the `rtw88` family. Captured from a
cold-boot usbmon session under `rtl8xxxu.ko` (kernel 6.12.107) — see
`driver_captures/captures_rtl8188ftv/`.

The 8188F silicon is also served by the vendor DKMS `rtl8188fu` driver;
that wire is captured separately (planned `chips/rtl8188ftv_dkms`).
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import Realtek

_IDS = (
    (0x0BDA, 0xF179, "RTL8188FTV", None, Realtek._8188FTV),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8188FTVDriver
    return RTL8188FTVDriver