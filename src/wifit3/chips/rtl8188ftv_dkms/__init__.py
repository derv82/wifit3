"""RTL8188FTV DKMS (vendor) port — scaffold.

Cleanroom port of the ``kelebek333/rtl8188fu`` vendor/DKMS driver
(``v4.3.23.6_20964.20170110``) for hotter, more stable 2.4 GHz monitor RX.
See ``RTL8188FTV_DKMS.md`` for coordinates and per-milestone status.

Claims ``0bda:f179`` via the ``rtl8188ftv`` family row in ``device/manager.py``.
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
    from .driver import Rtl8188ftvDkmsDriver
    return Rtl8188ftvDkmsDriver
