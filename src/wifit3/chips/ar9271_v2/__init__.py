"""AR9271 chip package. Exposes SUPPORTED_IDS without importing driver.py.

Only the AR9271 (single-chip, 2.4 GHz) IDs from the ath9k_htc table: the AR7010-based
dual-band entries are a different device (separate firmware) this port does not drive.
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import AMBIGUOUS_AR9271, AVM, AirTies, Altai, DLink, IMC, LiteOn, Netgear, Philips, TPLink, Ubiquiti, VIA

_IDS = (
    (0x0CF3, 0x9271, "AR9271", None, AMBIGUOUS_AR9271),
    (0x0CF3, 0x1006, "AR9271", None, TPLink.TL_WN322G_V2_V3),
    (0x0846, 0x9030, "AR9271", None, Netgear.N150),
    (0x07B8, 0x9271, "AR9271", None, Altai.WA1011N_GU),
    (0x07D1, 0x3A10, "AR9271", None, DLink.DWA_126),
    (0x13D3, 0x3327, "AR9271", None, IMC.AW_NU137),
    (0x13D3, 0x3328, "AR9271", None, IMC.AR9271_R28),
    (0x13D3, 0x3346, "AR9271", None, IMC.UB93),
    (0x13D3, 0x3348, "AR9271", None, IMC.AR9271_R48),
    (0x13D3, 0x3349, "AR9271", None, IMC.AR9271_R49),
    (0x13D3, 0x3350, "AR9271", None, IMC.AR9271_R50),
    (0x04CA, 0x4605, "AR9271", None, LiteOn.AR9271),
    (0x040D, 0x3801, "AR9271", None, VIA._802_11BGN),
    (0x0CF3, 0xB003, "AR9271", None, Ubiquiti.WIFISTATION_EXT),
    (0x0CF3, 0xB002, "AR9271", None, Ubiquiti.WIFISTATION),
    (0x057C, 0x8403, "AR9271", None, AVM.FRITZ_WLAN_N_V2),
    (0x0471, 0x209E, "AR9271", None, Philips.PTA01),
    (0x1EDA, 0x2315, "AR9271", None, AirTies.USB2),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import AR9271V2Driver
    return AR9271V2Driver
