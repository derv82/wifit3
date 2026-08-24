from wifit3.models.device_id import DeviceID

from .constants import USB_PID_RT5572, USB_VID_RALINK
from .transport import RT5572Transport
from wifit3.chips.products import Panda

SUPPORTED_IDS = [
    DeviceID(USB_VID_RALINK, USB_PID_RT5572, "RT5572",
             product_name=Panda.PAU09_N600, extras={"chip_id": "rt5572"}),
]

__all__ = ['RT5572Transport', 'SUPPORTED_IDS', 'import_driver']


def import_driver():
    from .driver import RT5572Driver
    return RT5572Driver
