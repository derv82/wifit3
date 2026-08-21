"""AR9271 chip package. Exposes SUPPORTED_IDS without importing driver.py.

Only the AR9271 (single-chip, 2.4 GHz) IDs from the ath9k_htc table: the AR7010-based
dual-band entries are a different device (separate firmware) this port does not drive.
"""
from wifit3.models.device_id import DeviceID

_IDS = (
    (0x0CF3, 0x9271, "AR9271", None, "ALFA AWUS036NHA / TL-WN722N v1"),
    (0x0CF3, 0x1006, "AR9271", None, "TP-Link TL-WN322G v3 / TL-WN422G v2"),
    (0x0846, 0x9030, "AR9271", None, "Netgear N150"),
    (0x07B8, 0x9271, "AR9271", None, "Altai WA1011N-GU"),
    (0x07D1, 0x3A10, "AR9271", None, "D-Link Wireless 150"),
    (0x13D3, 0x3327, "AR9271", None, "Azurewave"),
    (0x13D3, 0x3328, "AR9271", None, "Azurewave"),
    (0x13D3, 0x3346, "AR9271", None, "IMC Networks"),
    (0x13D3, 0x3348, "AR9271", None, "Azurewave"),
    (0x13D3, 0x3349, "AR9271", None, "Azurewave"),
    (0x13D3, 0x3350, "AR9271", None, "Azurewave"),
    (0x04CA, 0x4605, "AR9271", None, "Liteon"),
    (0x040D, 0x3801, "AR9271", None, "VIA"),
    (0x0CF3, 0xB003, "AR9271", None, "Ubiquiti WifiStation Ext"),
    (0x0CF3, 0xB002, "AR9271", None, "Ubiquiti WifiStation"),
    (0x057C, 0x8403, "AR9271", None, "AVM FRITZ!WLAN 11N v2 USB"),
    (0x0471, 0x209E, "AR9271", None, "Philips (or NXP) PTA01"),
    (0x1EDA, 0x2315, "AR9271", None, "AirTies"),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import AR9271V2Driver
    return AR9271V2Driver
