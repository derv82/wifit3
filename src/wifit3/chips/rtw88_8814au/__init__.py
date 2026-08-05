"""RTL8814AU driver package: the VID:PIDs it claims, readable without importing driver.py."""
from wifit3.models.device_id import DeviceID

from .constants import USB_IDS_8814AU

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in USB_IDS_8814AU
]


def import_driver():
    from .driver import RTL8814AUDriver
    return RTL8814AUDriver
