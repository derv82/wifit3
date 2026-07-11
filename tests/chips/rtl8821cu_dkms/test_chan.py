"""Hardware-free regression for the cut-gated channel-tune branches.

The full byte-for-byte check vs the cold-boot capture is
`scripts/rtl8821cu_dkms/verify_pcap.py` (the pcap card is cut 4). This pins the A-cut
(ODM_CUT_A) RF 0xb8 LCK-fix that a non-reference silicon revision would take, and confirms
the reference cut leaves RF 0xb8 untouched (byte-identical).
"""
import pytest

from wifit3.chips.rtl8821cu_dkms import chan

_RF18_RD = 0x2860        # read_rf(0x18) = 0x2800 + (0x18<<2)
_RFB8_RD = 0x2AE0        # read_rf(0xb8) = 0x2800 + (0xb8<<2)
_LSSI = 0x0C90           # write_rf LSSI port
_B8_WR_PREFIX = 0xB8 << 20   # write_rf(0xb8, data) = prefix | (data & 0xfffff)


class Rec:
    """Records ops; serves canned reads (for the RF/BB read-modify-writes)."""

    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}
        self.rega24 = self.rega28 = self.regaac = 0

    def read8(self, a):
        self.ops.append(("R8", a))
        return self.reads.get(a, 0)

    def read16(self, a):
        self.ops.append(("R16", a))
        return self.reads.get(a, 0)

    def read32(self, a):
        self.ops.append(("R32", a))
        return self.reads.get(a, 0)

    def write8(self, a, v):
        self.ops.append(("W8", a, v))

    def write16(self, a, v):
        self.ops.append(("W16", a, v))

    def write32(self, a, v):
        self.ops.append(("W32", a, v))


@pytest.fixture(autouse=True)
def _no_trx_stop(monkeypatch):
    # phydm_stop_ic_trx polls the BB dbg-port; irrelevant to the 0xb8 gating, stub it out.
    monkeypatch.setattr(chan.dm, "stop_ic_trx", lambda *a, **k: None)


def _b8_writes(rec):
    return [o for o in rec.ops if o[0] == "W32" and o[1] == _LSSI
            and (o[2] & 0x0FF00000) == _B8_WR_PREFIX]


# --- 2.4 GHz arm -----------------------------------------------------------------

def test_switch_channel_2g_reference_cut_leaves_rf_b8_untouched():
    rec = Rec(reads={_RF18_RD: 0x00013124})
    chan._switch_channel(rec, 1, cut=4)               # pcap card is cut 4
    assert ("R32", _RFB8_RD) not in rec.ops           # no A-cut RF 0xb8 read
    assert _b8_writes(rec) == []                       # no A-cut RF 0xb8 write


def test_switch_channel_2g_a_cut_sets_rf_b8_bit19():
    rec = Rec(reads={_RF18_RD: 0x00013124, _RFB8_RD: 0x00000})
    chan._switch_channel(rec, 1, cut=chan._ODM_CUT_A)
    assert ("R32", _RFB8_RD) in rec.ops
    # rf_b8 = 0 | BIT19 = 0x80000 -> LSSI (0xb8<<20)|0x80000.
    assert _b8_writes(rec) == [("W32", _LSSI, _B8_WR_PREFIX | 0x80000)]


# --- 5 GHz arm -------------------------------------------------------------------

def test_switch_channel_5g_reference_cut_leaves_rf_b8_untouched():
    rec = Rec(reads={_RF18_RD: 0x00013124})
    chan._switch_channel_5g(rec, 36, cut=4)
    assert ("R32", _RFB8_RD) not in rec.ops
    assert _b8_writes(rec) == []


def test_switch_channel_5g_a_cut_sets_bit19_outside_lck_band():
    # ch 36 is outside 5285-5375 MHz (ch 57-75) -> rf_b8 |= BIT19.
    rec = Rec(reads={_RF18_RD: 0x00013124, _RFB8_RD: 0x00000})
    chan._switch_channel_5g(rec, 36, cut=chan._ODM_CUT_A)
    assert _b8_writes(rec) == [("W32", _LSSI, _B8_WR_PREFIX | 0x80000)]


def test_switch_channel_5g_a_cut_clears_bit19_in_lck_band():
    # ch 60 is inside 5285-5375 MHz (ch 57-75) -> rf_b8 &= ~BIT19 (from a set-bit19 seed).
    rec = Rec(reads={_RF18_RD: 0x00013124, _RFB8_RD: 0x80000})
    chan._switch_channel_5g(rec, 60, cut=chan._ODM_CUT_A)
    assert _b8_writes(rec) == [("W32", _LSSI, _B8_WR_PREFIX | 0x0)]
