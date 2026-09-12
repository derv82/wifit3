"""rt3070 chipset driver (Ralink RT3070, ALFA AWUS036NH)."""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import ALFA, DLink

SUPPORTED_IDS = [
    DeviceID(0x148F, 0x3070, "RT3070", product_name=ALFA.AWUS036NH),
    DeviceID(0x07D1, 0x3C0A, "RT3072", product_name=DLink.DWA_140_REV_B2),
]


def import_driver():
    from .driver import RT3070Driver
    return RT3070Driver
