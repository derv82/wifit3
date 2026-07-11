"""Hardware-free regression for the ExternalLNA_2G-gated 2.4 GHz RFE pinmux.

phy_SetRFEReg8821 turns off RF PA/LNA (0xCB0[15:12]/[7:4]=7) then either bypasses the
2.4 GHz external LNA (reference, ext_lna_2g=0 -> pinmux b'111) or turns it on
(ext_lna_2g=1 -> 0xCB4 BIT20 + pinmux b'010). Pins both branches to their register images.
"""
from wifit3.chips.rtl8821au_dkms import chan


class FakeT:
    """Register-backed transport so set_bb's read-modify-write pokes accumulate."""
    def __init__(self):
        self.regs = {}

    def read32(self, a):
        return self.regs.get(a, 0)

    def write32(self, a, v):
        self.regs[a] = v & 0xFFFFFFFF


def test_rfe_2g_bypass_reference():
    # ext_lna_2g False (AWUS036ACS): external LNA bypassed -> pinmux b'111, inv cleared.
    t = FakeT()
    chan._set_rfe_2g(t, ext_lna_2g=False)
    assert t.regs[0x0CB0] == 0x7777        # [15:12]=7 [7:4]=7 [10:8]=7 [2:0]=7
    assert t.regs[0x0CB4] == 0x00000000    # BIT20=0 BIT22=0


def test_rfe_2g_external_lna_on():
    # ext_lna_2g True: turn on 2.4 GHz external LNA -> pinmux b'010, inv BIT20 set.
    t = FakeT()
    chan._set_rfe_2g(t, ext_lna_2g=True)
    assert t.regs[0x0CB0] == 0x7272        # [15:12]=7 [7:4]=7 [10:8]=2 [2:0]=2
    assert t.regs[0x0CB4] == 0x00100000    # BIT20=1 BIT22=0


class BandT(FakeT):
    """FakeT + the byte/word pokes _switch_band_2g issues around the RFE writes."""
    def read8(self, a):
        return self.regs.get(a, 0) & 0xFF

    def write8(self, a, v):
        self.regs[a] = v & 0xFF

    def read16(self, a):
        return self.regs.get(a, 0) & 0xFFFF

    def write16(self, a, v):
        self.regs[a] = v & 0xFFFF


def test_switch_band_2g_rfe_images():
    # Full 2.4 GHz band switch: 0xCB4 carries _ext_band_switch's DPDT(0x77)+band-2.4G
    # (0x10000000), then the RFE inv bit. Matches the vendor's documented 0xCB4 images.
    ref, var = BandT(), BandT()
    chan._switch_band_2g(ref, chan.BB_SWING_DEFAULT, ext_lna_2g=False)
    chan._switch_band_2g(var, chan.BB_SWING_DEFAULT, ext_lna_2g=True)
    assert ref.regs[0x0CB0] == 0x7777 and ref.regs[0x0CB4] == 0x10000077   # bypass
    assert var.regs[0x0CB0] == 0x7272 and var.regs[0x0CB4] == 0x10100077   # ext LNA on
