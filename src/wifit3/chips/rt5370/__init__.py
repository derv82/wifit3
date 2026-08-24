from wifit3.models.device_id import DeviceID
from wifit3.chips.products import LOTEKOO

SUPPORTED_IDS = [
    DeviceID(0x148F, 0x5370, "RT5370", product_name=LOTEKOO._150MBPS),
]


def import_driver():
    from .driver import RT5370Driver
    return RT5370Driver
