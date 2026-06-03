"""Hardware-free regression for the M2d channel tune.

The full byte-for-byte check vs the cold-boot capture is
`scripts/rtl8814au_dkms/verify_pcap.py`; this pins the channel/bw register math.
"""
import pytest

from wifit3.chips.rtl8814au_dkms import chan


class Rec:
    """Records ops; serves canned reads (for the RF read-modify-writes)."""

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


def test_config_bb_enables_ofdm_cck():
    rec = Rec(reads={0x808: 0x0E0282FF})
    chan.phy_config_bb(rec)
    # rOFDMCCKEN[29:28] = 3 -> 0x0e0282ff | 0x30000000.
    assert ("W32", 0x808, 0x3E0282FF) in rec.ops


def test_channel_rf_write_merges_channel_number():
    # phy_set_rf_reg(path A, 0x18, mask=0x703ff, value=channel) over the RF read.
    rec = Rec(reads={0x2860: 0x00013124})  # path-A RF reg 0x18 readback
    chan.set_rf_masked(rec, "a", chan.C.RF_CHNLBW, chan.C.RF_CHNLBW_CH_MASK, 1)
    # (0x13124 & ~0x703ff) | 0x1 = 0x3001 -> LSSI 0xc90 <- (0x18<<20)|0x3001.
    assert ("W32", 0x0C90, 0x01803001) in rec.ops


def test_bw20_rf_write_sets_bits_11_10():
    rec = Rec(reads={0x2860: 0x00003001})
    chan.set_rf_masked(rec, "a", chan.C.RF_CHNLBW, chan.C.RF_CHNLBW_BW_MASK, 0x3)
    # (0x3001 & ~0xc00) | (3<<10) = 0x3c01.
    assert ("W32", 0x0C90, 0x01803C01) in rec.ops


def test_set_channel_bw_cck_dfir_arms():
    # channels 1-11 vs 12-13 use different CCK TX-filter values.
    rec = Rec()
    chan._phy_sw_chnl(rec, 6)
    assert ("W32", 0x0A24, 0x090E1317) in rec.ops   # ch<=11 arm
    rec = Rec()
    chan._phy_sw_chnl(rec, 12)
    assert ("W32", 0x0A24, 0x090E1217) in rec.ops   # ch 12-13 arm


def test_set_channel_bw_rejects_5g():
    with pytest.raises(NotImplementedError):
        chan.set_channel_bw(Rec(), 36, ())


def test_spur_cal_resets_nbi_csi():
    rec = Rec(reads={0x87C: 0x000FC000})
    chan._spur_cal_reset(rec)
    addrs = [o[1] for o in rec.ops if o[0] == "W32"]
    for csi in (0x880, 0x884, 0x898, 0x89C):
        assert csi in addrs
    # NBI disabled: 0x87c[13] cleared (already 0 here -> unchanged).
    assert ("W32", 0x87C, 0x000FC000) in rec.ops


def test_set_rfe_reg_init_rfe1():
    # PHY_SetRFEReg8814A(TRUE): 0x1994[3:0]=0xf (0x77->0x7f), GPIO 0x42 |= 0xf0.
    rec = Rec(reads={0x1994: 0x77, 0x42: 0x00})
    chan.set_rfe_reg_init(rec, 1)
    assert ("W32", 0x1994, 0x7F) in rec.ops
    assert ("W8", 0x42, 0xF0) in rec.ops


def test_set_rfe_reg_init_rfe0_uses_c0():
    # rfe 0 drives the GPIO antenna pins [23:22]=0b11 -> |= 0xc0, not 0xf0.
    rec = Rec(reads={0x1994: 0x77, 0x42: 0x00})
    chan.set_rfe_reg_init(rec, 0)
    assert ("W8", 0x42, 0xC0) in rec.ops
