from wifit3.models.device_id import DeviceID

_IDS = (
    (0x14C3, 0x7922, "MT7922", "MediaTek", "MT7922 802.11ax PCIe Wireless Network Adapter"),
    (0x14C3, 0x0608, "MT7921K", "MediaTek", "MT7921K 802.11ax PCIe Wireless Network Adapter"),
    (0x14C3, 0x7961, "MT7921E", "MediaTek", "MT7921e 802.11ax PCIe Wireless Network Adapter"),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product, extras={"bus_type": "pci"})
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import LinuxNetdevDriver
    return LinuxNetdevDriver
