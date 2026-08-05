from wifit3.models.device_id import DeviceID

SUPPORTED_IDS = [
    DeviceID(0x148F, 0x5372, "RT5372", product_name="Panda PAU05/06"),
]


def import_driver():
    from .driver import RT5372Driver
    return RT5372Driver
