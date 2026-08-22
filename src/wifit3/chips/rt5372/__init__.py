from wifit3.models.device_id import DeviceID

SUPPORTED_IDS = [
    DeviceID(0x148F, 0x5372, "RT5372", product_name="Panda PAU05/06"),
    # https://linux-hardware.org/?view=search&busid=usb&name=RT5372&typeid=net%2Fwireless#list
    DeviceID(0x0B05, 0x17E8, "RT5372", product_name="ASUS USB-N14 N300"),
    DeviceID(0x2001, 0x3317, "RT5372", product_name="D-Link DWA-137 N300"),
    DeviceID(0x2001, 0x3C15, "RT5372", product_name="D-Link DWA-140 (rB30)"),
]

def import_driver():
    from .driver import RT5372Driver
    return RT5372Driver
