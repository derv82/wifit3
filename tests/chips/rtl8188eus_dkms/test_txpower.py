"""Hardware-free regression for the RTL8188EUS (DKMS) TX-power level (MISC11).

Locks the per-rate index math (base + extra_bias, clamped) and the path-A rate->
register/byte map against the cold-boot wire (capture-1 ops 1589-1628, init channel 6).
Full byte-for-byte replay lives in ``scripts/rtl8188eus_dkms/verify_pcap.py``.
"""
from wifit3.chips.rtl8188eus_dkms import txpower
from wifit3.chips.rtl8188eus_dkms.efuse import TxPwr2G


class RegTx:
    """Stateful fake: write32 updates the register so a later RMW reads it back."""
    def __init__(self, init=None):
        self.regs = dict(init or {})
        self.w32 = []

    def read32(self, a):
        return self.regs.get(a, 0x2D2D2D2D)   # cold-boot txagc tables seed all 0x2d

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w32.append((a, v))


# The path-A PG decode from capture-1's efuse (cck/bw40 base + 1TX diffs).
WIRE_TXPWR = TxPwr2G(
    cck_base=(0x30, 0x30, 0x2F, 0x2E, 0x2E, 0x2E),
    bw40_base=(0x33, 0x33, 0x33, 0x32, 0x31),
    cck_diff=0, ofdm_diff=1, bw20_diff=0,
)


def test_ch_group_2g():
    assert txpower.ch_group_2g(1) == (0, 0)
    assert txpower.ch_group_2g(6) == (2, 2)     # init channel
    assert txpower.ch_group_2g(11) == (3, 3)
    assert txpower.ch_group_2g(13) == (4, 4)
    assert txpower.ch_group_2g(14) == (4, 5)    # ch14 has its own cck group


def test_power_index_ch6_matches_wire():
    # ch6 -> group 2. CCK base=0x2f, BW40 base=0x33.
    gp, cck_gp = txpower.ch_group_2g(6)
    p = WIRE_TXPWR
    # 1M (CCK, no bias) = 0x2f; 2M (CCK, -9) = 0x26.
    assert txpower._power_index(p, gp, cck_gp, txpower._CCK, 0) == 0x2F
    assert txpower._power_index(p, gp, cck_gp, txpower._CCK, -9) == 0x26
    # OFDM = 0x33 + ofdm_diff(1) = 0x34; HT = 0x33 + bw20_diff(0) = 0x33.
    assert txpower._power_index(p, gp, cck_gp, txpower._OFDM, 0) == 0x34
    assert txpower._power_index(p, gp, cck_gp, txpower._HT, 0) == 0x33


def test_power_index_clamps_to_max():
    p = TxPwr2G((0x3F,) * 6, (0x3F,) * 5, 0, 4, 0)
    gp, cck_gp = txpower.ch_group_2g(6)
    # base 0x3f + ofdm_diff 4 = 0x43 -> clamps to MAX_POWER_INDEX 0x3f.
    assert txpower._power_index(p, gp, cck_gp, txpower._OFDM, 0) == 0x3F


def test_set_tx_power_final_register_values():
    t = RegTx()
    txpower.set_tx_power(t, WIRE_TXPWR, 6)
    r = t.regs
    assert r[0x0E08] == 0x2D2D2F2D                 # CCK 1M in byte1
    assert r[0x086C] == 0x2F2F262D                 # CCK 2M(0x26)/5.5M/11M bytes 1/2/3
    assert r[0x0E00] == 0x34343434 and r[0x0E04] == 0x34343434   # OFDM
    assert r[0x0E10] == 0x33333333 and r[0x0E14] == 0x33333333   # HT MCS0-7
    assert len(t.w32) == 20                        # 4 CCK + 8 OFDM + 8 HT rates
