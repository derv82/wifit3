"""rt3070 chipset driver (Ralink RT3070, ALFA AWUS036NH)."""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import ALFA

SUPPORTED_IDS = [
    DeviceID(0x148F, 0x3070, "RT3070", product_name=ALFA.AWUS036NH),
]


def import_driver():
    from .driver import RT3070Driver
    return RT3070Driver
