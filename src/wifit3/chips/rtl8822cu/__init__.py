"""RTL8822CU USB adapter monitor-mode RX support."""
from wifit3.models.device_id import DeviceID

from .constants import USB_IDS_RTL8822CU

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for vid, pid, chipset, vendor, product in USB_IDS_RTL8822CU
]


def import_driver():
    from .driver import RTL8822CUDriver
    return RTL8822CUDriver
