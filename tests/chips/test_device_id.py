"""DeviceID structured fields: description shim."""
from wifit3.models import DeviceID


def test_description_composes_vendor_and_product():
    e = DeviceID(0x2357, 0x0106, "RTL8814AU", "TP-Link", "Archer T9UH")
    assert e.description == "RTL8814AU (TP-Link Archer T9UH)"


def test_description_product_only_when_vendor_none():
    e = DeviceID(0x0e8d, 0x7961, "MT7921AU", None, "ALFA AXML / Panda PAU0F")
    assert e.description == "MT7921AU (ALFA AXML / Panda PAU0F)"


def test_description_chipset_only_when_no_brand():
    assert DeviceID(0x0bda, 0xb812, "RTL8822BU").description == "RTL8822BU"
