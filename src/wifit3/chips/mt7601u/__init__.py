"""MT7601U chip package. Exposes SUPPORTED_IDS without importing driver.py.

VID:PID list is ported 1:1 from driver_sources/mt7601u-source-v7.2/mt7601u/usb.c:14
(the kernel's `mt7601u_device_table`). Every entry is a generic MT7601U: the table
carries no retail-brand IDs, and this driver does no OUI-based product narrowing,
so `product_name` stays None and the UI shows the chipset alone.
"""
from wifit3.models.device_id import DeviceID

SUPPORTED_IDS = [
    DeviceID(0x0B05, 0x17D3, "MT7601U"),
    DeviceID(0x0E8D, 0x760A, "MT7601U"),
    DeviceID(0x0E8D, 0x760B, "MT7601U"),
    DeviceID(0x13D3, 0x3431, "MT7601U"),
    DeviceID(0x13D3, 0x3434, "MT7601U"),
    DeviceID(0x148F, 0x7601, "MT7601U"),
    DeviceID(0x148F, 0x760A, "MT7601U"),
    DeviceID(0x148F, 0x760B, "MT7601U"),
    DeviceID(0x148F, 0x760C, "MT7601U"),
    DeviceID(0x148F, 0x760D, "MT7601U"),
    DeviceID(0x2001, 0x3D04, "MT7601U"),
    DeviceID(0x2717, 0x4106, "MT7601U"),
    DeviceID(0x2955, 0x0001, "MT7601U"),
    DeviceID(0x2955, 0x1001, "MT7601U"),
    DeviceID(0x2955, 0x1003, "MT7601U"),
    DeviceID(0x2A5F, 0x1000, "MT7601U"),
    DeviceID(0x7392, 0x7710, "MT7601U"),
]


def import_driver():
    from .driver import MT7601UDriver

    return MT7601UDriver