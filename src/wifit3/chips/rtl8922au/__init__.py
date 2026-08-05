from wifit3.models.device_id import DeviceID

SUPPORTED_IDS = [
    DeviceID(vid=0x0411, pid=0x03ef, chipset="RTL8922AU"),
    DeviceID(vid=0x0502, pid=0x76d7, chipset="RTL8922AU"),
    DeviceID(vid=0x056e, pid=0x4025, chipset="RTL8922AU"),
    DeviceID(vid=0x056e, pid=0x4026, chipset="RTL8922AU"),
    DeviceID(vid=0x057c, pid=0x8701, chipset="RTL8922AU"),
    DeviceID(vid=0x0b05, pid=0x1bcf, chipset="RTL8922AU", vendor="ASUS"),
    DeviceID(vid=0x0b05, pid=0x1bd2, chipset="RTL8922AU", vendor="ASUS"),
    DeviceID(vid=0x0b05, pid=0x1d84, chipset="RTL8922AU", product_name="ASUS USB-BE93"),
    DeviceID(vid=0x0bda, pid=0x8912, chipset="RTL8922AU"),
    DeviceID(vid=0x0db0, pid=0xda0e, chipset="RTL8922AU"),
    DeviceID(vid=0x2001, pid=0x332b, chipset="RTL8922AU"),
    DeviceID(vid=0x2c4e, pid=0x0125, chipset="RTL8922AU"),
    DeviceID(vid=0x3625, pid=0x010a, chipset="RTL8922AU"),
    DeviceID(vid=0x37ad, pid=0x0100, chipset="RTL8922AU"),
    DeviceID(vid=0x37ad, pid=0x0101, chipset="RTL8922AU"),
    DeviceID(vid=0x7392, pid=0x3822, chipset="RTL8922AU"),
    DeviceID(vid=0x7392, pid=0x4822, chipset="RTL8922AU"),
    DeviceID(vid=0x7392, pid=0x5822, chipset="RTL8922AU"),
]


def import_driver():
    from .driver import RTL8922AUDriver
    return RTL8922AUDriver
