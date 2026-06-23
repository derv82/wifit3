"""RTL8821AU (DKMS) M-TXPWR: per-rate TX-power level — vendor port, 2.4 GHz.

`rtw_hal_set_tx_power_level` -> `PHY_SetTxPowerLevel8812` -> `phy_set_tx_power_level_by_path`
loops the (path-A, 1SS) rate sections and writes each rate's power index into the direct
TXAGC registers via `PHY_SetTxPowerIndex_8812A` [SRC rtl8812a_phycfg.c:538] — masked byte
writes to 0xC20..0xC44. Then `PHY_TxPowerTrainingByPath_8812` [:437] packs the MCS7 index
into 0xC54.

The Lucid-Duck Makefile builds with CONFIG_TXPWR_BY_RATE_EN=0 and CONFIG_TXPWR_LIMIT_EN=0,
so `hal_com_get_txpwr_idx` collapses to the PG base: the by-rate diff is 0, the limit is
non-binding, and amends (TPC/BTC) are 0 at init. So

    PowerIndex = phy_get_pg_txpwr_idx = base[rate-section][ch-group] + diff[1TX]

clamped to [0, txgi_max=63]. base/diff come from the efuse (efuse.PathTxPwr); for ch1 this
reproduces the wire exactly (CCK 0x31, OFDM 0x2d, HT/VHT 0x2b). The AWUS036ACS is a normal
chip, so the JAGUAR test-chip "odd index -> even" workaround does not apply. The 8821au is
1T1R, so only path A and the 1SS rate sections are written (HT MCS8-15 / VHT 2-3SS skipped).
Verified byte-for-byte; [WIRE] cap1 frames 7485-7607 (31 writes).
"""
from __future__ import annotations

from .efuse import PathTxPwr
from .rf import set_bb

_TXGI_MAX = 63                       # hal_spec->txgi_max (clamp ceiling)
_BYTE_MASK = (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)

# Path-A TXAGC registers [SRC] Hal8812PhyReg.h, in wire emit order. Each entry is
# (reg, rate-section, n_bytes): the section picks the base+diff, n_bytes is how many of
# the register's 4 rate slots the 1SS chip writes (0xC44 holds VHT1SS-MCS8/9 then
# VHT2SS, so only its low 2 bytes are written).
_RATE_REGS = (
    (0x0C20, "cck", 4),    # rTxAGC_A_CCK11_CCK1   : CCK 1/2/5.5/11M
    (0x0C24, "ofdm", 4),   # rTxAGC_A_Ofdm18_Ofdm6 : OFDM 6/9/12/18M
    (0x0C28, "ofdm", 4),   # rTxAGC_A_Ofdm54_Ofdm24: OFDM 24/36/48/54M
    (0x0C2C, "bw20", 4),   # rTxAGC_A_MCS3_MCS0    : HT MCS0-3
    (0x0C30, "bw20", 4),   # rTxAGC_A_MCS7_MCS4    : HT MCS4-7
    (0x0C3C, "bw20", 4),   # rTxAGC_A_Nss1Index3_0 : VHT1SS MCS0-3
    (0x0C40, "bw20", 4),   # rTxAGC_A_Nss1Index7_4 : VHT1SS MCS4-7
    (0x0C44, "bw20", 2),   # rTxAGC_A_Nss2Index1_Nss1Index8 : VHT1SS MCS8-9
)
_REG_A_TXPWR_TRAINING = 0x0C54       # rA_TxPwrTraing_Jaguar


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
    else:                  # 12..14
        g = 4
    return g, (5 if channel == 14 else g)


def _pg_idx(pp: PathTxPwr, section: str, group: int, cck_group: int) -> int:
    """[SRC] phy_get_pg_txpwr_idx (2.4G, ntx_idx=1) — base[section][group] + 1TX diff."""
    if section == "cck":
        v = pp.cck_base[cck_group] + pp.cck_diff[0]
    elif section == "ofdm":
        v = pp.bw40_base[group] + pp.ofdm_diff[0]
    else:                  # bw20: HT / VHT at 20 MHz share the BW20 diff
        v = pp.bw40_base[group] + pp.bw20_diff[0]
    return max(0, min(_TXGI_MAX, v))


# [SRC] rtw_get_ch_group (5G) — channel -> one of 14 UNII groups (the per-channel BW40
# base is just the group base; hal_load_pg_txpwr_info expands group -> per channel).
_CH_GROUP_5G = (
    (36, 42, 0), (44, 48, 1), (50, 58, 2), (60, 64, 3), (100, 106, 4), (108, 114, 5),
    (116, 122, 6), (124, 130, 7), (132, 138, 8), (140, 144, 9), (149, 155, 10),
    (157, 161, 11), (165, 171, 12), (173, 177, 13),
)
_RATE_REGS_5G = tuple(r for r in _RATE_REGS if r[1] != "cck")   # 5 GHz has no CCK


def _ch_group_5g(channel: int) -> int:
    for lo, hi, g in _CH_GROUP_5G:
        if lo <= channel <= hi:
            return g
    raise ValueError(f"RTL8821AU: 5G channel {channel} has no PG group")


def _training_word(bw20_idx: int) -> int:
    """[SRC] PHY_TxPowerTrainingByPath_8812: MCS7 (bw20) idx -10/-8/-6 cumulative, floored at 2."""
    pl = bw20_idx
    wd = 0
    for i, step in enumerate((10, 8, 6)):
        pl -= step
        wd |= (max(pl, 2) & 0xFF) << (i * 8)
    return wd


def set_tx_power(t, channel: int, pp: PathTxPwr) -> None:
    """[SRC] PHY_SetTxPowerLevel8812 (path A) — write the 2.4 GHz per-rate txagc + training."""
    g, cck_g = _ch_group_2g(channel)
    idx = {s: _pg_idx(pp, s, g, cck_g) for s in ("cck", "ofdm", "bw20")}
    for reg, section, n_bytes in _RATE_REGS:
        for b in range(n_bytes):
            set_bb(t, reg, _BYTE_MASK[b], idx[section])
    set_bb(t, _REG_A_TXPWR_TRAINING, 0x00FFFFFF, _training_word(idx["bw20"]))


def set_tx_power_5g(t, channel: int, pp: PathTxPwr) -> None:
    """[SRC] PHY_SetTxPowerLevel8812 (path A, 5 GHz) — per-rate txagc + training, no CCK."""
    g = _ch_group_5g(channel)
    idx = {"ofdm": _pg_idx(pp, "ofdm", g, 0), "bw20": _pg_idx(pp, "bw20", g, 0)}
    for reg, section, n_bytes in _RATE_REGS_5G:
        for b in range(n_bytes):
            set_bb(t, reg, _BYTE_MASK[b], idx[section])
    set_bb(t, _REG_A_TXPWR_TRAINING, 0x00FFFFFF, _training_word(idx["bw20"]))
