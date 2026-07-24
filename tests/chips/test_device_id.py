"""DeviceID structured fields: silicon_vendor derivation + the description shim."""
from wifit3.chips.driver import DeviceID


def test_silicon_vendor_from_chipset_prefix():
    assert DeviceID(0x0bda, 0x8812, "RTL8812AU").silicon_vendor == "Realtek"
    assert DeviceID(0x0e8d, 0x7612, "MT7612U").silicon_vendor == "MediaTek"
    assert DeviceID(0x148f, 0x2570, "RT2570").silicon_vendor == "Ralink"
    assert DeviceID(0x0cf3, 0x9271, "AR9271").silicon_vendor == "Atheros"


def test_silicon_vendor_rtl_resolves_before_rt():
    # "RTL8xxx" also startswith "RT"; the map order must resolve it to Realtek, not Ralink.
    assert DeviceID(0x0bda, 0x8187, "RTL8187L").silicon_vendor == "Realtek"


def test_description_composes_vendor_and_product():
    e = DeviceID(0x2357, 0x0106, "RTL8814AU", "TP-Link", "Archer T9UH")
    assert e.description == "RTL8814AU (TP-Link Archer T9UH)"


def test_description_product_only_when_vendor_none():
    e = DeviceID(0x0e8d, 0x7961, "MT7921AU", None, "ALFA AXML / Panda PAU0F")
    assert e.description == "MT7921AU (ALFA AXML / Panda PAU0F)"


def test_description_chipset_only_when_no_brand():
    assert DeviceID(0x0bda, 0xb812, "RTL8822BU").description == "RTL8822BU"
