"""RTL8187L / RTL8187B driver package (ALFA AWUS036H): every ``rtl8187_table`` entry (dev.c:43-80).

The chipset field carries the table's ``driver_info`` (DEVICE_RTL8187 / DEVICE_RTL8187B).
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import (
    ALFA, AMax, ASUS, AirLive, Belkin, CNet, Linksys, Logitec, Netgear, Qcom, SureCom, Turbolink,
)

from .constants import DEVICE_RTL8187, DEVICE_RTL8187B

_IDS = (
    (0x0BDA, 0x8187, DEVICE_RTL8187, None, ALFA.AWUS036H),
    (0x0B05, 0x171D, DEVICE_RTL8187, None, ASUS.W_LINK),
    (0x0769, 0x11F2, DEVICE_RTL8187, None, SureCom.EP_9001G),
    (0x0789, 0x010C, DEVICE_RTL8187, None, Logitec.RTL8187),
    (0x0846, 0x6100, DEVICE_RTL8187, None, Netgear.RTL8187),
    (0x0846, 0x6A00, DEVICE_RTL8187, None, Netgear.WG111_V1_V2),
    (0x03F0, 0xCA02, DEVICE_RTL8187, None, AMax._802_11G),
    (0x0DF6, 0x000D, DEVICE_RTL8187, None, None),
    (0x114B, 0x0150, DEVICE_RTL8187, None, Turbolink.UB801RE),
    (0x1371, 0x9401, DEVICE_RTL8187, None, CNet.CWD_8554),
    (0x13D1, 0xABE6, DEVICE_RTL8187, None, AMax._54MBPS),
    (0x18E8, 0x6232, DEVICE_RTL8187, None, Qcom._54G),
    (0x1B75, 0x8187, DEVICE_RTL8187, None, AirLive.WN_370USB),
    (0x050D, 0x705E, DEVICE_RTL8187B, None, Belkin.F5D7050_V5),
    (0x0BDA, 0x8189, DEVICE_RTL8187B, None, None),
    (0x0BDA, 0x8197, DEVICE_RTL8187B, None, None),
    (0x0BDA, 0x8198, DEVICE_RTL8187B, None, None),
    (0x0846, 0x4260, DEVICE_RTL8187B, None, Netgear.WG111V3),
    (0x0DF6, 0x0028, DEVICE_RTL8187B, None, None),
    (0x0DF6, 0x0029, DEVICE_RTL8187B, None, None),
    (0x1737, 0x0073, DEVICE_RTL8187B, None, Linksys.WUSB54GC_V2),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8187Driver
    return RTL8187Driver
