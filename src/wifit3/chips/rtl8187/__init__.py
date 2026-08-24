"""RTL8187L driver package (ALFA AWUS036H).

Only the ``DEVICE_RTL8187`` (8187L) IDs from the kernel rtl8187 table: the
``DEVICE_RTL8187B`` entries are a different chip (separate TX header + init) not ported here.
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import ALFA, AMax, ASUS, AirLive, CNet, Logitec, Netgear, Qcom, SureCom, Turbolink

_IDS = (
    (0x0BDA, 0x8187, "RTL8187L", None, ALFA.AWUS036H),
    (0x0B05, 0x171D, "RTL8187L", None, ASUS.W_LINK),
    (0x0769, 0x11F2, "RTL8187L", None, SureCom.EP_9001G),
    (0x0789, 0x010C, "RTL8187L", None, Logitec.RTL8187),
    (0x0846, 0x6100, "RTL8187L", None, Netgear.RTL8187),
    (0x0846, 0x6A00, "RTL8187L", None, Netgear.WG111_V1_V2),
    (0x03F0, 0xCA02, "RTL8187L", None, AMax._802_11G),
    (0x0DF6, 0x000D, "RTL8187L", None, None),
    (0x114B, 0x0150, "RTL8187L", None, Turbolink.UB801RE),
    (0x1371, 0x9401, "RTL8187L", None, CNet.CWD_8554),
    (0x13D1, 0xABE6, "RTL8187L", None, AMax._54MBPS),
    (0x18E8, 0x6232, "RTL8187L", None, Qcom._54G),
    (0x1B75, 0x8187, "RTL8187L", None, AirLive.WN_370USB),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8187Driver
    return RTL8187Driver
