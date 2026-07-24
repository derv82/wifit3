"""Splash interface-list labels: left margin, dedupe counter, brand tail, chipset alignment."""
from wifit3.chips.driver import DeviceID
from wifit3.ui.screens.splash import device_list_labels


def test_lone_chipset_has_a_left_margin_and_no_index():
    devs = [DeviceID(0x0e8d, 0x7961, "MT7921AU", None, "ALFA AXML / Panda PAU0F")]
    assert device_list_labels(devs) == [" MT7921AU · ALFA AXML / Panda PAU0F"]


def test_duplicate_chipsets_get_numbered():
    a = DeviceID(0x148f, 0x3070, "RT3070", None, "ALFA AWUS036NH")
    assert device_list_labels([a, a]) == [
        " RT3070 #1 · ALFA AWUS036NH", " RT3070 #2 · ALFA AWUS036NH"]


def test_vendor_and_product_join_with_a_space():
    devs = [DeviceID(0x2357, 0x0106, "RTL8814AU", "TP-Link", "Archer T9UH")]
    assert device_list_labels(devs) == [" RTL8814AU · TP-Link Archer T9UH"]


def test_no_brand_tail_when_vendor_and_product_are_none():
    assert device_list_labels([DeviceID(0x0bda, 0xb812, "RTL8822BU")]) == [" RTL8822BU"]


def test_mixed_list_numbers_only_the_duplicated_chipset():
    rt = DeviceID(0x148f, 0x3070, "RT3070", None, "ALFA AWUS036NH")
    mt = DeviceID(0x0e8d, 0x7961, "MT7921AU", None, "ALFA AXML")
    assert device_list_labels([rt, rt, mt]) == [
        " RT3070 #1 · ALFA AWUS036NH",
        " RT3070 #2 · ALFA AWUS036NH",
        " MT7921AU  · ALFA AXML",      # ljust padding: MT7921AU is 8, RT3070 #n is 9
    ]


def test_alpha_prefix_left_padded_so_model_digits_and_separators_align():
    rtl = DeviceID(0x0bda, 0x8812, "RTL8812AU", None, "ALFA AWUS036ACH")
    mt = DeviceID(0x0e8d, 0x7612, "MT7612U", None, "ALFA AWUS036ACM")
    labels = device_list_labels([rtl, mt])
    # 1-space margin on both; RTL prefix is 3 wide so MT (2) takes an extra pad space, landing both
    # model digits in the same column and both middots in the same column.
    assert labels == [" RTL8812AU · ALFA AWUS036ACH", "  MT7612U  · ALFA AWUS036ACM"]
    assert labels[0].index("8") == labels[1].index("7")     # model digits aligned
    assert labels[0].index("·") == labels[1].index("·")     # separators aligned
