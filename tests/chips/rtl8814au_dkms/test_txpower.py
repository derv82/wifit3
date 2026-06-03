"""Hardware-free regression for the M2e TX-power table.

The full byte-for-byte check vs the cold-boot capture is
`scripts/rtl8814au_dkms/verify_pcap.py`; this pins the rate table, the pg/clamp
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
    pp = efuse._parse_tx_power(bytes(m))[0]
    assert pp.cck_base == (0x20,) * 6
    assert pp.bw40_base == (0x20,) * 5
    # 0xee LSB/MSB nibble = 0xe -> signed -2; only the [2] (3TX) terms are nonzero.
    assert pp.bw20_diff == (0, 0, 0)
    assert pp.ofdm_diff == (0, 0, -2)
    assert pp.cck_diff == (0, 0, -2)


def _path(cck, bw40):
    return efuse.PathTxPwr(cck_base=(cck,) * 6, bw40_base=(bw40,) * 5,
                           cck_diff=(0, 0, -2), ofdm_diff=(0, 0, -2),
                           bw20_diff=(0, 0, 0))


def test_power_index_matches_wire_pattern():
    # Path A: CCK base 0x20, BW40 base 0x20 -> all rates 0x20+2 = 0x22.
    a = _path(0x20, 0x20)
    assert txpower.power_index(a, "cck", "cck", 1, 1) == 0x22
    assert txpower.power_index(a, "bw40", "bw20", 3, 1) == 0x22   # 3SS, diffs net 0
    # Path B: CCK 0x27 -> 0x29; BW40 0x28 -> 0x2a (matches the captured PP bytes).
    b = _path(0x27, 0x28)
    assert txpower.power_index(b, "cck", "cck", 1, 1) == 0x29
    assert txpower.power_index(b, "bw40", "ofdm", 1, 1) == 0x2A


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
