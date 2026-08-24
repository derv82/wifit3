from wifit3.models.device_id import DeviceID
from wifit3.chips.products import ASUS, DLink, Panda

SUPPORTED_IDS = [
    DeviceID(0x148F, 0x5372, "RT5372", product_name=Panda.PAU05_06),
    # https://linux-hardware.org/?view=search&busid=usb&name=RT5372&typeid=net%2Fwireless#list
    DeviceID(0x0B05, 0x17E8, "RT5372", product_name=ASUS.USB_N14),
    DeviceID(0x2001, 0x3317, "RT5372", product_name=DLink.DWA_137),
    DeviceID(0x2001, 0x3C15, "RT5372", product_name=DLink.DWA_140_REV_B3),
]

def import_driver():
    from .driver import RT5372Driver
    return RT5372Driver
