from wifit3.chips.products import DLink
from wifit3.models.device_id import DeviceID


SUPPORTED_IDS = [
    DeviceID(0x2001, 0x3328, "RTL8192FU", "D-Link", DLink.AN3U_REV_A1),
]


def import_driver():
    from .driver import RTL8192FUDriver

    return RTL8192FUDriver
