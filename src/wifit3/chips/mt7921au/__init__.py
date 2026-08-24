from wifit3.models.device_id import DeviceID
from wifit3.chips.products import AMBIGUOUS_MT7921AU, Comfast, Netgear

_IDS = (
    (0x0E8D, 0x7961, "MT7921AU", None, AMBIGUOUS_MT7921AU),
    (0x3574, 0x6211, "MT7921AU", None, Comfast.CF_953AX),
    (0x0846, 0x9060, "MT7921AU", None, Netgear.A8000),
    (0x0846, 0x9065, "MT7921AU", None, Netgear.A7500),
    (0x35BC, 0x0107, "MT7921AU", None, None),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import MT7921AUDriver
    return MT7921AUDriver
