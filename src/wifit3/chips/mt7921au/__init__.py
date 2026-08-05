from wifit3.models.device_id import DeviceID

SUPPORTED_IDS = [
    DeviceID(0x0e8d, 0x7961, "MT7921AU", product_name="ALFA AWUS036AXML / Panda PAU0F"),
]


def import_driver():
    from .driver import MT7921AUDriver
    return MT7921AUDriver
