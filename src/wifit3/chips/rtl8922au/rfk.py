"""RTL8922AU RF calibration (RFK), ported from rtw89-7.2 rtw8922a_rfk.c / phy.c / fw.c.

rfk_init_late runs the boot-time DACK + RXDCK per phy, all as firmware offload H2Cs (the driver
sends the command and the fw does the calibration). The per-channel RFK (IQK/DPK/TSSI) comes with
set_channel. [SRC] rtw8922a.c:2349-2367.
"""
import struct

from .constants import (
    H2C_CAT_OUTSRC, H2C_CL_OUTSRC_RF_FW_RFK, H2C_CL_OUTSRC_RF_FW_NOTIFY,
    H2C_FUNC_RFK_PRE_NOTIFY, H2C_FUNC_RFK_DACK_OFFLOAD, H2C_FUNC_RFK_RXDCK_OFFLOAD,
    H2C_FUNC_OUTSRC_RF_MCC_INFO, H2C_FUNC_RFK_TSSI_OFFLOAD, H2C_FUNC_RFK_IQK_OFFLOAD,
    H2C_FUNC_RFK_DPK_OFFLOAD, H2C_FUNC_RFK_TXGAPK_OFFLOAD, RTW89_BAND_2G, RTW89_BAND_5G,
    RTW89_TSSI_SCAN, RTW89_TSSI_NORMAL, MLO_1_PLUS_1_1RF, MLO_2_PLUS_0_1RF,
    RF_PATH_A, RF_PATH_B, RF_AB, RR_MOD, RR_MOD_MASK,
)
from . import firmware, phy, mac, coex

# rtw89_phy_rfk_tssi_fill_fwcmd_tmeter_tbl for the 2.4 GHz subband, both paths identical. This is
# built from the TXPWR_TRK firmware element (id 18) run through the phy.c:4950-4970 transform; for
# 2.4 GHz on this card it reduces to a constant. [SRC] phy.c:4856-4978.
_TSSI_FTABLE_2G = bytes.fromhex(
    "00000000010101000101010101010101010101010101010101010101010101010101010101010101"
    "010101010101010101010101010101010101010101010101"
    "fdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfd"
    "fefefdfdfffffefeffffffffffffffffffffffff0000ffff"
)


def _cck_group_2g(ch: int) -> int:
    """phy_tssi_get_cck_group, 2.4 GHz branch. [SRC] phy.c:4351."""
    if ch <= 2:
        return 0
    if ch <= 5:
        return 1
    if ch <= 8:
        return 2
    if ch <= 11:
        return 3
    if ch <= 13:
        return 4
    return 5                                          # ch 14


def _ofdm_group_2g(ch: int) -> int:
    """phy_tssi_get_ofdm_group, 2.4 GHz branch (no EXTRA/interpolation groups here). [SRC] phy.c:4379."""
    if ch <= 2:
        return 0
    if ch <= 5:
        return 1
    if ch <= 8:
        return 2
    if ch <= 11:
        return 3
    return 4                                          # ch 12-14


# 5G thermal-delta ftables by sub-band (extracted from the wire like _TSSI_FTABLE_2G). [SRC] phy.c:4856.
_TSSI_FTABLE_5G_B1 = bytes.fromhex(
    "00000000010101010101010101010101020101010202020203030303030303030303030303030303"
    "030303030303030303030303030303030303030303030303fdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfd"
    "fdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfefefefdfffffffe"
    "ffffffff000000ff")
_TSSI_FTABLE_5G_B3 = bytes.fromhex(
    "00000000010000000101010102020101040403030505040405050505050505050505050505050505"
    "050505050505050505050505050505050505050505050505fefefefefefefefefefefefefefefefe"
    "fefefefefefefefefefefefefefefefefefefefefefefefefefefefefffffefeffffffffffffffff"
    "ffffffff000000ff")
_TSSI_FTABLE_5G_B4 = bytes.fromhex(
    "00000000010101010101010102020201020202020202020202020202020202020202020202020202"
    "020202020202020202020202020202020202020202020202fdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfd"
    "fdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfdfefefefdfefefefeffffffff"
    "ffffffff00000000")


def _s8(v: int) -> int:
    return v - 256 if v & 0x80 else v


# phy_tssi_get_ofdm_group 5G: (lo, hi, group); EXTRA groups have _TSSI_EXTRA set and interpolate.
# [SRC] phy.c:4392, 4371 (PHY_TSSI_EXTRA_GROUP = BIT(31) | idx).
_TSSI_EXTRA = 1 << 31
_OFDM_GROUP_5G = (
    (36, 40, 5), (41, 43, _TSSI_EXTRA | 5), (44, 48, 6), (49, 51, _TSSI_EXTRA | 6),
    (52, 56, 7), (57, 59, _TSSI_EXTRA | 7), (60, 64, 8),
    (100, 104, 9), (105, 107, _TSSI_EXTRA | 9), (108, 112, 10), (113, 115, _TSSI_EXTRA | 10),
    (116, 120, 11), (121, 123, _TSSI_EXTRA | 11), (124, 128, 12), (129, 131, _TSSI_EXTRA | 12),
    (132, 136, 13), (137, 139, _TSSI_EXTRA | 13), (140, 144, 14),
    (149, 153, 15), (154, 156, _TSSI_EXTRA | 15), (157, 161, 16), (162, 164, _TSSI_EXTRA | 16),
    (165, 169, 17), (170, 172, _TSSI_EXTRA | 17), (173, 177, 18),
)


def _ofdm_de_5g(mcs_row: list, ch: int) -> int:
    """phy_tssi_get_ofdm_de 5G: tssi_mcs[gidx], or the (truncated) mean of two adjacent groups for
    an EXTRA sub-band. [SRC] phy.c:4651."""
    g = next((grp for lo, hi, grp in _OFDM_GROUP_5G if lo <= ch <= hi), 0)
    if g & _TSSI_EXTRA:
        i = g & ~_TSSI_EXTRA
        return int((_s8(mcs_row[i]) + _s8(mcs_row[i + 1])) / 2) & 0xFF
    return mcs_row[g] & 0xFF


def _ftable_5g(ch: int) -> bytes:
    """The 5G thermal-delta ftable by sub-band (BAND_1 36-64, BAND_3 100-144, BAND_4 149+)."""
    if ch <= 64:
        return _TSSI_FTABLE_5G_B1
    if ch <= 144:
        return _TSSI_FTABLE_5G_B3
    return _TSSI_FTABLE_5G_B4


def _tssi(t, chan: dict, tssi_mode: int, phy_idx: int) -> bytes:
    """rtw89_h2c_rf_tssi payload (300 bytes): the per-path CCK/OFDM de (efuse TSSI by channel group,
    trim 0 on the 8922A), the thermal, and the 2G thermal-delta ftable. 2.4 GHz only. [SRC] fw.c:7600,
    fw.h:4960, phy.c:4793 fill_fwcmd_efuse_to_de / 4856 fill_fwcmd_tmeter_tbl."""
    band, ch = chan["band_type"], chan["channel"]
    if band == RTW89_BAND_2G:
        cg, og = _cck_group_2g(ch), _ofdm_group_2g(ch)
        cck = [t.tssi_cck[0][cg], t.tssi_cck[1][cg]]  # trim_de is 0 on the 8922A, so de = efuse byte
        mcs = [t.tssi_mcs[0][og], t.tssi_mcs[1][og]]
        ftable = _TSSI_FTABLE_2G
    elif band == RTW89_BAND_5G:
        cck = [t.tssi_cck[0][0], t.tssi_cck[1][0]]    # cck_group folds to 0 for 5G
        mcs = [_ofdm_de_5g(t.tssi_mcs[0], ch), _ofdm_de_5g(t.tssi_mcs[1], ch)]
        ftable = _ftable_5g(ch)
    else:
        raise NotImplementedError("rfk tssi 6G de + ftable not ported yet")
    therm = t.tssi_therm
    b = bytearray(300)
    struct.pack_into("<H", b, 0, 300)
    b[2], b[3], b[4], b[5], b[6], b[7] = phy_idx, ch, chan["band_width"], band, 1, t.cv
    def _put2(off, pair):
        b[off], b[off + 1] = pair[0] & 0xFF, pair[1] & 0xFF
    for off in (10, 12, 14):                                    # cck 20m/40m/efuse (base 0)
        _put2(off, cck)
    for off in (18, 20, 22, 24, 26, 28):                        # ofdm 20/40/80/160/320m + efuse
        _put2(off, mcs)
    b[40], b[41] = therm[0] & 0xFF, therm[1] & 0xFF             # pg_thermal
    b[42:170] = ftable
    b[170:298] = ftable
    b[298] = tssi_mode
    b[299] = t.rfe_type & 0xFF
    return bytes(b)

def _pre_ntfy(phy_idx: int, mlo_mode: int = MLO_1_PLUS_1_1RF, mlo_1_1: int = 1,
              ch: int = 0, band: int = 0) -> bytes:
    """rtw89_fw_h2c_rf_pre_ntfy v2 payload: per-tbl chan/band + mlo mode/flag + phy_idx. init_late
    passes the cold defaults (ch 0, band 2G, MLO_1_PLUS_1); the per-channel RFK passes the tuned
    channel + band + the set_channel MLO mode. [SRC] fw.c:7360, fw.h:4884."""
    b = bytearray(84)
    struct.pack_into("<I", b, 0, ch)                     # dbcc.ch[0][0]
    struct.pack_into("<I", b, 12, ch)                    # dbcc.ch[1][0]
    struct.pack_into("<I", b, 24, band)                  # dbcc.band[0][0]
    struct.pack_into("<I", b, 36, band)                  # dbcc.band[1][0]
    struct.pack_into("<I", b, 48, mlo_mode)              # common.mlo_mode
    struct.pack_into("<I", b, 52, ch)                    # tbl.cur_ch[0]
    struct.pack_into("<I", b, 56, ch)                    # tbl.cur_ch[1]
    struct.pack_into("<I", b, 60, band)                  # tbl.cur_band[0]
    struct.pack_into("<I", b, 64, band)                  # tbl.cur_band[1]
    struct.pack_into("<I", b, 68, phy_idx)               # common.phy_idx
    struct.pack_into("<I", b, 72, mlo_1_1)               # v1.mlo_1_1 (is_mlo_1_1)
    return bytes(b)


def _pre_ntfy_mcc(mlo_mode: int = MLO_1_PLUS_1_1RF, rf18: int = 0) -> bytes:
    """rtw89_fw_h2c_rf_pre_ntfy_mcc v0: per-tbl chan_to_rf18 values + mlo mode. [SRC] fw.c:7489,
    fw.h:4937."""
    b = bytearray(36)
    struct.pack_into("<I", b, 0, rf18)                   # tbl_18[0][0]
    struct.pack_into("<I", b, 4, rf18)                   # tbl_18[0][1]
    struct.pack_into("<I", b, 24, rf18)                  # cur_18[0]
    struct.pack_into("<I", b, 28, rf18)                  # cur_18[1]
    struct.pack_into("<I", b, 32, mlo_mode)
    return bytes(b)


def _dack(phy_idx: int) -> bytes:
    """rtw89_fw_h2c_rf_dack: {len=3, phy, type=0}. [SRC] fw.c:7787."""
    return bytes((3, phy_idx, 0))


def _rxdck(phy_idx: int, ch: int = 0, is_chl_k: int = 0, band: int = 0, bw: int = 0) -> bytes:
    """rtw89_fw_h2c_rf_rxdck: {len=9, phy, is_afe=0, kpath=RF_AB, band, bw, ch, dbg=0, is_chl_k}.
    is_chl_k is false for init_late, true for the per-channel RFK. [SRC] fw.c:7824."""
    return bytes((9, phy_idx, 0, 0x3, band, bw, ch, 0, is_chl_k))


def _txgapk(phy_idx: int, band: int, bw: int, ch: int, cv: int) -> bytes:
    """rtw89_fw_h2c_rf_txgapk: {len=8, ktype=2, phy, kpath=RF_AB, band, bw, ch, cv}. [SRC] fw.c:7744."""
    return bytes((8, 2, phy_idx, RF_AB, band, bw, ch, cv))


def _iqk(phy_idx: int, band: int, bw: int, ch: int, cv: int, kpath: int) -> bytes:
    """rtw89_fw_h2c_rf_iqk: {len=8, ktype=0, phy, kpath, band, bw, ch, cv}. Unlike the other RFK
    H2Cs, iqk's kpath is rtw89_phy_get_kpath (per MLO mode), not RF_AB. [SRC] fw.c:7641,7678."""
    return bytes((8, 0, phy_idx, kpath, band, bw, ch, cv))


def _dpk(phy_idx: int, band: int, bw: int, ch: int) -> bytes:
    """rtw89_fw_h2c_rf_dpk: {len=8, phy, dpk_enable=1, kpath=RF_AB, band, bw, ch, dbg=0}. [SRC]
    fw.c:7702."""
    return bytes((8, phy_idx, 1, RF_AB, band, bw, ch, 0))


def _wait_rx_mode(t, kpath: int) -> None:
    """_wait_rx_mode: poll RR_MOD per path until it leaves RX mode (value != 2). At cold boot the
    first read already satisfies it. [SRC] rtw8922a_rfk.c / rtw8852a_rfk.c:92."""
    for path in (RF_PATH_A, RF_PATH_B):
        if not (kpath & (1 << path)):
            continue
        for _ in range(2500):
            if phy.read_rf(t, path, RR_MOD, RR_MOD_MASK) != 2:
                break


def _h2c(t, ep: int, cls: int, func: int, payload: bytes) -> None:
    firmware.h2c_command(t, ep, H2C_CAT_OUTSRC, cls, func, payload, rack=False, dack=False)


def _init_late_one(t, ep: int, phy_idx: int) -> None:
    """__rtw8922a_rfk_init_late(phy): pre-notify (+mcc), DACK, RXDCK, all fw offload. [SRC]
    rtw8922a.c:2349."""
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_PRE_NOTIFY, _pre_ntfy(phy_idx))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_NOTIFY, H2C_FUNC_OUTSRC_RF_MCC_INFO, _pre_ntfy_mcc())
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_DACK_OFFLOAD, _dack(phy_idx))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_RXDCK_OFFLOAD, _rxdck(phy_idx))


def rfk_init_late(t, ep: int) -> None:
    """rtw8922a_rfk_init_late: DACK + RXDCK for PHY_0 then (DBCC) PHY_1. [SRC] rtw8922a.c:2360."""
    _init_late_one(t, ep, 0)
    _init_late_one(t, ep, 1)


def rfk_band_changed(t, ep: int, chan: dict, phy_idx: int) -> None:
    """rtw8922a_rfk_band_changed: rtw89_phy_rfk_tssi_and_wait(RTW89_TSSI_SCAN). [SRC] rtw8922a.c:2412."""
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_TSSI_OFFLOAD, _tssi(t, chan, RTW89_TSSI_SCAN, phy_idx))


def rfk_channel(t, ep: int, chan: dict, phy_idx: int) -> None:
    """rtw8922a_rfk_channel (pure-monitor vif): wl_rfk START, stop sch-tx, wait RX idle, then the
    pre_ntfy / txgapk / iqk / tssi(NORMAL) / dpk / rxdck fw offloads, then resume + wl_rfk STOP. The
    per-H2C report waits are completion-based (no wire ops). [SRC] rtw8922a.c:2388."""
    mac_idx = phy_idx
    mode = MLO_1_PLUS_1_1RF if t.mlo_1_1 else MLO_2_PLUS_0_1RF
    band, bw, ch = chan["band_type"], chan["band_width"], chan["channel"]
    coex.ntfy_wl_rfk(t, ep, start=True)
    tx_en = mac.stop_sch_tx(t, mac_idx)
    _wait_rx_mode(t, RF_AB)
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_PRE_NOTIFY,
         _pre_ntfy(phy_idx, mode, 1 if t.mlo_1_1 else 0, ch, band))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_NOTIFY, H2C_FUNC_OUTSRC_RF_MCC_INFO,
         _pre_ntfy_mcc(mode, phy._chan_to_rf18_val(chan)))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_TXGAPK_OFFLOAD, _txgapk(phy_idx, band, bw, ch, t.cv))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_IQK_OFFLOAD,
         _iqk(phy_idx, band, bw, ch, t.cv, phy._get_kpath(t, phy_idx)))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_TSSI_OFFLOAD, _tssi(t, chan, RTW89_TSSI_NORMAL, phy_idx))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_DPK_OFFLOAD, _dpk(phy_idx, band, bw, ch))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_RXDCK_OFFLOAD,
         _rxdck(0, ch, 1, band, bw))  # PHY_0 fixed
    mac.resume_sch_tx(t, mac_idx, tx_en)
    coex.ntfy_wl_rfk(t, ep, start=False)
