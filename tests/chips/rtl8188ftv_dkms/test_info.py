"""rtl8188ftv_dkms M1: chip-version decode.

The recorded SYS_CFG value pins the parse; synthetic values cover the
vendor/test-chip arms.
"""


class FakeT:
    def __init__(self, value32):
        self._value32 = value32

    def read32(self, addr):
        assert addr == 0x00F0
        return self._value32


def test_recorded_sys_cfg_decodes():
    from wifit3.chips.rtl8188ftv_dkms import info
    version = info.read_chip_version(FakeT(0x44441525))   # capture-2 op0
    assert version.test_chip is False
    assert version.vendor == info.VENDOR_SMIC
    assert info.is_smic(version) is True
    assert version.cut == 1
    assert version.rf_paths == 1


def test_vendor_arms():
    from wifit3.chips.rtl8188ftv_dkms import info
    assert info.read_chip_version(FakeT(0x00000000)).vendor == info.VENDOR_TSMC
    assert info.read_chip_version(FakeT(0x00040000)).vendor == info.VENDOR_SMIC
    assert info.read_chip_version(FakeT(0x00080000)).vendor == info.VENDOR_UMC
    assert info.read_chip_version(FakeT(0x000C0000)).vendor == info.VENDOR_TSMC


def test_test_chip_arm():
    from wifit3.chips.rtl8188ftv_dkms import info
    assert info.read_chip_version(FakeT(0x00800000)).test_chip is True
