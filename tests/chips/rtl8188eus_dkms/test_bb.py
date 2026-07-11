"""Hardware-free regression for the RTL8188EUS (DKMS) BB config + crystal cap.

Full byte-for-byte replay lives in ``scripts/rtl8188eus_dkms/verify_pcap.py``;
this locks the prologue, the crystal-cap mask math, and that PHY_REG/AGC rows are
full-32-bit writes.
"""
from wifit3.chips.rtl8188eus_dkms import bb
from wifit3.chips.rtl8188eus_dkms.constants import REG_AFE_XTAL_CTRL
from wifit3.chips.rtl8188eus_dkms.efuse import BoardOptions


class Tx:
    def __init__(self, reads=None):
        self.w8, self.w16, self.w32 = [], [], []
        self._reads = dict(reads or {})

    def read16(self, a):
        return self._reads.get((a, 2), 0x0000)

    def read32(self, a):
        return self._reads.get((a, 4), 0x00000000)

    def write8(self, a, v):
        self.w8.append((a, v & 0xFF))

    def write16(self, a, v):
        self.w16.append((a, v & 0xFFFF))

    def write32(self, a, v):
        self.w32.append((a, v & 0xFFFFFFFF))


def test_crystal_cap_mask_math():
    # cap=0x20 -> field = 0x20 | (0x20<<6) = 0x820 at bits [22:11] = 0x410000.
    t = Tx(reads={(REG_AFE_XTAL_CTRL, 4): 0x350007FF})
    bb.set_crystal_cap(t, 0x20)
    assert t.w32 == [(REG_AFE_XTAL_CTRL, 0x350007FF & ~0x007FF800 | 0x410000)]


def test_bb_prologue_and_tables():
    # SYS_FUNC_EN read returns 0xfc1c (as on the wire) -> | 0x2003 = 0xfc1f.
    t = Tx(reads={(0x0002, 2): 0xFC1C, (REG_AFE_XTAL_CTRL, 4): 0x350007FF})
    bb.phy_bb_config(t, crystal_cap=0x20)
    assert t.w16[0] == (0x0002, 0xFC1F)
    assert (0x001F, 0x07) in t.w8 and (0x0002, 0x17) in t.w8
    # First PHY_REG row 0x800,0x80040000 is a full write32; AGC rows are write32 too.
    assert t.w32[0] == (0x0800, 0x80040000)
    assert len(t.w32) == 323        # PHY_REG + AGC_TAB taken rows + crystal cap
    assert t.w32[-1][0] == REG_AFE_XTAL_CTRL   # crystal cap is the last BB write


class RegTx:
    """Stateful fake: write32 updates the register so a later RMW reads it back."""
    def __init__(self, init):
        self.regs = dict(init)
        self.w32 = []

    def read32(self, a):
        return self.regs.get(a, 0)

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w32.append((a, v))


def test_bb_turn_on_block():
    # cap1 op 1584-1587: enable CCK (BIT24) then OFDM (BIT25) in 0x800, each a RMW.
    t = RegTx({0x0800: 0x80040000})
    bb.bb_turn_on_block(t)
    assert t.w32 == [(0x0800, 0x81040000), (0x0800, 0x83040000)]


def test_phy_set_rfe_reg_internal_is_noop():
    # Reference card (internal PA+LNA): PHY_SetRFEReg_8188E early-returns, no wire ops.
    t = RegTx({})
    bb.phy_set_rfe_reg(t, BoardOptions(external_pa_2g=False, external_lna_2g=False,
                                       type_glna=0x0))
    assert t.w32 == []


def test_phy_set_rfe_reg_external_writes_three():
    # External PA or LNA: 0x40[3:2]=0x3, 0xEE8[28]=1, 0x87C[0]=0 (RMW each).
    t = RegTx({0x40: 0x0, 0xEE8: 0x0, 0x87C: 0x1})
    bb.phy_set_rfe_reg(t, BoardOptions(external_pa_2g=False, external_lna_2g=True,
                                       type_glna=0x1))
    assert t.w32 == [(0x40, 0x0000000C), (0xEE8, 0x10000000), (0x87C, 0x00000000)]
