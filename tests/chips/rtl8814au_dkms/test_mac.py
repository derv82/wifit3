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


class _Rec:
    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}

    def read8(self, a):
        self.ops.append(("R", a))
        return self.reads.get(a, 0)

    def write8(self, a, v):
        self.ops.append(("W", a, v))


def test_hal_init_turn_on():
    rec = _Rec(reads={0x4C6: 0x04})
    mac.hal_init_turn_on(rec, "00:c0:ca:b8:bd:93")
    w = {a: v for k, a, v in (o for o in rec.ops if o[0] == "W")}
    assert w[0x4C6] == 0x04                 # REG_QUEUE_CTRL & 0xF7 (bit3 already clear)
    assert w[0x652] == 0xEB                 # NAV upper = roundup(30000/128)
    assert w[0x421] == 0x0F                 # Tx-report enable
    assert w[0x070] == 0x00 and w[0x03E] == 0x00
    # MAC address programmed to REG_MACID (0x610..0x615), then read back.
    assert [w[0x610 + i] for i in range(6)] == [0x00, 0xC0, 0xCA, 0xB8, 0xBD, 0x93]
    assert all(("R", 0x610 + i) in rec.ops for i in range(6))


def test_hal_init_turn_on_requires_mac():
    import pytest
    with pytest.raises(ValueError):
        mac.hal_init_turn_on(_Rec(reads={0x4C6: 0x04}), None)
