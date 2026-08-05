"""RTL8812AU (ALFA AWUS036ACH) mainline-derived port. See RTL8812AU.md."""
from wifit3.models.device_id import DeviceID

from .constants import USB_PID_AWUS036ACH, USB_VID_REALTEK

SUPPORTED_IDS = [
    DeviceID(USB_VID_REALTEK, USB_PID_AWUS036ACH, "RTL8812AU",
             product_name="ALFA AWUS036ACH"),
]


def import_driver():
    from .driver import RTL8812AUDriver
    return RTL8812AUDriver
