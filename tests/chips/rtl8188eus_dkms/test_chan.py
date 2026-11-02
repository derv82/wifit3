"""Hardware-free regression for the RTL8188EUS (DKMS) channel tune.

Locks the stateful RfRegChnlVal RMW (channel field then 20 MHz BW field) and the two
RF_CHNLBW writes. Full byte-for-byte replay of the initial tune lives in
``scripts/chips/rtl8188eus_dkms/verify_channels.py``.
"""
import pytest

from wifit3.chips.rtl8188eus_dkms import chan, dig
from wifit3.chips.rtl8188eus_dkms.efuse import TxPwr2G

WIRE_TXPWR = TxPwr2G(
    cck_base=(0x30, 0x30, 0x2F, 0x2E, 0x2E, 0x2E),
    bw40_base=(0x33, 0x33, 0x33, 0x32, 0x31),
    cck_diff=0, ofdm_diff=1, bw20_diff=0,
)


class RegTx:
    """Stateful register fake; all reads start at 0 (txagc RMWs harmless)."""
    def __init__(self):
        self.regs = {}
        self.w8, self.w16, self.w32 = [], [], []

    def read8(self, a):
        return self.regs.get(a, 0) & 0xFF

    def read16(self, a):
        return self.regs.get(a, 0) & 0xFFFF

    def read32(self, a):
        return self.regs.get(a, 0)

    def write8(self, a, v):
        self.regs[a] = v & 0xFF
        self.w8.append((a, v & 0xFF))

    def write16(self, a, v):
        self.regs[a] = v & 0xFFFF
        self.w16.append((a, v & 0xFFFF))

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w32.append((a, v))


def _rf18_writes(t):
    """The RF_CHNLBW (RF reg 0x18) values written via the path-A LSSI register 0x840."""
    return [v & 0xFFFFF for a, v in t.w32 if a == 0x0840 and ((v >> 20) & 0xFF) == 0x18]


def test_set_channel_ch1():
    t = RegTx()
    new = chan.set_channel(t, WIRE_TXPWR, 0x07407, 1, False, 0, 0, dig.WatchdogState(cur_ig_value=0x20))
    assert new == 0x07C01                       # ch1 in [9:0], 20 MHz (BIT10|BIT11)
    assert _rf18_writes(t) == [0x07401, 0x07C01]   # channel write, then BW write
    assert (0x0603, 0x04) in t.w8               # BWOPMODE |= BW_OPMODE_20MHZ


def test_set_channel_preserves_upper_bits_and_steps_channel():
    # A second tune from the BW-set state: channel field replaced, BW field re-asserted.
    t = RegTx()
    new = chan.set_channel(t, WIRE_TXPWR, 0x07C01, 6, False, 0, 0, dig.WatchdogState(cur_ig_value=0x20))
    assert (new & 0x3FF) == 6 and (new >> 10) & 0x3 == 0x3   # ch6, 20 MHz
    assert _rf18_writes(t)[0] & 0x3FF == 6                   # channel write carries ch6


def test_spur_calibration_clears_receiver_blocking_on_icut_ch13():
    st = dig.WatchdogState(cur_ig_value=0x20, is_receiver_blocking_en=True)
    chan.set_channel(RegTx(), WIRE_TXPWR, 0x07407, 13, True, 0, 0, st)
    assert st.is_receiver_blocking_en is False


def test_spur_calibration_keeps_receiver_blocking_off_ch13_14():
    st = dig.WatchdogState(cur_ig_value=0x20, is_receiver_blocking_en=True)
    chan.set_channel(RegTx(), WIRE_TXPWR, 0x07407, 1, True, 0, 0, st)
    assert st.is_receiver_blocking_en is True
    chan.set_channel(RegTx(), WIRE_TXPWR, 0x07407, 13, False, 0, 0, st)   # not I-cut
    assert st.is_receiver_blocking_en is True


def test_channels_2g():
    assert chan.CHANNELS_2G == list(range(1, 15))


class OpLog:
    """Register fake that logs every read and write in order over a seeded register map."""
    def __init__(self, regs):
        self.regs = dict(regs)
        self.ops = []

    def read8(self, a):
        self.ops.append(("r8", a))
        return self.regs.get(a, 0) & 0xFF

    def read32(self, a):
        self.ops.append(("r32", a))
        return self.regs.get(a, 0)

    def write8(self, a, v):
        self.regs[a] = v & 0xFF
        self.ops.append(("w8", a, v & 0xFF))

    def write32(self, a, v):
        self.regs[a] = v & 0xFFFFFFFF
        self.ops.append(("w32", a, v & 0xFFFFFFFF))


def _c_phy_set_bb_reg(regs, ops, addr, mask, data):
    """PHY_SetBBReg8188E [SRC] rtl8188e_phycfg.c:398: read32 + masked write32."""
    orig = regs.get(addr, 0)
    shift = (mask & -mask).bit_length() - 1
    new = (orig & ~mask) | ((data << shift) & mask)
    regs[addr] = new
    ops += [("r32", addr), ("w32", addr, new)]


def _c_rf_chnlbw_write(ops, val):
    """phy_set_rf_reg(RF_PATH_A, RF_CHNLBW=0x18, bRFRegOffsetMask) -> phy_RFSerialWrite
    [SRC] rtl8188e_phycfg.c:600 onto rFPGA0_XA_LSSIParameter (0x840)."""
    ops.append(("w32", 0x840, ((0x18 << 20) | (val & 0xFFFFF)) & 0x0FFFFFFF))


def _c_spur_calibration_ch14(regs, ops):
    """phy_SpurCalibration_8188E 20 MHz ch13/14 arm [SRC] rtl8188e_phycfg.c:1499."""
    _c_phy_set_bb_reg(regs, ops, 0xC40, 1 << 9, 0x1)                    # rOFDM0_RxDSP
    _c_phy_set_bb_reg(regs, ops, 0xD3C, (1 << 8) | (1 << 7) | (1 << 6), 0x2)   # rOFDM1_IntfDet
    _c_phy_set_bb_reg(regs, ops, 0xC40, 0x1F << 24, 0x1F)


def _expected_ch14_ops(seed, tx_pwr, rf_chnl_val, i_cut, remnant_cck, remnant_ofdm):
    """PHY_SetSwChnlBWMode8188E(14, CHANNEL_WIDTH_20) [SRC] rtl8188e_phycfg.c:1857."""
    regs, ops = dict(seed), []
    # rtw_get_ch_group(14) [SRC] rtw_rf.c:368/373: group 4, cck_group 5.
    cck = tx_pwr.cck_base[5] + tx_pwr.cck_diff + remnant_cck
    ofdm = tx_pwr.bw40_base[4] + tx_pwr.ofdm_diff + remnant_ofdm
    ht = tx_pwr.bw40_base[4] + tx_pwr.bw20_diff + remnant_ofdm
    # PHY_SetTxPowerIndex_8188E path A [SRC] rtl8188e_phycfg.c:1186, rates in
    # mgn_rates_cck / _ofdm / _mcs0_7 order; MGN_2M takes tx_power_extra_bias -9.
    for addr, mask, idx in (
        (0xE08, 0x0000FF00, cck), (0x86C, 0x0000FF00, cck - 9),
        (0x86C, 0x00FF0000, cck), (0x86C, 0xFF000000, cck),
        *((0xE00, 0xFF << (8 * b), ofdm) for b in range(4)),
        *((0xE04, 0xFF << (8 * b), ofdm) for b in range(4)),
        *((0xE10, 0xFF << (8 * b), ht) for b in range(4)),
        *((0xE14, 0xFF << (8 * b), ht) for b in range(4)),
    ):
        _c_phy_set_bb_reg(regs, ops, addr, mask, max(0, min(0x3F, idx)))
    rf_chnl_val = (rf_chnl_val & 0xFFFFFC00) | 14                       # _PHY_SwChnl8188E s2
    _c_rf_chnlbw_write(ops, rf_chnl_val)
    if i_cut:
        _c_spur_calibration_ch14(regs, ops)
    # _PHY_SetBWMode88E 20 MHz [SRC] rtl8188e_phycfg.c:1565.
    bwop = regs.get(0x603, 0)
    ops += [("r8", 0x603), ("r8", 0x442), ("w8", 0x603, bwop | 0x04)]
    _c_phy_set_bb_reg(regs, ops, 0x800, 0x1, 0)
    _c_phy_set_bb_reg(regs, ops, 0x900, 0x1, 0)
    rf_chnl_val = (rf_chnl_val & 0xFFFFF3FF) | (1 << 10) | (1 << 11)   # rtl8188e_rf6052.c:144
    _c_rf_chnlbw_write(ops, rf_chnl_val)
    if i_cut:
        _c_spur_calibration_ch14(regs, ops)
    return ops, rf_chnl_val


@pytest.mark.parametrize("i_cut", [True, False])
def test_set_channel_ch14_matches_vendor_sequence(i_cut):
    seed = {0x603: 0x10, 0x800: 0x03, 0x900: 0x01, 0xC40: 0x0A0001E0, 0xD3C: 0x000001C0,
            0xE00: 0x01020304, 0xE04: 0x05060708, 0xE08: 0x11223344, 0x86C: 0x55667788,
            0xE10: 0x090A0B0C, 0xE14: 0x0D0E0F10}
    tx_pwr = TxPwr2G(cck_base=(0x20, 0x21, 0x22, 0x23, 0x24, 0x2A),
                     bw40_base=(0x30, 0x31, 0x32, 0x33, 0x34),
                     cck_diff=0, ofdm_diff=1, bw20_diff=-1)
    t = OpLog(seed)
    st = dig.WatchdogState(cur_ig_value=0x20, is_receiver_blocking_en=True)
    new = chan.set_channel(t, tx_pwr, 0x07C01, 14, i_cut, 1, -2, st)
    expected_ops, expected_rf = _expected_ch14_ops(seed, tx_pwr, 0x07C01, i_cut, 1, -2)
    assert t.ops == expected_ops
    assert new == expected_rf == 0x07C0E
    assert st.is_receiver_blocking_en is (not i_cut)
