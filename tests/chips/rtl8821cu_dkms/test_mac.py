"""Hardware-free regression for the rfe-gated hal_init_misc branch.

The pcap card is rfe_type_expand 0x22, so `verify_pcap.py` never exercises the "1212 module"
(rfe_type 2) PAD_CTRL1 5G-RX fix. This pins that the fix fires only for an rfe-2 board and the
reference burn skips it byte-identically.
"""
from types import SimpleNamespace

from wifit3.chips.rtl8821cu_dkms import mac

_PAD_CTRL1_P3 = mac.REG_PAD_CTRL1 + 3    # 0x0067


class Rec:
    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}

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


def test_hal_init_misc_reference_rfe_skips_pad_ctrl1():
    rec = Rec()
    mac.hal_init_misc(rec, SimpleNamespace(rfe_type=0x22))    # pcap card
    assert not any(o[0] == "W8" and o[1] == _PAD_CTRL1_P3 for o in rec.ops)


def test_hal_init_misc_rfe2_writes_pad_ctrl1():
    rec = Rec()
    mac.hal_init_misc(rec, SimpleNamespace(rfe_type=2))       # "1212 module"
    assert ("W8", _PAD_CTRL1_P3, 0x36) in rec.ops
