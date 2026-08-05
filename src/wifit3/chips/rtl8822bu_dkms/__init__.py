"""RTL8822BU vendor/DKMS driver package: its VID:PID, readable without importing driver.py."""
from wifit3.models.device_id import DeviceID

# vid/pid inlined from driver.py's USB_VID_REALTEK / USB_PID_T3U_PLUS to stay driver-import-free.
SUPPORTED_IDS = [
    DeviceID(0x2357, 0x0138, "RTL8822BU",
             vendor="TP-Link", product_name="Archer T3U Plus"),
]


def import_driver():
    from .driver import Rtl8822buDkmsDriver
    return Rtl8822buDkmsDriver
