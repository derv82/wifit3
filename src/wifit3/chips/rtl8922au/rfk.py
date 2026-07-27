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
    H2C_FUNC_RFK_DPK_OFFLOAD, H2C_FUNC_RFK_TXGAPK_OFFLOAD, RTW89_BAND_2G,
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


def _tssi(t, chan: dict, tssi_mode: int, phy_idx: int) -> bytes:
    """rtw89_h2c_rf_tssi payload (300 bytes): the per-path CCK/OFDM de (efuse TSSI, trim 0 on the
    8922A), the thermal, and the 2G thermal-delta ftable. 2.4 GHz group-0 only. [SRC] fw.c:7600,
    fw.h:4960, phy.c:4793 fill_fwcmd_efuse_to_de / 4856 fill_fwcmd_tmeter_tbl."""
    if chan["band_type"] != RTW89_BAND_2G:
        raise NotImplementedError("rfk tssi 5/6G de + ftable not ported yet")
    cck, mcs, therm = t.tssi_cck, t.tssi_mcs, t.tssi_therm
    b = bytearray(300)
    struct.pack_into("<H", b, 0, 300)
    b[2], b[3], b[4], b[5], b[6], b[7] = phy_idx, chan["channel"], chan["band_width"], RTW89_BAND_2G, 1, t.cv
    def _put2(off, pair):
        b[off], b[off + 1] = pair[0] & 0xFF, pair[1] & 0xFF
    for off in (10, 12, 14):                                    # cck 20m/40m/efuse (base 0)
        _put2(off, cck)
    for off in (18, 20, 22, 24, 26, 28):                        # ofdm 20/40/80/160/320m + efuse
        _put2(off, mcs)
    b[40], b[41] = therm[0] & 0xFF, therm[1] & 0xFF             # pg_thermal
    b[42:170] = _TSSI_FTABLE_2G
    b[170:298] = _TSSI_FTABLE_2G
    b[298] = tssi_mode
    b[299] = t.rfe_type & 0xFF
    return bytes(b)

def _pre_ntfy(phy_idx: int, mlo_mode: int = MLO_1_PLUS_1_1RF, mlo_1_1: int = 1,
              ch: int = 0) -> bytes:
    """rtw89_fw_h2c_rf_pre_ntfy v2 payload: per-tbl chan + mlo mode/flag + phy_idx. init_late passes
    the cold defaults (ch 0, MLO_1_PLUS_1); the per-channel RFK passes the tuned channel + the
    set_channel MLO mode. [SRC] fw.c:7360, fw.h:4884."""
    b = bytearray(84)
    struct.pack_into("<I", b, 0, ch)                     # dbcc.ch[0][0]
    struct.pack_into("<I", b, 12, ch)                    # dbcc.ch[1][0]
    struct.pack_into("<I", b, 48, mlo_mode)              # common.mlo_mode
    struct.pack_into("<I", b, 52, ch)                    # tbl.cur_ch[0]
    struct.pack_into("<I", b, 56, ch)                    # tbl.cur_ch[1]
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


def _rxdck(phy_idx: int, ch: int = 0, is_chl_k: int = 0) -> bytes:
    """rtw89_fw_h2c_rf_rxdck: {len=9, phy, is_afe=0, kpath=RF_AB, band/bw=0, ch, dbg=0, is_chl_k}.
    is_chl_k is false for init_late, true for the per-channel RFK. [SRC] fw.c:7824."""
    return bytes((9, phy_idx, 0, 0x3, 0, 0, ch, 0, is_chl_k))


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
         _pre_ntfy(phy_idx, mode, 1 if t.mlo_1_1 else 0, ch))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_NOTIFY, H2C_FUNC_OUTSRC_RF_MCC_INFO,
         _pre_ntfy_mcc(mode, phy._chan_to_rf18_val(chan)))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_TXGAPK_OFFLOAD, _txgapk(phy_idx, band, bw, ch, t.cv))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_IQK_OFFLOAD,
         _iqk(phy_idx, band, bw, ch, t.cv, phy._get_kpath(t, phy_idx)))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_TSSI_OFFLOAD, _tssi(t, chan, RTW89_TSSI_NORMAL, phy_idx))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_DPK_OFFLOAD, _dpk(phy_idx, band, bw, ch))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_RXDCK_OFFLOAD, _rxdck(0, ch, 1))  # PHY_0 fixed
    mac.resume_sch_tx(t, mac_idx, tx_en)
    coex.ntfy_wl_rfk(t, ep, start=False)
