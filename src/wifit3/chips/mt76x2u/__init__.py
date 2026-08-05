"""MT76x2U chip package. Exposes SUPPORTED_IDS without importing driver.py."""
from wifit3.models.device_id import DeviceID

from .constants import USB_IDS_MT76X2U

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in USB_IDS_MT76X2U
]


def import_driver():
    from .driver import MT76x2UDriver
    return MT76x2UDriver
