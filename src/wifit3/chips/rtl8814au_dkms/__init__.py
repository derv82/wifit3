"""RTL8814AU vendor (morrownr DKMS) cleanroom port. See RTL8814AU_DKMS.md."""
from wifit3.models.device_id import DeviceID

from .constants import PID_RTL8814AU, VID_REALTEK
from wifit3.chips.products import ALFA

SUPPORTED_IDS = [
    DeviceID(VID_REALTEK, PID_RTL8814AU, "RTL8814AU",
             product_name=ALFA.AWUS1900),
]


def import_driver():
    from .driver import Rtl8814auDkmsDriver
    return Rtl8814auDkmsDriver
