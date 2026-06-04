"""Hardware-free regression for the M-TXPWR per-rate power computation.

Pins the PG power-index formula (base + 1TX diff, by-rate/limit disabled), the 2.4 GHz
channel-group mapping, and the full txagc register sweep against the cold-boot wire —
ch1 gives CCK 0x31 / OFDM 0x2d / HT-VHT 0x2b and the 0xc54 training word 0x131921.
"""
from wifit3.chips.rtl8821au_dkms import txpower
from wifit3.chips.rtl8821au_dkms.efuse import PathTxPwr

# Decoded path-A PG block from the cold-boot efuse (verify_efuse_pcap). Plain power
# indices / signed diffs — no card identity.
_PP = PathTxPwr(
    cck_base=(0x31, 0x31, 0x31, 0x30, 0x30, 0x30),
    bw40_base=(0x2B, 0x2D, 0x2E, 0x2E, 0x2E),
    cck_diff=(0, -1, -1),
    ofdm_diff=(2, -1, -1),
    bw20_diff=(0, -1, -1),
)


class FakeT:
    """Register-backed transport so set_bb's read-modify-write byte pokes accumulate."""
    def __init__(self):
        self.regs = {}

    def read32(self, a):
        return self.regs.get(a, 0)

    def write32(self, a, v):
        self.regs[a] = v & 0xFFFFFFFF


def test_ch_group_2g():
    assert txpower._ch_group_2g(1) == (0, 0)
    assert txpower._ch_group_2g(6) == (2, 2)
    assert txpower._ch_group_2g(11) == (3, 3)
    assert txpower._ch_group_2g(14) == (4, 5)   # ch14 takes the dedicated CCK group


def test_pg_idx_ch1_matches_wire():
    assert txpower._pg_idx(_PP, "cck", 0, 0) == 0x31
    assert txpower._pg_idx(_PP, "ofdm", 0, 0) == 0x2D
    assert txpower._pg_idx(_PP, "bw20", 0, 0) == 0x2B


def test_pg_idx_clamps():
    hot = PathTxPwr((0xFF,) * 6, (0xFF,) * 5, (0, 0, 0), (8, 0, 0), (0, 0, 0))
    assert txpower._pg_idx(hot, "ofdm", 0, 0) == 63   # 0xFF + 8 clamped to txgi_max


def test_set_tx_power_writes_wire_values():
    t = FakeT()
    txpower.set_tx_power(t, 1, _PP)
    assert t.regs[0x0C20] == 0x31313131            # CCK
    assert t.regs[0x0C24] == 0x2D2D2D2D            # OFDM 6-18
    assert t.regs[0x0C28] == 0x2D2D2D2D            # OFDM 24-54
    assert t.regs[0x0C2C] == 0x2B2B2B2B            # HT MCS0-3
    assert t.regs[0x0C40] == 0x2B2B2B2B            # VHT1SS MCS4-7
    assert t.regs[0x0C44] & 0xFFFF == 0x2B2B       # only VHT1SS MCS8-9 (low 2 bytes)
    assert t.regs[0x0C54] == 0x00131921            # MCS7(0x2b) -10/-8/-6 training word
