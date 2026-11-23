"""rt3572 chipset driver (Ralink RT3572)."""
from wifit3.models.device_id import DeviceID

from .constants import USB_PID_RT3572, USB_VID_RALINK
from .transport import RT3572Transport
from wifit3.chips.products import ALFA

SUPPORTED_IDS = [
    DeviceID(USB_VID_RALINK, USB_PID_RT3572, "RT3572",
             product_name=ALFA.AWUS051NH_V2),
    # 148f:5572 (RT5572 / PAU09) is now the standalone chips/rt5572 driver.
]


def import_driver():
    from .driver import RT3572Driver
    return RT3572Driver


__all__ = ["SUPPORTED_IDS", "RT3572Transport", "import_driver"]
