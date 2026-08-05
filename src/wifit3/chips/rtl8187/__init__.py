"""RTL8187L driver package (ALFA AWUS036H)."""
from wifit3.models.device_id import DeviceID

from .constants import USB_PID_RTL8187, USB_VID_REALTEK

SUPPORTED_IDS = [
    DeviceID(USB_VID_REALTEK, USB_PID_RTL8187, "RTL8187L", product_name="ALFA AWUS036H"),
]


def import_driver():
    from .driver import RTL8187Driver
    return RTL8187Driver
