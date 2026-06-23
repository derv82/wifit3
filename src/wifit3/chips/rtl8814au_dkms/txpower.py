"""RTL8814AU TX-power level (M2e) — port of the vendor stack, 2.4 GHz.

`rtw_hal_set_tx_power_level` -> `PHY_SetTxPowerLevel8814` loops the 4 RF paths and,
per rate, writes the per-rate power index into the "txagc table" at BB reg 0x1998
[SRC PHY_SetTxPowerIndex_8814A:742]:

    txagc_table_wd = 0x00801000 | (RFPath<<8) | MRateToHwRate(Rate) | (PowerIndex<<24)

For this morrownr build, `CONFIG_TXPWR_BY_RATE_EN=0` and `CONFIG_TXPWR_LIMIT_EN=0`
[SRC Makefile/drv_conf.h], so `PHY_GetTxPowerByRate` returns 0 and `PHY_GetTxPowerLimit`
returns the non-binding ceiling. `PHY_GetTxPowerIndex8814A` then collapses to:

    PowerIndex = clamp(pg + (CurrentTxPwrIdx - 18), 0, txgi_max)

where `pg` is the efuse base for the rate's group + the cumulative nTX diff
[SRC phy_get_pg_txpwr_idx]. CurrentTxPwrIdx defaults to 20 (-> +2). For this card
the diffs net to zero across every rate, so PowerIndex = base + 2 — but the diff
accumulation is ported in full (it is channel/efuse general). Verified
byte-for-byte; [WIRE] cap1 frames 13843-14377 (268 writes = 67/path x 4).
"""
from __future__ import annotations

REG_TXAGC = 0x1998
_TXAGC_BASE = 0x00801000
_CURRENT_TX_PWR_IDX = 20        # rtl8814a_dm.c default; phy_TxPwrAdjInPercentage += idx-18
_TXGI_MAX = 63                  # hal_spec->txgi_max (clamp ceiling)

# Per-rate descriptor: (hw_rate, base_kind, diff_kind, nss) in the exact emit order
# of phy_set_tx_power_level_by_path's rate sections [SRC rates_by_sections]:
#   CCK, OFDM, HT0-7, VHT1SS, HT8-15, VHT2SS, HT16-23, VHT3SS.
# base_kind: 'cck' uses CCK base, else BW40 base. diff_kind picks the diff array.
def _build_rate_table() -> tuple:
    rows = []
    rows += [(hw, "cck", "cck", 1) for hw in range(0x00, 0x04)]    # CCK 1..11M
    rows += [(hw, "bw40", "ofdm", 1) for hw in range(0x04, 0x0C)]  # OFDM 6..54M
    rows += [(hw, "bw40", "bw20", 1) for hw in range(0x0C, 0x14)]  # HT MCS0-7
    rows += [(hw, "bw40", "bw20", 1) for hw in range(0x2C, 0x36)]  # VHT1SS MCS0-9
    rows += [(hw, "bw40", "bw20", 2) for hw in range(0x14, 0x1C)]  # HT MCS8-15
    rows += [(hw, "bw40", "bw20", 2) for hw in range(0x36, 0x40)]  # VHT2SS MCS0-9
    rows += [(hw, "bw40", "bw20", 3) for hw in range(0x1C, 0x24)]  # HT MCS16-23
    rows += [(hw, "bw40", "bw20", 3) for hw in range(0x40, 0x4A)]  # VHT3SS MCS0-9
    return tuple(rows)


RATE_TABLE = _build_rate_table()   # 66 rates


def _ch_group_2g(channel: int) -> tuple:
    """[SRC] rtw_get_ch_group — 2.4G channel -> (group, cck_group)."""
    if channel <= 2:
        g = 0
    elif channel <= 5:
        g = 1
    elif channel <= 8:
        g = 2
    elif channel <= 11:
        g = 3
    else:                  # 12..14
        g = 4
    return g, (5 if channel == 14 else g)


# [SRC] rtw_get_ch_group (5G branch) — channel -> one of 14 UNII groups. The per-channel
# BW40 base is just the group base (hal_load_pg_txpwr_info expands group -> per channel).
_CH_GROUP_5G = (
    (36, 42, 0), (44, 48, 1), (50, 58, 2), (60, 64, 3), (100, 106, 4), (108, 114, 5),
    (116, 122, 6), (124, 130, 7), (132, 138, 8), (140, 144, 9), (149, 155, 10),
    (157, 161, 11), (165, 171, 12), (173, 177, 13),
)


def _ch_group_5g(channel: int) -> int:
    """[SRC] rtw_get_ch_group — 5G channel -> group index (0..13)."""
    for lo, hi, g in _CH_GROUP_5G:
        if lo <= channel <= hi:
            return g
    raise ValueError(f"RTL8814AU: 5G channel {channel} has no PG group")


def power_index(pp, base_kind: str, diff_kind: str, nss: int,
                group: int, cck_group: int = 0) -> int:
    """[SRC] phy_get_pg_txpwr_idx + PHY_GetTxPowerIndex8814A (by-rate/limit = no-op).

    ``group`` is the channel's PG group (``_ch_group_2g`` / ``_ch_group_5g``); ``cck_group``
    only matters when ``base_kind == "cck"`` (2.4 GHz). 5 GHz uses ``bw40_base`` + OFDM/BW20
    diffs only — identical to the 2.4G non-CCK path, just a different group + bases.
    """
    base = pp.cck_base[cck_group] if base_kind == "cck" else pp.bw40_base[group]
    diff = {"cck": pp.cck_diff, "ofdm": pp.ofdm_diff, "bw20": pp.bw20_diff}[diff_kind]
    pg = base + sum(diff[k] for k in range(nss))   # cumulative diff over stream count
    idx = pg + (_CURRENT_TX_PWR_IDX - 18)
    return max(0, min(_TXGI_MAX, idx))


def set_tx_power(t, channel: int, tx_power: tuple, write_cck: bool = True) -> None:
    """[SRC] PHY_SetTxPowerLevel8814 -> phy_set_tx_power_level_by_path — write the 2.4 GHz
    txagc table for all 4 paths.

    ``phy_set_tx_power_level_by_path`` writes the CCK rate section only when
    ``current_band_type == BAND_ON_2_4G``; ``write_cck`` carries that decision. When False
    (the software band field is still BAND_MAX, e.g. the airmon retune + 2.4 GHz hops before
    any 5G crossing), CCK is skipped and the table starts at OFDM (hw rate 0x04).
    """
    g, cck_g = _ch_group_2g(channel)
    rate_table = RATE_TABLE if write_cck else RATE_TABLE_5G   # _5G == RATE_TABLE minus CCK
    for path in range(4):
        pp = tx_power[path]
        for hw, base_kind, diff_kind, nss in rate_table:
            pidx = power_index(pp, base_kind, diff_kind, nss, g, cck_g)
            wd = (_TXAGC_BASE | (path << 8) | hw | (pidx << 24)) & 0xFFFFFFFF
            t.write32(REG_TXAGC, wd)
            if hw == 0x00:                 # MGN_1M: written twice to turn on the table
                t.write32(REG_TXAGC, wd)


# 5 GHz rate set = the 2.4G rate sections minus CCK (no CCK on 5 GHz). [SRC] the
# rates_by_sections order in phy_set_tx_power_level_by_path.
RATE_TABLE_5G = tuple(r for r in RATE_TABLE if r[1] != "cck")   # 62 rates


def set_tx_power_5g(t, channel: int, tx_power_5g: tuple) -> None:
    """[SRC] PHY_SetTxPowerLevel8814 (5 GHz) — write the txagc table for all 4 paths.

    Same per-(path,rate) 0x1998 write as 2.4 GHz, but the 5 GHz group base + OFDM/BW20
    diffs and no CCK (so no MGN_1M double-write — the table is already on from the 2.4G
    init tune)."""
    g = _ch_group_5g(channel)
    for path in range(4):
        pp = tx_power_5g[path]
        for hw, base_kind, diff_kind, nss in RATE_TABLE_5G:
            pidx = power_index(pp, base_kind, diff_kind, nss, g)
            wd = (_TXAGC_BASE | (path << 8) | hw | (pidx << 24)) & 0xFFFFFFFF
            t.write32(REG_TXAGC, wd)
