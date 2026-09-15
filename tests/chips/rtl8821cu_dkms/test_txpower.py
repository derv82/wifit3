"""rtl8821cu_dkms per-channel TXAGC — the unfused/partial-PG fallback + index clamp (H1).

An unfused (0xFF) or out-of-range PG base drops to the IC-default PG base (2.4G 0x2D / 5G 0x28)
via the vendor's multi-source fallback; the final TXAGC index is clamped to [0, txgi_max=63].
[SRC] hal_load_pg_txpwr_info hal_com_phycfg.c:1004 / rtl8821c_pg_txpwr_def_info :356 / clamp :6126.
"""
from types import SimpleNamespace

from wifit3.chips.rtl8821cu_dkms import txpower

_WLG, _BTG = 1, 0


class Rec:
    def __init__(self):
        self.ops = []

    def write32(self, a, v):
        self.ops.append(("W32", a, v))


def _txagc_bytes(rec):
    """All per-rate TXAGC bytes written to the 0x1d00 table (4 packed per dword)."""
    out = []
    for op in rec.ops:
        if op[0] == "W32" and 0x1D00 <= op[1] < 0x1E00:
            out += list(op[2].to_bytes(4, "little"))
    return out


def test_parse_pg_unfused_substitutes_ic_default_base():
    pg = txpower.parse_pg(b"\xff" * 512)
    assert pg.cck_base[0] == [0x2D] * 6            # invalid CCK base -> 2.4G IC-default
    assert pg.bw40_base_2g[0] == [0x2D] * 5
    assert pg.bw40_base_5g[0] == [0x28] * 14       # invalid 5G base -> 5G IC-default
    assert pg.ofdm_1t[0] == -1 and pg.bw20_1t[0] == -1   # 0xFF nibble -> -1 (valid diff, kept)


def test_unfused_card_writes_no_garbage_all_indices_clamped():
    info = SimpleNamespace(log_map=b"\xff" * 512, default_rf_set=_WLG)
    rec = Rec()
    txpower.set_tx_power_level(rec, info, channel=1)
    bytes_written = _txagc_bytes(rec)
    assert bytes_written                            # something was written
    assert all(0 <= b <= 63 for b in bytes_written)  # never the 0xFF-derived garbage
    # ch1 group0: CCK = IC-default 0x2D; OFDM/HT/VHT = 0x2D + (-1) diff = 0x2C.
    assert 0x2D in bytes_written and 0x2C in bytes_written


def test_final_index_clamped_high_and_low():
    lm = bytearray(b"\xff" * 512)
    # path A (WLG 2G) group0: bw40 base @0x16, first diff byte @0x1B (ofdm=low nibble).
    lm[0x16] = 0x3F                                  # base 63 (valid)
    lm[0x1B] = 0x07                                  # ofdm diff +7 -> 63+7=70 -> clamp 63
    rec = Rec()
    txpower.set_tx_power_level(rec, SimpleNamespace(log_map=bytes(lm), default_rf_set=_WLG), channel=1)
    assert max(_txagc_bytes(rec)) == 63

    lm[0x16] = 0x00                                  # base 0
    lm[0x1B] = 0x08                                  # ofdm diff -8 -> 0-8=-8 -> clamp 0
    rec = Rec()
    txpower.set_tx_power_level(rec, SimpleNamespace(log_map=bytes(lm), default_rf_set=_WLG), channel=1)
    assert min(_txagc_bytes(rec)) == 0
