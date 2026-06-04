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


_SW = (0x200, 0x200, 0x200, 0x200)   # 0 dB per-path BB-swing, both bands


def test_set_channel_bw_cck_dfir_arms():
    # channels 1-11 vs 12-13 use different CCK TX-filter values.
    rec = Rec()
    chan._phy_sw_chnl(rec, 6, _SW, _SW)
    assert ("W32", 0x0A24, 0x090E1317) in rec.ops   # ch<=11 arm
    rec = Rec()
    chan._phy_sw_chnl(rec, 12, _SW, _SW)
    assert ("W32", 0x0A24, 0x090E1217) in rec.ops   # ch 12-13 arm


def test_set_channel_bw_rejects_5g():
    # 5 GHz channel *select* (fc-area / RF sub-band) is M5b; set_channel_bw still rejects.
    with pytest.raises(NotImplementedError):
        chan.set_channel_bw(Rec(), 36, (), _SW, _SW)


def test_switch_wireless_band_5g_sequence():
    # 5G branch: CCK_CHECK bit7 marker + 0xa80[18] come FIRST (before RFE), CCK left off
    # (rOFDMCCKEN=2 OFDM-only), 0x958 AGC select deferred to the channel switch (M5b).
    rec = Rec(reads={0x1002: 0x01})
    chan.switch_wireless_band_5g(rec, _SW)
    w = rec.ops
    assert ("W8", 0x0454, 0x80) in w                 # CCK_CHECK bit7 = 5G marker
    assert ("W32", 0x0A80, 1 << 18) in w             # 0xa80[18]=1 (CCK Tx enable)
    # RFE pinmux: A/B/C = 0x33173317, D differs (0x77177717); inv 0x1abc[27:20]=0x33.
    assert ("W32", 0x0CB0, 0x33173317) in w
    assert ("W32", 0x0EB0, 0x33173317) in w
    assert ("W32", 0x18B4, 0x33173317) in w
    assert ("W32", 0x1AB4, 0x77177717) in w
    assert ("W32", 0x1ABC, 0x03300000) in w          # [27:20]=0x33 over read 0
    # 5G scalars: rTxPath[7:4]=0, rCCK_RX[27:24]=0xF, rOFDMCCKEN[29:28]=2 (OFDM only).
    assert ("W32", 0x080C, 0x0) in w
    assert ("W32", 0x0A04, 0x0F000000) in w
    assert ("W32", 0x0808, 0x20000000) in w
    # the 0x958 AGC-table select is NOT written here (postponed to the M5b channel switch).
    assert not any(o[0] == "W32" and o[1] == 0x0958 for o in w)
    # clock gated off (0x1002 read 0x01 -> &~1 = 0) then back on (-> |1 = 1).
    assert ("W8", 0x1002, 0x00) in w
    assert ("W8", 0x1002, 0x01) in w


def test_phy_sw_band_no_switch_same_band():
    # current 2.4G (0x454 bit7=0) + target 2.4G (ch6) -> only the band-marker read.
    rec = Rec(reads={0x454: 0x00})
    chan.phy_sw_band(rec, 6, _SW, _SW)
    assert rec.ops == [("R8", 0x0454)]
    # current 5G (bit7=1) + target 5G (ch36) -> only the read, no switch.
    rec = Rec(reads={0x454: 0x80})
    chan.phy_sw_band(rec, 36, _SW, _SW)
    assert rec.ops == [("R8", 0x0454)]


def test_phy_sw_band_switches_on_crossing():
    # 2.4G -> 5G (ch36): writes the 5G marker + 5G RFE pinmux.
    rec = Rec(reads={0x454: 0x00})
    chan.phy_sw_band(rec, 36, _SW, _SW)
    assert ("W8", 0x0454, 0x80) in rec.ops
    assert ("W32", 0x0CB0, 0x33173317) in rec.ops
    # 5G -> 2.4G (ch1, the 165->1 wrap): clears the marker + 2.4G RFE pinmux.
    rec = Rec(reads={0x454: 0x80})
    chan.phy_sw_band(rec, 1, _SW, _SW)
    assert ("W8", 0x0454, 0x00) in rec.ops
    assert ("W32", 0x0CB0, 0x77777777) in rec.ops


def test_nbi_reg_idx_matches_wire():
    # The 2.4G spur notch tap (f_intf=2440), verified byte-for-byte vs the cold-boot wire.
    assert chan._nbi_reg_idx(4, 2440) == 19
    assert chan._nbi_reg_idx(6, 2440) == 4
    assert chan._nbi_reg_idx(8, 2440) == 9


def test_spur_nbi_2g_off_spur_resets_csi_and_disables():
    # A non-spur channel (ch1): reset NBI tap + CSI fix masks, NBI disabled (bit13=0),
    # and NO notch-tap write to 0x87c[19:14].
    rec = Rec(reads={0x87C: 0x000FC000})
    chan._spur_nbi_2g(rec, 1)
    addrs = [o[1] for o in rec.ops if o[0] == "W32"]
    for csi in (0x880, 0x884, 0x898, 0x89C):
        assert csi in addrs
    # disable writes bit13=0 last; the read returned 0xfc000 so the value stays 0xfc000.
    assert ("W32", 0x87C, 0x000FC000) in rec.ops


def test_spur_nbi_2g_on_spur_notches_and_enables():
    # ch6 (spur 2440): set the notch tap (reg_idx 4 -> 0x87c[19:14]) then enable NBI.
    rec = Rec(reads={0x87C: 0x000FC000})
    chan._spur_nbi_2g(rec, 6)
    nbi_writes = [o for o in rec.ops if o[0] == "W32" and o[1] == 0x87C]
    # last two 0x87c writes: notch tap (reg_idx 4 << 14 = 0x10000) then enable (bit13).
    assert nbi_writes[-2][2] == 0x00010000           # [19:14] = 4, bit13 still 0
    assert nbi_writes[-1][2] & (1 << 13)             # NBI enabled
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
