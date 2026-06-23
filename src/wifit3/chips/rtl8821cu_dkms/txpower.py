"""RTL8821CU per-channel TX power (TXAGC) — the `0x1d00` writes that close a channel tune.

`rtl8821c_set_tx_power_level` [SRC] rtl8821c_phy.c:556 loops the rate sections, computes a per-rate
power index, and writes it into the TXAGC table at `0x1d00 + (hw_rate & 0xfc)`, 4 rates packed per
dword (`rtl8821c_set_tx_power_index` [SRC] :420 -> `config_phydm_write_txagc_8821c`). 8821C is 1T1R,
so only the 1SS sections (CCK / OFDM / HT MCS0-7 / VHT-1SS) are written — all to `0x1d00` (the path-A
register, forced by `set_tx_power_index`) even though a BTG card looks the index up on RF_PATH_B.

The full index is `base + min((by_rate + ...), rlimit, limit, ulimit) + tpc + ...` [SRC]
hal_com_phycfg.c:6055. The captured DKMS build compiles **`CONFIG_TXPWR_BY_RATE_EN=n` +
`CONFIG_TXPWR_LIMIT_EN=n`** [SRC] Makefile:94,96, so `by_rate` folds to 0 and the regulatory limit
early-returns; every other term is 0 at init. The index reduces to **`base = phy_get_pg_txpwr_idx`**
(the EFUSE PG base), verified `base == wire` on channel 1 (CCK 0x2d / OFDM 0x2a / HT-VHT 0x28).

Consequence (documented, not silent): wifit3 applies **no regulatory TX-power cap** here, matching
the captured driver's default build. The user owns regional compliance for any manual TX.

PG base block at logical EFUSE `pg_txpwr_saddr = 0x10` [SRC] rtl8821c_halinit.c:61; per-path layout
`PG_TXPWR_1PATH_BYTE_NUM_2G = 18` / `_5G = 24` [SRC] hal_com_phycfg.c:20,23 via
`hal_load_pg_txpwr_info_path_2g` [SRC] :714.
"""
from __future__ import annotations

from dataclasses import dataclass

_TXAGC_BASE_A = 0x1D00
_PG_SADDR = 0x10
_PG_1PATH = 18 + 24                # 2.4G (18) + 5G (24) bytes per path
_GRP_2G = 6                        # MAX_CHNL_GROUP_24G (CCK base count; BW40 base = this - 1)
_SWITCH_TO_BTG = 0


@dataclass
class TxpwrPG:
    """Per-path PG TX-power base + the 1T BW20-relevant 2.4G diffs — enough for the BW20 base
    lookup. The 5G block and the 2T/3T/4T diff bytes are skipped (1T1R, 2.4 GHz)."""
    cck_base: list        # [path][group]  (6 groups)
    bw40_base_2g: list    # [path][group]  (5 groups)
    ofdm_1t: list         # [path]
    bw20_1t: list         # [path]


def _s4(nib: int) -> int:
    """Low-nibble signed-4-bit -> s8 (PG_TXPWR_*_DIFF_TO_S8BIT)."""
    return nib - 16 if (nib & 0x8) else nib


def parse_pg(log_map: bytes, npaths: int = 2) -> TxpwrPG:
    """Decode the 2.4G PG TX-power base (both paths) from the logical EFUSE map: CCK base x6, BW40
    base x5, then diff byte 0 = BW20-1T[7:4] | OFDM-1T[3:0]. The rest of each path's block (more
    diffs + the 5G block) is skipped via the fixed per-path stride."""
    cck, bw40, ofdm_1t, bw20_1t = [], [], [], []
    for p in range(npaths):
        base = _PG_SADDR + p * _PG_1PATH
        cck.append([log_map[base + g] for g in range(_GRP_2G)])
        bw40.append([log_map[base + _GRP_2G + g] for g in range(_GRP_2G - 1)])
        d0 = log_map[base + _GRP_2G + (_GRP_2G - 1)]     # first diff byte
        ofdm_1t.append(_s4(d0 & 0xF))
        bw20_1t.append(_s4(d0 >> 4))
    return TxpwrPG(cck, bw40, ofdm_1t, bw20_1t)


def _ch_group_2g(ch: int) -> tuple[int, int]:
    """rtw_get_ch_group [SRC] rtw_rf.c:505 — 2.4G (bw40 group, cck group)."""
    g = 0 if ch <= 2 else 1 if ch <= 5 else 2 if ch <= 8 else 3 if ch <= 11 else 4
    return g, (5 if ch == 14 else g)


# 1T rate sections written at init (under_survey false). hw_rate is the DESC rate index; the
# register is 0x1d00 + (hw_rate & 0xfc). [SRC] rtl8821c_set_tx_power_level rtl8821c_phy.c:556
_SECTIONS = (
    ("cck", range(0, 4)),          # CCK 1/2/5.5/11M
    ("ofdm", range(4, 12)),        # OFDM 6..54M
    ("ht", range(12, 20)),         # HT MCS0-7 (1ss)
    ("vht", range(44, 54)),        # VHT 1SS MCS0-9 (DESC 0x2c-0x35)
)
_VHTSS1MCS9 = 53                   # the extra flush point besides hw_rate%4==3


def _pg_base(pg: TxpwrPG, path: int, ch: int, section: str) -> int:
    """phy_get_pg_txpwr_idx [SRC] hal_com_phycfg.c:2322 at BW20 — EFUSE PG base + section diff:
    CCK is the CCK base; OFDM adds the OFDM-1T diff; HT/VHT add the BW20-1T diff."""
    g, cck_g = _ch_group_2g(ch)
    if section == "cck":
        return pg.cck_base[path][cck_g]
    base = pg.bw40_base_2g[path][g]
    if section == "ofdm":
        return base + pg.ofdm_1t[path]
    return base + pg.bw20_1t[path]


def set_tx_power_level(t, info, channel: int) -> None:
    """rtl8821c_set_tx_power_level [SRC] rtl8821c_phy.c:556 — write the per-channel TXAGC table
    (0x1d00). A BTG 2.4 GHz card looks the index up on RF_PATH_B; the register write is always
    path A. 4 rate bytes accumulate per dword, flushed at hw_rate%4==3 (and at the last VHT rate).

    under_survey_ch is FALSE at init, so all four 1SS sections are written."""
    pg = parse_pg(info.log_map)
    btg = info.default_rf_set == _SWITCH_TO_BTG
    path = 1 if (channel <= 14 and btg) else 0          # RF_PATH_B for a BTG 2.4 GHz card
    buf = 0
    for section, hw_rates in _SECTIONS:
        val = _pg_base(pg, path, channel, section) & 0xFF
        for hw in hw_rates:
            shift = hw & 0x3
            buf |= val << (shift * 8)
            if shift == 3 or hw == _VHTSS1MCS9:
                t.write32(_TXAGC_BASE_A + (hw & 0xFC), buf & 0xFFFFFFFF)
                buf = 0
