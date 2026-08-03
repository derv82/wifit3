"""Hardware-free regression for the rfe_type / cut branches generalized for any-card support.

The captured ALFA AWUS036ACH (rfe_type=3, C-cut) is byte-diffed by
`scripts/chips/rtl8812au_dkms/verify_pcap.py`; these pin the OTHER runtime-EFUSE / cut branches
that only a non-reference 8812AU card walks (they have no capture to diff against).
"""
from wifit3.chips.rtl8812au_dkms import chan


class Rec:
    """Records ops; serves canned reads (for the masked read-modify-writes)."""

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


# --- phy_SetRFEReg8812 (2.4 GHz) rfe_type switch ------------------------------------------

def test_set_rfe_2g_type3_reference_is_antsel_pinmux():
    # The captured card: rfe_type 3 -> 0x54337770 pinmux both paths + r_ANTSEL_SW.
    rec = Rec()
    chan._set_rfe_2g(rec, 3)
    assert ("W32", 0x0CB0, 0x54337770) in rec.ops
    assert ("W32", 0x0EB0, 0x54337770) in rec.ops
    assert ("W32", 0x0900, 0x1) in rec.ops                 # r_ANTSEL_SW[1:0] = 1


def test_set_rfe_2g_type5_partial_writes():
    # rfe_type 5 was previously missing (fell through the 0/1/2/4 branch); it must emit the
    # vendor case-5 shape: a path-A pinmux BYTE write + a path-A inv-byte RMW clearing BIT0.
    rec = Rec(reads={0x0CB7: 0x55})                        # rA_RFE_Inv+3 current byte
    chan._set_rfe_2g(rec, 5)
    assert ("W8", 0x0CB2, 0x77) in rec.ops                 # rA_RFE_Pinmux+2 (partial byte)
    assert ("W32", 0x0EB0, 0x77777777) in rec.ops          # rB_RFE_Pinmux full dword
    assert ("W8", 0x0CB7, 0x54) in rec.ops                 # rA_RFE_Inv+3 &= ~BIT0 (0x55 -> 0x54)
    assert ("W32", 0x0EB4, 0x0) in rec.ops                 # rB_RFE_Inv = 0x000
    # case 5 must NOT do a full path-A pinmux dword write (that is the 0/1/2/3/4/6 shape).
    assert not any(o[:2] == ("W32", 0x0CB0) for o in rec.ops)


# --- phy_FixSpur_8812A cut branch ---------------------------------------------------------

def test_fix_spur_ccut_writes_adc_buf_clk():
    # C-cut (captured card) writes both 0x8AC (twice) and the 0x8C4 ADC-buf-clock reg.
    rec = Rec()
    chan._fix_spur(rec, 1, is_c_cut=True)
    assert ("W32", 0x08C4, 0x0) in rec.ops
    assert any(o[:2] == ("W32", 0x08AC) for o in rec.ops)


def test_fix_spur_non_ccut_2ghz_single_write():
    # Non-C-cut 8812a: ONLY the 2480 MHz 0x8AC[9:8] workaround, no 0x8AC[11:10], no 0x8C4[30].
    rec = Rec()
    chan._fix_spur(rec, 1, is_c_cut=False)
    writes = [o for o in rec.ops if o[0] == "W32"]
    assert writes == [("W32", 0x08AC, 0x200)]              # 0x8AC[9:8] = 2


def test_fix_spur_non_ccut_ch13():
    rec = Rec()
    chan._fix_spur(rec, 13, is_c_cut=False)
    writes = [o for o in rec.ops if o[0] == "W32"]
    assert writes == [("W32", 0x08AC, 0x300)]              # 0x8AC[9:8] = 3 (2480 MHz spur)


def test_fix_spur_non_ccut_5ghz_is_noop():
    # The non-C-cut branch only touches 2.4 GHz; a 5 GHz channel emits nothing.
    rec = Rec()
    chan._fix_spur(rec, 36, is_c_cut=False)
    assert rec.ops == []
