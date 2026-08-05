"""RTL8821AU (mainline-derived) — VID:PID declaration readable without importing driver.py."""
from wifit3.models.device_id import DeviceID

SUPPORTED_IDS = [
    DeviceID(0x0BDA, 0x0811, "RTL8821AU", product_name="ALFA AWUS036ACS"),
]


def import_driver():
    from .driver import RTL8821AUDriver
    return RTL8821AUDriver
