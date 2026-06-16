"""RTL8822BU per-channel TX power (TXAGC) — the `0x1d00`/`0x1d80` writes after a channel tune.

`rtl8822b_set_tx_power_level` `[SRC] rtl8822b_phy.c:671` loops RF paths x rate-sections, computes a
per-rate power index, and writes it into the TXAGC table at `0x1d00`(path A)/`0x1d80`(path B) +
`(hw_rate & 0xfc)`, 4 rates packed per dword `[SRC] phydm_hal_api8822b.c:1047`.

The full index is `base + min((by_rate + btc + extra), rlimit, limit, ulimit) + tpc + tpt + dpd`
`[SRC] hal_com_phycfg.c:6054`. The captured morrownr build compiles with **both** power-by-rate and
the regulatory limit DISABLED at runtime — `CONFIG_TXPWR_BY_RATE_EN=n` + `CONFIG_TXPWR_LIMIT_EN=n`
`[SRC] driver-source/Makefile:130,132`. So `RegEnableTxPowerByRate=0` makes `by_rate` fold to 0
(`phy_get_txpwr_target`: `if (by_rate == txgi_max) by_rate = 0` `[SRC] hal_com_phycfg.c:5940`) and
`RegEnableTxPowerLimit=0` makes `phy_get_txpwr_lmt` early-return "no limit". Every other term is 0 at
init, so the index reduces to **`base = phy_get_pg_txpwr_idx`** (the EFUSE PG base) `[SRC]
hal_com_phycfg.c:2322` — domain-independent, verified `base == wire` on every captured channel.

Consequence (documented, not silent): wifit3 applies **no regulatory TX-power cap** here, matching the
captured driver's default build. The user owns regional compliance for any manual TX.

The PG base block lives at logical EFUSE `pg_txpwr_saddr = 0x10` `[SRC] rtl8822b_halinit.c:64`; the
per-path byte layout is `hal_load_pg_txpwr_info_path_2g/5g` `[SRC] hal_com_phycfg.c:714,843`.
"""
from __future__ import annotations

from dataclasses import dataclass

_TXAGC_BASE = (0x1D00, 0x1D80)         # offset_txagc[path]
_PG_SADDR = 0x10
_GRP_2G = 6                            # MAX_CHNL_GROUP_24G (CCK base count; BW40 base = this - 1)
_GRP_5G = 14                           # MAX_CHNL_GROUP_5G


@dataclass
class TxpwrPG:
    """Per-path PG TX-power base indices + the BW20-relevant diffs (1T/2T), 2.4G + 5G.

    Only the fields the BW20 base lookup needs are kept (CCK/OFDM/HT/VHT at 20 MHz); the BW40/BW80/
    BW160 diff bytes are parsed-through to keep the offset aligned but not stored."""
    cck_base: list        # [path][group]  (6 groups, 2.4G)
    bw40_base_2g: list    # [path][group]  (5 groups, 2.4G)
    bw40_base_5g: list    # [path][group]  (14 groups, 5G)
    ofdm_1t: list         # [path] {2g,5g}
    bw20_1t: list         # [path] {2g,5g}
    bw20_2t: list         # [path] {2g,5g}


def _s4(nib: int) -> int:
    """Low-nibble signed-4-bit -> s8 (PG_TXPWR_*_DIFF_TO_S8BIT)."""
    return nib - 16 if (nib & 0x8) else nib


def parse_pg(log_map: bytes, npaths: int = 2) -> TxpwrPG:
    """Decode the PG TX-power base block (both paths) from the logical EFUSE map."""
    off = _PG_SADDR
    cck, bw40_2g, bw40_5g = [], [], []
    ofdm_1t, bw20_1t, bw20_2t = [], [], []
    for _ in range(npaths):
        # --- 2.4G: CCK base x6, BW40 base x5, then 7 diff bytes (tx0..tx3) ---
        cck.append([log_map[off + g] for g in range(_GRP_2G)])
        off += _GRP_2G
        bw40_2g.append([log_map[off + g] for g in range(_GRP_2G - 1)])
        off += _GRP_2G - 1
        d0, d1 = log_map[off], log_map[off + 1]      # tx0 [BW20-1T|OFDM-1T], tx1 [BW40-2T|BW20-2T]
        off += 7                                     # tx0(1) + tx1..tx3(2 each); only 1T/2T used
        ofdm_1t.append({"2g": _s4(d0 & 0xF)})
        bw20_1t.append({"2g": _s4(d0 >> 4)})
        bw20_2t.append({"2g": _s4(d1 & 0xF)})
        # --- 5G: BW40 base x14, then 10 diff bytes (tx0 [BW20-1T|OFDM-1T], tx1 [BW40-2T|BW20-2T]) ---
        bw40_5g.append([log_map[off + g] for g in range(_GRP_5G)])
        off += _GRP_5G
        e0, e1 = log_map[off], log_map[off + 1]
        off += 10                                    # tx0..tx3 (4) + OFDM2T~3T + OFDM4T + BW80/160(4)
        ofdm_1t[-1]["5g"] = _s4(e0 & 0xF)
        bw20_1t[-1]["5g"] = _s4(e0 >> 4)
        bw20_2t[-1]["5g"] = _s4(e1 & 0xF)
    return TxpwrPG(cck, bw40_2g, bw40_5g, ofdm_1t, bw20_1t, bw20_2t)


def _ch_group(ch: int) -> tuple:
    """[SRC] rtw_get_ch_group (rtw_rf.c:505) — (bw40 group, cck group). 5G has no CCK group."""
    if ch <= 14:
        g = 0 if ch <= 2 else 1 if ch <= 5 else 2 if ch <= 8 else 3 if ch <= 11 else 4
        return g, (5 if ch == 14 else g)
    for hi, g in ((42, 0), (48, 1), (58, 2), (64, 3), (106, 4), (114, 5), (122, 6),
                  (130, 7), (138, 8), (144, 9), (155, 10), (161, 11), (171, 12), (177, 13)):
        if ch <= hi:
            return g, None
    return None, None


# rate sections written for a 2T2R card at BW20 (under_survey false): (key, ntx, hw_rate list).
# hw_rate = sequential DESC_RATE index; the register is 0x1d00/0x1d80 + (hw_rate & 0xfc).
_SECTIONS = (
    ("cck", 1, range(0, 4)),          # CCK 1/2/5.5/11M (2.4G only)
    ("ofdm", 1, range(4, 12)),        # OFDM 6..54M
    ("ht1", 1, range(12, 20)),        # HT MCS0-7  (1ss)
    ("ht2", 2, range(20, 28)),        # HT MCS8-15 (2ss)
    ("vht1", 1, range(44, 54)),       # VHT 1SS MCS0-9
    ("vht2", 2, range(54, 64)),       # VHT 2SS MCS0-9
)


def _pg_base(pg: TxpwrPG, path: int, ch: int, section: str) -> int:
    """[SRC] phy_get_pg_txpwr_idx (hal_com_phycfg.c:2322) at BW20 — EFUSE PG base + section/ntx diff.

    CCK is 1ss (cck_diff[1T]=0). OFDM uses the 1T OFDM diff. HT/VHT use the BW20 1T diff, plus the
    BW20 2T diff for the 2ss sections. diff_factor is 1 on 8822b."""
    g, cck_g = _ch_group(ch)
    if ch <= 14:
        if section == "cck":
            return pg.cck_base[path][cck_g]
        base = pg.bw40_base_2g[path][g]
        band = "2g"
    else:
        base = pg.bw40_base_5g[path][g]
        band = "5g"
    if section == "ofdm":
        return base + pg.ofdm_1t[path][band]
    base += pg.bw20_1t[path][band]                    # 1ss BW20
    if section in ("ht2", "vht2"):
        base += pg.bw20_2t[path][band]                # 2ss adds the 2T BW20 diff
    return base


def set_tx_power_level(t, ch: int, pg: TxpwrPG, rf_2t2r: bool = True) -> None:
    """[SRC] rtl8822b_set_tx_power_level — write the per-channel TXAGC table (0x1d00/0x1d80).

    One 4-byte power index is accumulated per path and flushed to `base + (hw_rate & 0xfc)` whenever
    the rate index crosses a dword boundary (`hw_rate % 4 == 3`); the buffer carries across rate
    sections, so a boundary register (e.g. 0x1d34) legitimately mixes VHT-1SS and VHT-2SS rates."""
    for path in (0, 1) if rf_2t2r else (0,):
        buf = 0
        for section, _ntx, hw_rates in _SECTIONS:
            if section == "cck" and ch > 14:
                continue
            val = _pg_base(pg, path, ch, section) & 0xFF
            for hw in hw_rates:
                shift = hw & 0x3
                buf |= val << (shift * 8)
                if shift == 3:
                    t.write32(_TXAGC_BASE[path] + (hw & 0xFC), buf & 0xFFFFFFFF)
                    buf = 0
