"""Pure-spec tests for the Probe Request / Probe Response builders."""
from wifit3.dot11.ie import ds_param_ie, ext_rates_ie, rates_ie, ssid_ie
from wifit3.dot11.probe import probe_req, probe_resp

_BSSID = bytes.fromhex("112233445566")
_OUR_MAC = bytes.fromhex("021122334455")


def test_probe_req_is_directed_at_the_bssid():
    f = probe_req(_BSSID, _OUR_MAC, "Net")
    assert f[0:2] == b"\x40\x00"
    assert f[4:10] == _BSSID and f[10:16] == _OUR_MAC and f[16:22] == _BSSID
    assert ssid_ie("Net") in f


def test_probe_req_rates_follow_the_target_channel():
    five = probe_req(_BSSID, _OUR_MAC, "Net", channel=36)
    assert rates_ie(36) in five and rates_ie(1) not in five
    assert ext_rates_ie(1) not in five
    two = probe_req(_BSSID, _OUR_MAC, "Net", channel=6)
    assert rates_ie(6) in two and ext_rates_ie(6) in two


def test_probe_resp_rates_follow_the_advertised_channel():
    five = probe_resp(_BSSID, "Net", 36)
    assert ds_param_ie(36) in five
    assert rates_ie(36) in five and rates_ie(1) not in five
    assert ext_rates_ie(1) not in five
