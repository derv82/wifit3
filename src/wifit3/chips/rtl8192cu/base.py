"""rtlwifi base.c: RX config init, the TX rate/tcb descriptor helpers, action-frame filtering,
and the 2 s watchdog work."""
from __future__ import annotations

from . import constants as C
from .priv import Ieee80211TxInfo, RtlPriv, RtlTcbDesc
from .wifi import is_hal_stop

rtl_ratetable_2g_hw_value = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b)
"""rtl_ratetable_2g[].hw_value (base.c:85): 1, 2, 5.5, 11, 6, 9, 12, 18, 24, 36, 48, 54 Mbps."""


def ieee80211_is_data(fc: int) -> bool:
    return (fc & C.IEEE80211_FCTL_FTYPE) == C.IEEE80211_FTYPE_DATA


def ieee80211_is_mgmt(fc: int) -> bool:
    return (fc & C.IEEE80211_FCTL_FTYPE) == C.IEEE80211_FTYPE_MGMT


def ieee80211_is_ctl(fc: int) -> bool:
    return (fc & C.IEEE80211_FCTL_FTYPE) == C.IEEE80211_FTYPE_CTL


def ieee80211_is_data_qos(fc: int) -> bool:
    mask = C.IEEE80211_FCTL_FTYPE | C.IEEE80211_STYPE_QOS_DATA
    return (fc & mask) == (C.IEEE80211_FTYPE_DATA | C.IEEE80211_STYPE_QOS_DATA)


def _is_ftype_stype(fc: int, ftype: int, stype: int) -> bool:
    return (fc & (C.IEEE80211_FCTL_FTYPE | C.IEEE80211_FCTL_STYPE)) == (ftype | stype)


def ieee80211_is_beacon(fc: int) -> bool:
    return _is_ftype_stype(fc, C.IEEE80211_FTYPE_MGMT, C.IEEE80211_STYPE_BEACON)


def ieee80211_is_action(fc: int) -> bool:
    return _is_ftype_stype(fc, C.IEEE80211_FTYPE_MGMT, C.IEEE80211_STYPE_ACTION)


def ieee80211_is_nullfunc(fc: int) -> bool:
    return _is_ftype_stype(fc, C.IEEE80211_FTYPE_DATA, C.IEEE80211_STYPE_NULLFUNC)


def ieee80211_has_pm(fc: int) -> bool:
    return bool(fc & C.IEEE80211_FCTL_PM)


def ieee80211_get_DA(hdr: bytes) -> bytes:
    """include/linux/ieee80211.h: addr3 for a ToDS frame, else addr1."""
    fc = int.from_bytes(hdr[0:2], "little")
    return bytes(hdr[16:22]) if fc & C.IEEE80211_FCTL_TODS else bytes(hdr[4:10])


def is_multicast_ether_addr(addr: bytes) -> bool:
    return bool(addr[0] & 0x01)


def is_broadcast_ether_addr(addr: bytes) -> bool:
    return addr == b"\xff" * 6


def rtl_init_rx_config(rtlpriv: RtlPriv) -> None:
    """base.c:590."""
    rtlpriv.mac80211.rx_conf = rtlpriv.cfg.ops.get_hw_reg(rtlpriv, C.HW_VAR_RCR)


def _rtl_qurey_shortpreamble_mode(tcb_desc: RtlTcbDesc, info: Ieee80211TxInfo) -> None:
    """base.c:604."""
    tcb_desc.use_shortpreamble = False
    if tcb_desc.hw_rate == C.HalCfgMaps.RTL_RC_CCK_RATE1M:
        return
    if info.rate_flags & C.IEEE80211_TX_RC_USE_SHORT_PREAMBLE:
        tcb_desc.use_shortpreamble = True


def _rtl_query_shortgi(tcb_desc: RtlTcbDesc) -> None:
    """base.c:622 with no STA: short GI stays off."""
    tcb_desc.use_shortgi = False


def _rtl_query_protection_mode(tcb_desc: RtlTcbDesc, info: Ieee80211TxInfo) -> None:
    """base.c:674."""
    tcb_desc.rts_stbc = False
    tcb_desc.cts_enable = False
    tcb_desc.rts_sc = 0
    tcb_desc.rts_bw = False
    tcb_desc.rts_use_shortpreamble = False
    tcb_desc.rts_use_shortgi = False
    if info.rate_flags & C.IEEE80211_TX_RC_USE_CTS_PROTECT:
        tcb_desc.rts_enable = True
        tcb_desc.cts_enable = True
        tcb_desc.rts_rate = C.HalCfgMaps.RTL_RC_OFDM_RATE24M
    elif info.rate_flags & C.IEEE80211_TX_RC_USE_RTS_CTS:
        tcb_desc.rts_enable = True
        tcb_desc.rts_rate = C.HalCfgMaps.RTL_RC_OFDM_RATE24M


def _rtl_txrate_selectmode(rtlpriv: RtlPriv, tcb_desc: RtlTcbDesc) -> None:
    """base.c:773 with no STA. rtl8192cu sets no RTL_SPEC_NEW_RATEID, so SET_RATE_ID is the identity."""
    mac = rtlpriv.mac80211
    ratr_index = C.RATR_INX_WIRELESS_MC
    if not tcb_desc.disable_ratefallback or not tcb_desc.use_driver_rate:
        if mac.opmode == C.NL80211_IFTYPE_STATION:
            tcb_desc.ratr_index = 0
        elif mac.opmode in (C.NL80211_IFTYPE_ADHOC, C.NL80211_IFTYPE_MESH_POINT):
            if tcb_desc.multicast or tcb_desc.broadcast:
                tcb_desc.hw_rate = C.HalCfgMaps.RTL_RC_CCK_RATE2M
                tcb_desc.use_driver_rate = True
                tcb_desc.ratr_index = C.RATR_INX_WIRELESS_MC
            else:
                tcb_desc.ratr_index = ratr_index
        elif mac.opmode == C.NL80211_IFTYPE_AP:
            tcb_desc.ratr_index = ratr_index
    if rtlpriv.dm.useramask:
        tcb_desc.ratr_index = ratr_index
        if mac.opmode in (C.NL80211_IFTYPE_STATION, C.NL80211_IFTYPE_MESH_POINT):
            tcb_desc.mac_id = 0
            if mac.mode == C.WIRELESS_MODE_AC_5G:
                tcb_desc.ratr_index = C.RATR_INX_WIRELESS_AC_5N
            elif mac.mode == C.WIRELESS_MODE_AC_24G:
                tcb_desc.ratr_index = C.RATR_INX_WIRELESS_AC_24N
            elif mac.mode == C.WIRELESS_MODE_N_24G:
                tcb_desc.ratr_index = C.RATR_INX_WIRELESS_NGB
            elif mac.mode == C.WIRELESS_MODE_N_5G:
                tcb_desc.ratr_index = C.RATR_INX_WIRELESS_NG
            elif mac.mode & C.WIRELESS_MODE_G:
                tcb_desc.ratr_index = C.RATR_INX_WIRELESS_GB
            elif mac.mode & C.WIRELESS_MODE_B:
                tcb_desc.ratr_index = C.RATR_INX_WIRELESS_B
            elif mac.mode & C.WIRELESS_MODE_A:
                tcb_desc.ratr_index = C.RATR_INX_WIRELESS_G
        elif mac.opmode in (C.NL80211_IFTYPE_AP, C.NL80211_IFTYPE_ADHOC):
            tcb_desc.mac_id = 0


def _rtl_query_bandwidth_mode(tcb_desc: RtlTcbDesc) -> None:
    """base.c:860 with no STA: 20 MHz."""
    tcb_desc.packet_bw = False


def _rtl_get_tx_hw_rate(info: Ieee80211TxInfo) -> int:
    """base.c:1192. rtl8192cu never leaves BAND_ON_2_4G, so the 5 GHz floor does not apply."""
    if info.rate_flags & C.IEEE80211_TX_RC_MCS:
        return C.HalCfgMaps.RTL_RC_HT_RATEMCS15 - 15 + info.rate_idx
    if 0 <= info.rate_idx < len(rtl_ratetable_2g_hw_value):
        return rtl_ratetable_2g_hw_value[info.rate_idx]
    return 0x0


def rtl_get_tcb_desc(rtlpriv: RtlPriv, info: Ieee80211TxInfo, hdr: bytes, tcb_desc: RtlTcbDesc) -> None:
    """base.c:1228 with no STA (injected frames have none), so the HT/VHT highest-rate picks are skipped."""
    mac = rtlpriv.mac80211
    fc = int.from_bytes(hdr[0:2], "little")
    tcb_desc.hw_rate = _rtl_get_tx_hw_rate(info)
    if ieee80211_is_data(fc):
        if info.rate_idx == 0 or ieee80211_is_nullfunc(fc):
            tcb_desc.use_driver_rate = True
            tcb_desc.ratr_index = C.RATR_INX_WIRELESS_MC
            tcb_desc.disable_ratefallback = True
        elif mac.mode == C.WIRELESS_MODE_B:
            tcb_desc.hw_rate = C.HalCfgMaps.RTL_RC_CCK_RATE11M
        else:
            tcb_desc.hw_rate = C.HalCfgMaps.RTL_RC_OFDM_RATE54M
        addr1 = bytes(hdr[4:10])
        if is_multicast_ether_addr(addr1):
            tcb_desc.multicast = True
        elif is_broadcast_ether_addr(addr1):
            tcb_desc.broadcast = True
        _rtl_txrate_selectmode(rtlpriv, tcb_desc)
        _rtl_query_bandwidth_mode(tcb_desc)
        _rtl_qurey_shortpreamble_mode(tcb_desc, info)
        _rtl_query_shortgi(tcb_desc)
        _rtl_query_protection_mode(tcb_desc, info)
    else:
        tcb_desc.use_driver_rate = True
        tcb_desc.ratr_index = C.RATR_INX_WIRELESS_MC
        tcb_desc.disable_ratefallback = True
        tcb_desc.mac_id = 0
        tcb_desc.packet_bw = False


def rtlwifi_rate_mapping(isht: bool, desc_rate: int) -> int:
    """base.c:986 for the 2.4 GHz band, the only one rtl8192cu tunes; rtl92cu passes isvht false."""
    if not isht:
        if C.DESC_RATE1M <= desc_rate <= C.DESC_RATE54M:
            return desc_rate - C.DESC_RATE1M
        return 0
    if C.DESC_RATEMCS0 <= desc_rate <= C.DESC_RATEMCS15:
        return desc_rate - C.DESC_RATEMCS0
    return 0


def rtl_action_proc(rtlpriv: RtlPriv, skb: bytes, is_tx: bool) -> bool:
    """base.c:1367. A received ADDBA request finds no STA (this port keeps no station table) and passes."""
    fc = int.from_bytes(skb[0:2], "little")
    if not ieee80211_is_action(fc):
        return True
    act = skb[C.MAC80211_3ADDR_LEN:C.MAC80211_3ADDR_LEN + 2]
    if len(act) < 2:
        return True
    if act[0] == C.ACT_CAT_BA and act[1] == C.ACT_ADDBAREQ and rtlpriv.mac80211.act_scanning:
        return False
    return True


def rtl_watchdog_wq_callback(rtlpriv: RtlPriv) -> None:
    """base.c:2036, the steps a non-linked (monitor) interface runs. The linked traffic/LPS
    and roaming blocks are STA-only; the scan-list and ack-queue expiry touch no register."""
    if is_hal_stop(rtlpriv.rtlhal):
        return
    if not rtlpriv.cfg.mod_params.disable_watchdog:
        rtlpriv.cfg.ops.dm_watchdog(rtlpriv)
