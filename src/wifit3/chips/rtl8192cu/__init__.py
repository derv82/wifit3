"""RTL8192CU / RTL8188CU / RTL8188RU mainline rtlwifi port. See RTL8192CU.md.

VID:PID set from rtlwifi rtl8192cu/sw.c rtl8192c_usb_ids[] (linux-stable v6.12.107), in table order.
"""
from wifit3.chips.products import ALFA, Belkin, DLink, Netgear, OnNetworks, Sitecom, TPLink
from wifit3.models.device_id import DeviceID

_IDS = (
    (0x0BDA, 0x8191, "RTL8192CU", "Realtek", None),
    (0x0BDA, 0x018A, "RTL8188CTV", "Realtek", None),
    (0x0BDA, 0x8170, "RTL8188CE-VAU", "Realtek", None),
    (0x0BDA, 0x8176, "RTL8188CU", "Realtek", None),
    (0x0BDA, 0x8177, "RTL8188CU", "Realtek", None),
    (0x0BDA, 0x817A, "RTL8188CU", "Realtek", None),
    (0x0BDA, 0x817B, "RTL8188CU", "Realtek", None),
    (0x0BDA, 0x817D, "RTL8188RU", "Realtek", None),
    (0x0BDA, 0x817E, "RTL8188CE-VAU", "Realtek", None),
    (0x0BDA, 0x817F, "RTL8188RU", "ALFA", ALFA.AWUS036NHR),
    (0x0BDA, 0x818A, "RTL8188CUS-VL", "Realtek", None),
    (0x0BDA, 0x819A, "RTL8188CU", "Realtek", None),
    (0x0BDA, 0x8754, "RTL8188CU", "Realtek", None),
    (0x0BDA, 0x8178, "RTL8192CU", "Realtek", None),
    (0x0BDA, 0x817C, "RTL8192CE-VAU", "Realtek", None),
    (0x050D, 0x1102, "RTL8188CU", "Belkin", None),
    (0x050D, 0x11F2, "RTL8188CU", "Belkin", None),
    (0x06F8, 0xE033, "RTL8188CU", "Hercules", None),
    (0x07B8, 0x8189, "RTL8188CU", "Funai", None),
    (0x0846, 0x9041, "RTL8188CU", "Netgear", Netgear.WNA1000M),
    (0x0846, 0x9043, "RTL8188CU", "Netgear", Netgear.WNA1000M_V2),
    (0x0B05, 0x17BA, "RTL8188CU", "ASUS", None),
    (0x0BDA, 0x5088, "RTL8188CU", "Thinkware", None),
    (0x0DF6, 0x0052, "RTL8188CU", "Sitecom", None),
    (0x0DF6, 0x005C, "RTL8188CU", "Sitecom", None),
    (0x0DF6, 0x0070, "RTL8188CU", "Sitecom", Sitecom._150N),
    (0x0DF6, 0x0077, "RTL8188CU", "Sitecom", Sitecom.WLA_2100_V2),
    (0x0EB0, 0x9071, "RTL8188CU", None, None),
    (0x4856, 0x0091, "RTL8188CU", "NetweeN", None),
    (0x103C, 0x1629, "RTL8188CUS", "HP", None),
    (0x13D3, 0x3357, "RTL8188CU", "AzureWave", None),
    (0x2001, 0x3308, "RTL8188CU", "D-Link", None),
    (0x2019, 0x4902, "RTL8188CU", "Planex", None),
    (0x2019, 0xAB2A, "RTL8188CU", "Planex", None),
    (0x2019, 0xAB2E, "RTL8188CU", "Abocom", None),
    (0x2019, 0xED17, "RTL8188CU", "PCI", None),
    (0x20F4, 0x648B, "RTL8188CU", "TRENDnet", None),
    (0x7392, 0x7811, "RTL8188CU", "Edimax", None),
    (0x13D3, 0x3358, "RTL8188CE-VAU", "AzureWave", None),
    (0x13D3, 0x3359, "RTL8188CE-VAU", "AzureWave", None),
    (0x4855, 0x0090, "RTL8188CU", "Feixun", None),
    (0x4855, 0x0091, "RTL8188CU", "NetweeN", None),
    (0x9846, 0x9041, "RTL8188CU", "Netgear", None),
    (0x0BDA, 0x317F, "RTL8188RU", "Netcore", None),
    (0x04F2, 0xAFF7, "RTL8188CUS", "Xavi", None),
    (0x04F2, 0xAFF9, "RTL8188CUS", "Xavi", None),
    (0x04F2, 0xAFFA, "RTL8188CUS", "Xavi", None),
    (0x0846, 0x9042, "RTL8188CUS", "On Networks", OnNetworks.N150MA),
    (0x04F2, 0xAFF8, "RTL8188CUS", "Xavi", None),
    (0x04F2, 0xAFFB, "RTL8188CUS", "Xavi", None),
    (0x04F2, 0xAFFC, "RTL8188CUS", "Xavi", None),
    (0x2019, 0x1201, "RTL8188CUS", "Planex", None),
    (0x050D, 0x1004, "RTL8192CU", "Belkin", Belkin.SURF_N300),
    (0x050D, 0x2102, "RTL8192CU", "Belkin", None),
    (0x050D, 0x2103, "RTL8192CU", "Belkin", None),
    (0x0586, 0x341F, "RTL8192CU", "Zyxel", None),
    (0x07AA, 0x0056, "RTL8192CU", "ATKK", None),
    (0x07B8, 0x8178, "RTL8192CU", "Funai", None),
    (0x0846, 0x9021, "RTL8192CU", "Netgear", None),
    (0x0846, 0xF001, "RTL8192CU", "On Networks", OnNetworks.N300MA),
    (0x0B05, 0x17AB, "RTL8192CU", "ASUS", None),
    (0x0BDA, 0x8186, "RTL8192CE-VAU", "Realtek", None),
    (0x0DF6, 0x0061, "RTL8192CU", "Sitecom", None),
    (0x0E66, 0x0019, "RTL8192CU", "Hawking", None),
    (0x2001, 0x3307, "RTL8192CU", "D-Link", None),
    (0x2001, 0x3309, "RTL8192CU", "D-Link", None),
    (0x2001, 0x330A, "RTL8192CU", "D-Link", None),
    (0x2001, 0x330D, "RTL8192CU", "D-Link", DLink.DWA_131),
    (0x2019, 0xAB2B, "RTL8192CU", "Planex", None),
    (0x20F4, 0x624D, "RTL8192CU", "TRENDnet", None),
    (0x2357, 0x0100, "RTL8192CU", "TP-Link", TPLink.TL_WN8200ND),
    (0x7392, 0x7822, "RTL8192CU", "Edimax", None),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import RTL8192CUDriver
    return RTL8192CUDriver
