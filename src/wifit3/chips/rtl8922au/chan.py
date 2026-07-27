"""RTL8922AU channel tuning (rtw8922a_set_channel).

Builds the rtw89_chan for a channel and runs the per-channel BB/RF/MAC tune plus RFK, the unit
airmon-ng drives once per hop. Only the head (pre_set_channel_bb) is ported so far; the rest are
marked TODO. [SRC] core.c:531 __rtw89_set_channel, rtw8922a.c:2232 set_channel.
"""
from . import mac, phy, txpwr, coex, rfk
from .constants import RTW89_BAND_2G, RTW89_BAND_5G, RTW89_BAND_6G, RTW89_CHANNEL_WIDTH_20


def band_of(channel: int) -> int:
    """The band a channel number lives in. airmon-ng tunes 2.4 GHz (1-14) and 5 GHz (36+)."""
    return RTW89_BAND_2G if channel <= 14 else RTW89_BAND_5G


def channel_to_freq(channel: int, band: int) -> int:
    """IEEE center frequency (MHz) for a channel. [SRC] ieee80211_channel_to_frequency."""
    if band == RTW89_BAND_2G:
        return 2484 if channel == 14 else 2407 + channel * 5
    if band == RTW89_BAND_6G:
        return 5950 + channel * 5
    return 5000 + channel * 5


def freq_to_channel(freq: int) -> int:
    """Inverse of channel_to_freq, for the verify harness peeking at the R_FC0 write."""
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq >= 5955:
        return (freq - 5950) // 5
    return (freq - 5000) // 5


def make_chan(channel: int, bandwidth: int = RTW89_CHANNEL_WIDTH_20) -> dict:
    """rtw89_chan_create for a monitor tune: primary == center (20 MHz). [SRC] chan.c:131."""
    band = band_of(channel)
    return {"channel": channel, "primary_channel": channel, "band_type": band,
            "band_width": bandwidth, "freq": channel_to_freq(channel, band), "pri_sb_idx": 0}


def _set_channel_one(t, ep: int, chan: dict, phy_idx: int, mac_idx: int) -> None:
    """__rtw89_set_channel for one PHY: help(enter), set_channel mac/bb/rf, set_txpwr, help(leave),
    then (first tune / band change) btc_switch_band + rfk_band_changed, then the pure-monitor RFK.
    [SRC] core.c:531."""
    band = chan["band_type"]
    tx_en = phy.set_channel_help(t, t.cv, band, enter=True, phy_idx=phy_idx, mac_idx=mac_idx)
    mac.set_channel_mac(t, chan, mac_idx)
    phy.set_channel_bb(t, chan, phy_idx)
    phy.set_channel_rf(t, chan, phy_idx)
    txpwr.set_txpwr(t, chan, phy_idx)
    phy.set_channel_help(t, t.cv, band, enter=False, phy_idx=phy_idx, mac_idx=mac_idx, tx_en=tx_en)
    band_changed = t.last_band[phy_idx] is not None and t.last_band[phy_idx] != band
    if not t.entity_active[phy_idx] or band_changed:
        coex.ntfy_switch_band(t, ep)
        rfk.rfk_band_changed(t, ep, chan, phy_idx)
    t.entity_active[phy_idx] = True
    t.last_band[phy_idx] = band
    # rfk_channel_for_pure_mon_vif runs only where the monitor vif has a link: PHY_0 only.
    if phy_idx == 0:
        rfk.rfk_channel(t, ep, chan, phy_idx)


def set_channel(t, channel: int, ep: int = None, mlo_1_1: bool = False) -> dict:
    """rtw89_set_channel: for a BE chip, __rtw89_set_channel runs for PHY_0/MAC_0 then PHY_1/MAC_1
    (same monitor channel). rtw89_entity_recalc picks the MLO mode per hop from the active-link map;
    the monitor setup flips it (MLO_2_PLUS_0_1RF vs MLO_1_PLUS_1_1RF), so mlo_1_1 is a per-hop input.
    [SRC] core.c:563, chan.c:485 rtw89_entity_sel_mlo_dbcc_mode."""
    chan = make_chan(channel)
    t.mlo_1_1 = mlo_1_1
    _set_channel_one(t, ep, chan, phy_idx=0, mac_idx=0)
    _set_channel_one(t, ep, chan, phy_idx=1, mac_idx=1)
    return chan
