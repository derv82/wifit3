"""rt2800usb chipset driver (Ralink RT3572 / RT5572 family)."""
from wifit3.models.device_id import DeviceID

from .constants import USB_PID_RT3572, USB_VID_RALINK
from .transport import RT2800USBTransport
from wifit3.chips.products import ALFA

SUPPORTED_IDS = [
    DeviceID(USB_VID_RALINK, USB_PID_RT3572, "RT3572",
             product_name=ALFA.AWUS051NH_V2, extras={"chip_id": "rt3572"}),
    # 148f:5572 (RT5572 / PAU09) is now the standalone chips/rt5572 driver.
]


def import_driver():
    from .driver import RT2800USBDriver
    return RT2800USBDriver


__all__ = ["SUPPORTED_IDS", "RT2800USBTransport", "import_driver"]
