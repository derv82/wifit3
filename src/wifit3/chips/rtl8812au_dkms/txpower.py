"""RTL8812AU per-rate TX-power level — 2-path (2T2R), vendor faithful, 2.4 GHz.

``PHY_SetTxPowerLevel8812`` loops each (path, rate-section) and writes the rate's power
index into the direct TXAGC registers via ``PHY_SetTxPowerIndex_8812A`` [SRC]
rtl8812a_phycfg.c:538 — masked byte writes to 0xC20..0xC44 (path A) and 0xE20..0xE44
(path B, = path A + 0x200). ``PHY_TxPowerTrainingByPath_8812`` then packs the MCS7 index
into 0xC54 (A) / 0xE54 (B).

The Lucid-Duck Makefile builds CONFIG_TXPWR_BY_RATE_EN=0 / CONFIG_TXPWR_LIMIT_EN=0, so
``hal_com_get_txpwr_idx`` collapses to the PG base:

    PowerIndex = base[rate-section][ch-group] + diff[1TX]   (clamped [0, 63])

base/diff come from the EFUSE per-path PG block (efuse.PathTxPwr). The AWUS036ACH is a
normal chip, so the JAGUAR test-chip odd-index workaround does not fire. The 8812 is
2T2R, so BOTH paths' 1SS rate sections are written from their own PG data (HT MCS8-15 /
VHT 2-3SS are skipped — 20 MHz 1SS only).
"""
from __future__ import annotations

from typing import List

from ..rtl88xxau_base.sipi import set_bb
from .efuse import PathTxPwr

_TXGI_MAX = 63
_BYTE_MASK = (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)

# Path-A TXAGC registers (path B = +0x200), in wire emit order: (reg, rate-section,
# n_bytes). 0xC44 holds VHT1SS-MCS8/9 then VHT2SS, so only its low 2 bytes are 1SS.
_RATE_REGS_A = (
    (0x0C20, "cck", 4),    # CCK 1/2/5.5/11M
    (0x0C24, "ofdm", 4),   # OFDM 6/9/12/18M
    (0x0C28, "ofdm", 4),   # OFDM 24/36/48/54M
    (0x0C2C, "bw20", 4),   # HT MCS0-3
    (0x0C30, "bw20", 4),   # HT MCS4-7
    (0x0C3C, "bw20", 4),   # VHT1SS MCS0-3
    (0x0C40, "bw20", 4),   # VHT1SS MCS4-7
    (0x0C44, "bw20", 2),   # VHT1SS MCS8-9
)
_PATH_B_OFFSET = 0x200
_REG_A_TXPWR_TRAINING = 0x0C54
_RATE_REGS_A_5G = tuple(r for r in _RATE_REGS_A if r[1] != "cck")   # 5 GHz has no CCK


def _ch_group_2g(channel: int) -> tuple:
    """[SRC] rtw_get_ch_group — 2.4G channel -> (bw40/ofdm group, cck group)."""
    if channel <= 2:
        g = 0
    elif channel <= 5:
        g = 1
    elif channel <= 8:
        g = 2
    elif channel <= 11:
        g = 3
    else:
        g = 4
    return g, (5 if channel == 14 else g)


_CH_GROUP_5G = (
    (36, 42, 0), (44, 48, 1), (50, 58, 2), (60, 64, 3), (100, 106, 4), (108, 114, 5),
    (116, 122, 6), (124, 130, 7), (132, 138, 8), (140, 144, 9), (149, 155, 10),
    (157, 161, 11), (165, 171, 12), (173, 177, 13),
)


def _ch_group_5g(channel: int) -> int:
    for lo, hi, g in _CH_GROUP_5G:
        if lo <= channel <= hi:
            return g
    raise ValueError(f"RTL8812AU: 5G channel {channel} has no PG group")


def _pg_idx(pp: PathTxPwr, section: str, group: int, cck_group: int) -> int:
    """[SRC] phy_get_pg_txpwr_idx (ntx_idx=1) — base[section][group] + 1TX diff."""
    if section == "cck":
        v = pp.cck_base[cck_group] + pp.cck_diff[0]
    elif section == "ofdm":
        v = pp.bw40_base[group] + pp.ofdm_diff[0]
    else:                  # bw20: HT / VHT at 20 MHz share the BW20 diff
        v = pp.bw40_base[group] + pp.bw20_diff[0]
    return max(0, min(_TXGI_MAX, v))


def _training_word(bw20_idx: int) -> int:
    """[SRC] PHY_TxPowerTrainingByPath_8812: MCS7 (bw20) idx -10/-8/-6 cumulative, floored 2."""
    pl, wd = bw20_idx, 0
    for i, step in enumerate((10, 8, 6)):
        pl -= step
        wd |= (max(pl, 2) & 0xFF) << (i * 8)
    return wd


def _write_path(t, rate_regs, idx: dict, off: int) -> None:
    for reg, section, n_bytes in rate_regs:
        for b in range(n_bytes):
            set_bb(t, reg + off, _BYTE_MASK[b], idx[section])
    set_bb(t, _REG_A_TXPWR_TRAINING + off, 0x00FFFFFF, _training_word(idx["bw20"]))


def set_tx_power(t, channel: int, tx_power_2g: List[PathTxPwr]) -> None:
    """[SRC] PHY_SetTxPowerLevel8812 (2.4 GHz) — per-rate txagc + training, both paths."""
    g, cck_g = _ch_group_2g(channel)
    for path, pp in enumerate(tx_power_2g):
        idx = {s: _pg_idx(pp, s, g, cck_g) for s in ("cck", "ofdm", "bw20")}
        _write_path(t, _RATE_REGS_A, idx, path * _PATH_B_OFFSET)


def set_tx_power_5g(t, channel: int, tx_power_5g: List[PathTxPwr]) -> None:
    """[SRC] PHY_SetTxPowerLevel8812 (5 GHz) — per-rate txagc + training, both paths, no CCK."""
    g = _ch_group_5g(channel)
    for path, pp in enumerate(tx_power_5g):
        idx = {"ofdm": _pg_idx(pp, "ofdm", g, 0), "bw20": _pg_idx(pp, "bw20", g, 0)}
        _write_path(t, _RATE_REGS_A_5G, idx, path * _PATH_B_OFFSET)
