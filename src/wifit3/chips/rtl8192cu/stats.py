"""rtlwifi stats.c: RX power/EVM percentages and the per-BSSID signal smoothing."""
from __future__ import annotations

from . import constants as C
from .priv import RtlPriv, RtlStats


def rtl_query_rxpwrpercentage(antpower: int) -> int:
    """stats.c:8."""
    if antpower <= -100 or antpower >= 20:
        return 0
    if antpower >= 0:
        return 100
    return 100 + antpower


def rtl_evm_db_to_percentage(value: int) -> int:
    """stats.c:19."""
    ret_val = min(max(-value, 0), 33) * 3
    if ret_val == 99:
        ret_val = 100
    return ret_val


def rtl_translate_todbm(signal_strength_index: int) -> int:
    """stats.c:30."""
    signal_power = (signal_strength_index + 1) >> 1
    signal_power -= 95
    return signal_power


def rtl_signal_scale_mapping(currsig: int) -> int:
    """stats.c:40."""
    if 61 <= currsig <= 100:
        return 90 + (currsig - 60) // 4
    if 41 <= currsig <= 60:
        return 78 + (currsig - 40) // 2
    if 31 <= currsig <= 40:
        return 66 + (currsig - 30)
    if 21 <= currsig <= 30:
        return 54 + (currsig - 20)
    if 5 <= currsig <= 20:
        return 42 + ((currsig - 5) * 2) // 3
    if currsig == 4:
        return 36
    if currsig == 3:
        return 27
    if currsig == 2:
        return 18
    if currsig == 1:
        return 9
    return currsig


def _c_div(numerator: int, denominator: int) -> int:
    """C integer division: truncates toward zero."""
    quotient = abs(numerator) // abs(denominator)
    return quotient if (numerator >= 0) == (denominator > 0) else -quotient


def rtl_process_ui_rssi(rtlpriv: RtlPriv, pstatus: RtlStats) -> None:
    """stats.c:69."""
    stats = rtlpriv.stats
    rtlphy = rtlpriv.phy
    if not pstatus.packet_toself and not pstatus.packet_beacon:
        return
    stats.pwdb_all_cnt += pstatus.rx_pwdb_all
    stats.rssi_calculate_cnt += 1
    ui_rssi = stats.ui_rssi
    total_num = ui_rssi.total_num
    ui_rssi.total_num += 1
    if total_num >= C.PHY_RSSI_SLID_WIN_MAX:
        ui_rssi.total_num = C.PHY_RSSI_SLID_WIN_MAX
        last_rssi = ui_rssi.elements[ui_rssi.index]
        ui_rssi.total_val -= last_rssi
    ui_rssi.total_val += pstatus.signalstrength
    ui_rssi.elements[ui_rssi.index] = pstatus.signalstrength
    ui_rssi.index += 1
    if ui_rssi.index >= C.PHY_RSSI_SLID_WIN_MAX:
        ui_rssi.index = 0
    tmpval = ui_rssi.total_val // ui_rssi.total_num
    stats.signal_strength = rtl_translate_todbm(tmpval & 0xFF)
    pstatus.rssi = stats.signal_strength
    if pstatus.is_cck:
        return
    for rfpath in range(C.RF90_PATH_A, rtlphy.num_total_rfpath):
        if stats.rx_rssi_percentage[rfpath] == 0:
            stats.rx_rssi_percentage[rfpath] = pstatus.rx_mimo_signalstrength[rfpath]
        if pstatus.rx_mimo_signalstrength[rfpath] > stats.rx_rssi_percentage[rfpath]:
            stats.rx_rssi_percentage[rfpath] = (
                (stats.rx_rssi_percentage[rfpath] * (C.RX_SMOOTH_FACTOR - 1))
                + pstatus.rx_mimo_signalstrength[rfpath]) // C.RX_SMOOTH_FACTOR
            stats.rx_rssi_percentage[rfpath] = (stats.rx_rssi_percentage[rfpath] + 1) & 0xFF
        else:
            stats.rx_rssi_percentage[rfpath] = (
                (stats.rx_rssi_percentage[rfpath] * (C.RX_SMOOTH_FACTOR - 1))
                + pstatus.rx_mimo_signalstrength[rfpath]) // C.RX_SMOOTH_FACTOR
        stats.rx_snr_db[rfpath] = pstatus.rx_snr[rfpath]
        stats.rx_evm_dbm[rfpath] = pstatus.rx_mimo_evm_dbm[rfpath]
        stats.rx_cfo_short[rfpath] = pstatus.cfo_short[rfpath]
        stats.rx_cfo_tail[rfpath] = pstatus.cfo_tail[rfpath]


def rtl_update_rxsignalstatistics(rtlpriv: RtlPriv, pstatus: RtlStats) -> None:
    """stats.c:135."""
    stats = rtlpriv.stats
    weighting = 0
    if stats.recv_signal_power == 0:
        stats.recv_signal_power = pstatus.recvsignalpower
    if pstatus.recvsignalpower > stats.recv_signal_power:
        weighting = 5
    elif pstatus.recvsignalpower < stats.recv_signal_power:
        weighting = -5
    stats.recv_signal_power = _c_div(stats.recv_signal_power * 5 + pstatus.recvsignalpower + weighting, 6)


def rtl_process_pwdb(rtlpriv: RtlPriv, pstatus: RtlStats) -> None:
    """stats.c:151. rtl_find_sta finds no station: this port keeps no station table (AP/ADHOC)."""
    undec_sm_pwdb = rtlpriv.dm.undec_sm_pwdb
    if undec_sm_pwdb < 0:
        undec_sm_pwdb = pstatus.rx_pwdb_all
    if pstatus.rx_pwdb_all > undec_sm_pwdb:
        undec_sm_pwdb = (undec_sm_pwdb * (C.RX_SMOOTH_FACTOR - 1) + pstatus.rx_pwdb_all) // C.RX_SMOOTH_FACTOR
        undec_sm_pwdb = undec_sm_pwdb + 1
    else:
        undec_sm_pwdb = (undec_sm_pwdb * (C.RX_SMOOTH_FACTOR - 1) + pstatus.rx_pwdb_all) // C.RX_SMOOTH_FACTOR
    rtlpriv.dm.undec_sm_pwdb = undec_sm_pwdb
    rtl_update_rxsignalstatistics(rtlpriv, pstatus)


def rtl_process_ui_link_quality(rtlpriv: RtlPriv, pstatus: RtlStats) -> None:
    """stats.c:193."""
    stats = rtlpriv.stats
    if pstatus.signalquality == 0:
        return
    quality = stats.ui_link_quality
    total_num = quality.total_num
    quality.total_num += 1
    if total_num >= C.PHY_LINKQUALITY_SLID_WIN_MAX:
        quality.total_num = C.PHY_LINKQUALITY_SLID_WIN_MAX
        last_evm = quality.elements[quality.index]
        quality.total_val -= last_evm
    quality.total_val += pstatus.signalquality
    quality.elements[quality.index] = pstatus.signalquality
    quality.index += 1
    if quality.index >= C.PHY_LINKQUALITY_SLID_WIN_MAX:
        quality.index = 0
    tmpval = quality.total_val // quality.total_num
    stats.signal_quality = tmpval
    stats.last_sigstrength_inpercent = tmpval
    for n_stream in range(2):
        if pstatus.rx_mimo_sig_qual[n_stream] != -1:
            if stats.rx_evm_percentage[n_stream] == 0:
                stats.rx_evm_percentage[n_stream] = pstatus.rx_mimo_sig_qual[n_stream] & 0xFF
            stats.rx_evm_percentage[n_stream] = (
                (stats.rx_evm_percentage[n_stream] * (C.RX_SMOOTH_FACTOR - 1))
                + pstatus.rx_mimo_sig_qual[n_stream] * 1) // C.RX_SMOOTH_FACTOR & 0xFF


def rtl_process_phyinfo(rtlpriv: RtlPriv, pstatus: RtlStats) -> None:
    """stats.c:236."""
    if not pstatus.packet_matchbssid:
        return
    rtl_process_ui_rssi(rtlpriv, pstatus)
    rtl_process_pwdb(rtlpriv, pstatus)
    rtl_process_ui_link_quality(rtlpriv, pstatus)
