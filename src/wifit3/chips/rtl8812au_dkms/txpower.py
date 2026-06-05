"""RTL8812AU per-rate TX-power level — 2-path (2T2R), vendor faithful, 2.4 GHz.

``PHY_SetTxPowerLevel8812`` loops each path; ``phy_set_tx_power_level_by_path`` emits the
rate sections in this order (CCK only on 2.4 GHz; the 2SS sections only because the 8812
is 2T2R): **CCK, OFDM, HT MCS0-7, VHT1SS MCS0-9, HT MCS8-15, VHT2SS MCS0-9** [SRC]
hal_com_phycfg.c:2761. Each rate is one masked *byte* RMW into a TXAGC register via
``PHY_SetTxPowerIndex_8812A`` [SRC] rtl8812a_phycfg.c:538 (path B = path A + 0x200), then
``PHY_TxPowerTrainingByPath_8812`` packs the MCS7 index into 0xC54 (A) / 0xE54 (B).

The Lucid-Duck/morrownr Makefile builds CONFIG_TXPWR_BY_RATE_EN=0 / CONFIG_TXPWR_LIMIT_EN
=0, so ``hal_com_get_txpwr_idx`` collapses to the PG base:

    PowerIndex = base[rate-section][ch-group] + diff[ntx_idx]   (clamped [0, 63])

ntx_idx is the rate's spatial-stream count - 1: 1SS rates use diff[0] (1TX), the 2SS
sections use diff[1] (2TX). base/diff come from the EFUSE per-path PG block
(efuse.PathTxPwr). The AWUS036ACH is a normal chip, so the JAGUAR test-chip odd-index
workaround does not fire. The 2SS sections land on the AGC-table default here, but the
vendor still RMWs them, so they are part of the byte stream and must be reproduced.
"""
from __future__ import annotations

from typing import List

from ..rtl88xxau_base.sipi import set_bb
from .efuse import PathTxPwr

_TXGI_MAX = 63
_BYTE_MASK = (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
_PATH_B_OFFSET = 0x200
_REG_TXPWR_TRAINING = 0x0C54

# Rate-section -> (reg, first_byte, last_byte) spans, path A, in phy_set_tx_power_level_by_path
# emit order. PHY_SetTxPowerIndex_8812A maps each MGN rate to one (reg, byte). 0xC44 is split:
# bytes [0,1] = VHT1SS MCS8/9, bytes [2,3] = VHT2SS MCS0/1.
_SEC_CCK = ((0x0C20, 0, 3),)
_SEC_OFDM = ((0x0C24, 0, 3), (0x0C28, 0, 3))
_SEC_HT_1SS = ((0x0C2C, 0, 3), (0x0C30, 0, 3))                    # HT MCS0-7
_SEC_VHT_1SS = ((0x0C3C, 0, 3), (0x0C40, 0, 3), (0x0C44, 0, 1))   # VHT1SS MCS0-9
_SEC_HT_2SS = ((0x0C34, 0, 3), (0x0C38, 0, 3))                    # HT MCS8-15
_SEC_VHT_2SS = ((0x0C44, 2, 3), (0x0C48, 0, 3), (0x0C4C, 0, 3))   # VHT2SS MCS0-9


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


def _clamp(v: int) -> int:
    return max(0, min(_TXGI_MAX, v))


def _training_word(bw20_idx: int) -> int:
    """[SRC] PHY_TxPowerTrainingByPath_8812: HT-1SS MCS7 idx, -10/-8/-6 cumulative.

    The vendor's PowerLevel is u32, so the `(PowerLevel > 2) ? PowerLevel : 2` floor only
    catches 0..2 -- a level that goes negative wraps to a huge unsigned value, passes the
    `> 2` test, and lands as its two's-complement low byte (e.g. -4 -> 0xFC). Mirror that
    with an unsigned comparison rather than a signed max().
    """
    pl, wd = bw20_idx, 0
    for i, step in enumerate((10, 8, 6)):
        pl -= step
        v = pl if (pl & 0xFFFFFFFF) > 2 else 2
        wd |= (v & 0xFF) << (i * 8)
    return wd


def _write_section(t, spans, value: int, off: int) -> None:
    """One rate section: a masked byte RMW per rate (all rates share the same PG index
    since power-by-rate is disabled)."""
    for reg, b0, b1 in spans:
        for b in range(b0, b1 + 1):
            set_bb(t, reg + off, _BYTE_MASK[b], value)


def set_tx_power(t, channel: int, tx_power_2g: List[PathTxPwr]) -> None:
    """[SRC] PHY_SetTxPowerLevel8812 (2.4 GHz) — per-rate txagc + training, both paths."""
    g, cck_g = _ch_group_2g(channel)
    for path, pp in enumerate(tx_power_2g):
        off = path * _PATH_B_OFFSET
        cck = _clamp(pp.cck_base[cck_g] + pp.cck_diff[0])
        ofdm = _clamp(pp.bw40_base[g] + pp.ofdm_diff[0])
        # The per-nTX diffs are CUMULATIVE [SRC] phy_get_pg_txpwr_idx (hal_com_phycfg.c:2370):
        # BW20-2S = base + BW20_Diff[1TX] + BW20_Diff[2TX].
        ss1 = _clamp(pp.bw40_base[g] + pp.bw20_diff[0])                    # HT/VHT 1SS @ 20 MHz
        ss2 = _clamp(pp.bw40_base[g] + pp.bw20_diff[0] + pp.bw20_diff[1])  # HT/VHT 2SS @ 20 MHz
        _write_section(t, _SEC_CCK, cck, off)
        _write_section(t, _SEC_OFDM, ofdm, off)
        _write_section(t, _SEC_HT_1SS, ss1, off)
        _write_section(t, _SEC_VHT_1SS, ss1, off)
        _write_section(t, _SEC_HT_2SS, ss2, off)
        _write_section(t, _SEC_VHT_2SS, ss2, off)
        set_bb(t, _REG_TXPWR_TRAINING + off, 0x00FFFFFF, _training_word(ss1))


def set_tx_power_5g(t, channel: int, tx_power_5g: List[PathTxPwr]) -> None:
    """[SRC] PHY_SetTxPowerLevel8812 (5 GHz) — per-rate txagc + training, both paths, no CCK."""
    g = _ch_group_5g(channel)
    for path, pp in enumerate(tx_power_5g):
        off = path * _PATH_B_OFFSET
        ofdm = _clamp(pp.bw40_base[g] + pp.ofdm_diff[0])
        ss1 = _clamp(pp.bw40_base[g] + pp.bw20_diff[0])
        ss2 = _clamp(pp.bw40_base[g] + pp.bw20_diff[0] + pp.bw20_diff[1])
        _write_section(t, _SEC_OFDM, ofdm, off)
        _write_section(t, _SEC_HT_1SS, ss1, off)
        _write_section(t, _SEC_VHT_1SS, ss1, off)
        _write_section(t, _SEC_HT_2SS, ss2, off)
        _write_section(t, _SEC_VHT_2SS, ss2, off)
        set_bb(t, _REG_TXPWR_TRAINING + off, 0x00FFFFFF, _training_word(ss1))
