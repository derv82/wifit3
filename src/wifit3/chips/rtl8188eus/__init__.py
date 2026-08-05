"""RTL8188EUS driver — TP-Link TL-WN722N v2/v3, etc.

Cleanroom port of the kernel `rtl8xxxu` driver (specifically the 8188e
fileops vector), not to be confused with the unrelated `rtw88` family.
"""
from wifit3.models.device_id import DeviceID

SUPPORTED_IDS = [
    DeviceID(0x2357, 0x010C, "RTL8188EUS", vendor="TP-Link", product_name="TL-WN722N v2/v3"),
]


def import_driver():
    from .driver import RTL8188EUSDriver
    return RTL8188EUSDriver
