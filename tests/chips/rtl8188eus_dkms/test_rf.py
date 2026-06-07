"""Hardware-free regression for the RTL8188EUS (DKMS) RF (radio-A) config.

Full byte-for-byte replay lives in ``scripts/rtl8188eus_dkms/verify_pcap.py``;
this locks the LSSI write encoding and the RFENV setup ordering.
"""
from wifit3.chips.rtl8188eus_dkms import rf
from wifit3.chips.rtl8188eus_dkms.constants import RF_LSSI_WRITE_A


class Tx:
    def __init__(self, reads=None):
        self.w32 = []
        self._reads = dict(reads or {})

    def read32(self, a):
        return self._reads.get(a, 0x00000000)

    def write32(self, a, v):
        self.w32.append((a, v & 0xFFFFFFFF))


def test_lssi_write_encoding():
    # First radio_a row 0x000,0x00030000 -> ((0<<20)|0x30000)&0x0FFFFFFF = 0x00030000.
    t = Tx()
    rf._emit_rf(t)(0x000, 0x00030000)
    assert t.w32 == [(RF_LSSI_WRITE_A, 0x00030000)]
    # RF reg 0x18 with a 20-bit value: addr in [27:20], data in [19:0].
    t = Tx()
    rf._emit_rf(t)(0x018, 0x00012345)
    assert t.w32 == [(RF_LSSI_WRITE_A, (0x18 << 20) | 0x12345)]


def test_delay_address_is_not_written():
    t = Tx()
    rf._emit_rf(t)(0xFFE, 0x00000000)   # 50 ms settling delay marker
    assert t.w32 == []


def test_rf_config_rfenv_then_table_then_restore():
    t = Tx(reads={0x0870: 0x07000760, 0x0860: 0x66F60110, 0x0824: 0x00390204})
    rf.phy_rf_config(t)
    addrs = [a for a, _ in t.w32]
    # RFENV setup touches 0x860 (x2) and 0x824 (x2) before any LSSI write...
    first_lssi = addrs.index(RF_LSSI_WRITE_A)
    assert set(addrs[:first_lssi]) == {0x0860, 0x0824}
    # ...the bulk are LSSI writes (91 radio-A rows on this card)...
    assert addrs.count(RF_LSSI_WRITE_A) == 91
    # ...and the final write restores RFENV on 0x870.
    assert addrs[-1] == 0x0870
