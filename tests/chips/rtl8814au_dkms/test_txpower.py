"""Hardware-free regression for the M2e TX-power table.

The full byte-for-byte check vs the cold-boot capture is
`scripts/chips/rtl8814au_dkms/verify_pcap.py`; this pins the rate table, the pg/clamp
math, the efuse diff unpacking, and the 0x1998 write format.
"""
from wifit3.chips.rtl8814au_dkms import efuse, txpower


def test_rate_table_shape_and_order():
    t = txpower.RATE_TABLE
    assert len(t) == 66
    # Section order: CCK, OFDM, HT0-7, VHT1SS, HT8-15, VHT2SS, HT16-23, VHT3SS.
    assert t[0] == (0x00, "cck", "cck", 1)
    assert t[4] == (0x04, "bw40", "ofdm", 1)      # OFDM 6M
    assert t[12] == (0x0C, "bw40", "bw20", 1)     # HT MCS0
    assert t[20] == (0x2C, "bw40", "bw20", 1)     # VHT1SS MCS0 (interleaved before HT8)
    assert t[-1] == (0x49, "bw40", "bw20", 3)     # VHT3SS MCS9


def test_efuse_diff_unpacking():
    # Path A bytes from the capture: bases 0x20, diff [00,00,00,00,ee,ee,ee].
    m = bytearray(b"\xFF" * efuse.C.EFUSE_MAP_LEN)
    base = 0x10
    m[base:base + 6] = bytes([0x20] * 6)          # CCK base
    m[base + 6:base + 11] = bytes([0x20] * 5)     # BW40 base
    m[base + 11:base + 18] = bytes([0, 0, 0, 0, 0xEE, 0xEE, 0xEE])
    pp = efuse._parse_tx_power(bytes(m), 2)[0]
    assert pp.cck_base == (0x20,) * 6
    assert pp.bw40_base == (0x20,) * 5
    # The 0xee bytes encode -2 on the 3rd stream, but max_tx_cnt=2 zeros it (the loader
    # leaves the 3rd-stream diff unloaded); the 1st/2nd-stream diffs here are 0 anyway.
    assert pp.bw20_diff == (0, 0, 0)
    assert pp.ofdm_diff == (0, 0, 0)
    assert pp.cck_diff == (0, 0, 0)


def _path(cck, bw40):
    return efuse.PathTxPwr(cck_base=(cck,) * 6, bw40_base=(bw40,) * 5,
                           cck_diff=(0, 0, -2), ofdm_diff=(0, 0, -2),
                           bw20_diff=(0, 0, 0))


def test_power_index_matches_wire_pattern():
    # 5th arg is the PG group (0 = 2.4G ch1-2); cck_group defaults to it.
    a = _path(0x20, 0x20)
    assert txpower.power_index(a, "cck", "cck", 1, 0) == 0x22
    assert txpower.power_index(a, "bw40", "bw20", 3, 0) == 0x22   # 3SS, diffs net 0
    # Path B: CCK 0x27 -> 0x29; BW40 0x28 -> 0x2a (matches the captured PP bytes).
    b = _path(0x27, 0x28)
    assert txpower.power_index(b, "cck", "cck", 1, 0) == 0x29
    assert txpower.power_index(b, "bw40", "ofdm", 1, 0) == 0x2A


def test_set_tx_power_write_format_and_count():
    writes = []

    class Rec:
        def write32(self, a, v):
            writes.append((a, v))

    tx = tuple(_path(0x20, 0x20) for _ in range(4))
    txpower.set_tx_power(Rec(), 1, tx)
    assert len(writes) == 268                      # 67/path (66 + MGN_1M twice) x 4
    assert all(a == 0x1998 for a, _ in writes)
    # First write: path 0, CCK 1M, PP=0x22 -> 0x00801000 | 0 | 0 | 0x22<<24.
    assert writes[0] == (0x1998, 0x22801000)
    assert writes[1] == (0x1998, 0x22801000)       # MGN_1M written twice
    # byte1 carries 0x10|path; last path is D (0x13).
    assert ((writes[-1][1] >> 8) & 0xFF) == 0x13


def test_ch_group_5g_matches_vendor():
    # rtw_get_ch_group 5G ranges (the 14 UNII groups).
    assert txpower._ch_group_5g(36) == 0 and txpower._ch_group_5g(42) == 0
    assert txpower._ch_group_5g(44) == 1 and txpower._ch_group_5g(48) == 1
    assert txpower._ch_group_5g(64) == 3
    assert txpower._ch_group_5g(100) == 4
    assert txpower._ch_group_5g(149) == 10 and txpower._ch_group_5g(153) == 10
    assert txpower._ch_group_5g(165) == 12


def test_rate_table_5g_drops_cck():
    t5 = txpower.RATE_TABLE_5G
    assert len(t5) == 62                           # 66 - 4 CCK rows
    assert all(r[1] != "cck" for r in t5)
    assert t5[0] == (0x04, "bw40", "ofdm", 1)      # starts at OFDM 6M


def test_parse_tx_power_5g_unpacks_block():
    # 5G block follows the 18 B 2.4G block (path A at 0x10 -> 5G at 0x22).
    m = bytearray(b"\xFF" * efuse.C.EFUSE_MAP_LEN)
    b5 = 0x10 + 18
    m[b5:b5 + 14] = bytes(range(0x30, 0x3E))        # 14 BW40 group bases
    # diff bytes: 14=BW20[1T]/OFDM[1T], 15=BW40[2T]/BW20[2T], 16=BW40[3T]/BW20[3T],
    #             18=OFDM[2T]/OFDM[3T]
    m[b5 + 14] = 0x21        # BW20[1T]=2, OFDM[1T]=1
    m[b5 + 15] = 0x53        # (BW40[2T]=5), BW20[2T]=3
    m[b5 + 16] = 0x74        # (BW40[3T]=7), BW20[3T]=4
    m[b5 + 18] = 0x65        # OFDM[2T]=6, OFDM[3T]=5
    pp = efuse._parse_tx_power_5g(bytes(m), 2)[0]
    assert pp.bw40_base == tuple(range(0x30, 0x3E))
    assert pp.cck_base == () and pp.cck_diff == ()
    # 1st/2nd-stream diffs loaded; 3rd-stream zeroed by max_tx_cnt=2.
    assert pp.ofdm_diff == (1, 6, 0)               # 1T, 2T, (3T -> 0)
    assert pp.bw20_diff == (2, 3, 0)


def test_set_tx_power_5g_count_and_no_double_write():
    writes = []

    class Rec:
        def write32(self, a, v):
            writes.append((a, v))

    pp = efuse.PathTxPwr(cck_base=(), bw40_base=(0x24,) * 14, cck_diff=(),
                         ofdm_diff=(0, 0, 0), bw20_diff=(0, 0, 0))
    txpower.set_tx_power_5g(Rec(), 36, (pp,) * 4)
    assert len(writes) == 62 * 4                    # 62 rates/path, NO MGN_1M double-write
    assert all(a == 0x1998 for a, _ in writes)
    # base 0x24 + 2 = 0x26; first write: path 0, OFDM 6M (hw 0x04).
    assert writes[0] == (0x1998, 0x26801004)
