"""AR9271 chip package. Exposes SUPPORTED_IDS without importing driver.py."""
from wifit3.models.device_id import DeviceID

from .constants import AR9271_PID, AR9271_VID

SUPPORTED_IDS = [
    DeviceID(AR9271_VID, AR9271_PID, "AR9271", product_name="ALFA AWUS036NHA"),
]


def import_driver():
    from .driver import AR9271V2Driver
    return AR9271V2Driver
