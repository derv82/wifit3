"""RTL8187L driver package (ALFA AWUS036H).

Only the ``DEVICE_RTL8187`` (8187L) IDs from the kernel rtl8187 table: the
``DEVICE_RTL8187B`` entries are a different chip (separate TX header + init) not ported here.
"""
from wifit3.models.device_id import DeviceID

_IDS = (
    (0x0BDA, 0x8187, "RTL8187L", None, "ALFA AWUS036H"),
    (0x0B05, 0x171D, "RTL8187L", None, "Belkin"),
    (0x0769, 0x11F2, "RTL8187L", None, "Logitech"),
    (0x0789, 0x010C, "RTL8187L", None, "Netgear"),
    (0x0846, 0x6100, "RTL8187L", None, None),
    (0x0846, 0x6A00, "RTL8187L", None, None),
    (0x03F0, 0xCA02, "RTL8187L", None, "Sitecom"),
    (0x0DF6, 0x000D, "RTL8187L", None, None),
    (0x114B, 0x0150, "RTL8187L", None, "Dick Smith Electronics"),
    (0x1371, 0x9401, "RTL8187L", None, "Abocom"),
    (0x13D1, 0xABE6, "RTL8187L", None, "Qcom"),
    (0x18E8, 0x6232, "RTL8187L", None, "AirLive"),
    (0x1B75, 0x8187, "RTL8187L", None, "Linksys"),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8187Driver
    return RTL8187Driver
