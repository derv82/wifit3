from wifit3.models.device_id import DeviceID
from wifit3.chips.products import MediaTek, Netgear

SUPPORTED_IDS = [
    DeviceID(0x0e8d, 0x7925, "MT7925AU", product_name=MediaTek.MT7925U),
    DeviceID(0x0846, 0x9072, "MT7925AU", product_name=Netgear.A9000),
]


def import_driver():
    from .driver import MT7925AUDriver
    return MT7925AUDriver
