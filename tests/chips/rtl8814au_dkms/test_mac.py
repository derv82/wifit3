"""Hardware-free regression for the M2a MAC register table.

Golden anchors are from the vendor source `array_mp_8814a_mac_reg`; the full
byte-for-byte check vs the capture is `scripts/rtl8814au_dkms/verify_pcap.py`.
"""
from wifit3.chips.rtl8814au_dkms import mac


def test_mac_table_shape():
    t = mac.MAC_REG_TABLE
    assert len(t) == 143
    assert all(0 <= a <= 0xFFFF and 0 <= v <= 0xFF for a, v in t)
    assert t[0] == (0x010, 0x7C)      # first table entry
    assert t[-1] == (0x7DA, 0x0B)     # last table entry
    assert (0x608, 0x0E) in t         # RCR seed lives in the MAC table


def test_phy_mac_config_emits_table_as_write8():
    calls = []

    class Rec:
        def write8(self, a, v):
            calls.append((a, v))

    mac.phy_mac_config(Rec())
    assert calls == [(a, v) for a, v in mac.MAC_REG_TABLE]
