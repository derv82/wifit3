"""RTL8922AU channel tuning (rtw8922a_set_channel).

Builds the rtw89_chan for a channel and runs the per-channel BB/RF/MAC tune plus RFK, the unit
airmon-ng drives once per hop. Only the head (pre_set_channel_bb) is ported so far; the rest are
marked TODO. [SRC] core.c:531 __rtw89_set_channel, rtw8922a.c:2232 set_channel.
"""
from . import phy

RTW89_BAND_2G = 0                # core.h
RTW89_BAND_5G = 1
RTW89_BAND_6G = 2
RTW89_CHANNEL_WIDTH_20 = 0       # core.h


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
            "band_width": bandwidth, "freq": channel_to_freq(channel, band)}


def set_channel(t, channel: int, phy_idx: int = 0, mac_idx: int = 0) -> dict:
    """One per-channel tune: __rtw89_set_channel = set_channel_help(enter) [pre_set_channel bb/rf +
    hal_reset], set_channel (mac/bb/rf), set_txpwr, set_channel_help(exit) [hal_reset +
    post_set_channel bb/rf], then rfk. Only the head pre_set_channel_bb is ported so far.
    [SRC] core.c:531, rtw8922a.c:2321 set_channel_help / 2232 set_channel."""
    chan = make_chan(channel)
    phy.pre_set_channel_bb(t, phy_idx)
    # TODO: pre_set_channel_rf(phy_idx); hal_reset(enter); set_channel_mac/bb/rf(chan, phy_idx);
    #       set_txpwr(chan); hal_reset(exit) + post_set_channel bb/rf; rtw8922a_rfk(chan).
    return chan
