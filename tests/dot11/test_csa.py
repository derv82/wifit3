"""Spec tests for the CSA/ECSA IE + beacon-rewrite builders."""
import pytest

from wifit3.dot11.ie import csa_ie, ecsa_ie, ssid_ie, rates_ie, ds_param_ie, secondary_channel_offset_ie
from wifit3.dot11.csa import build_csa_beacon

_SCO = secondary_channel_offset_ie(0)          # every CSA beacon trails a 20 MHz secondary-offset IE
_BODY = bytes(range(36))          # stand-in 24B header + 12B fixed; only its bytes must survive


def test_csa_ie_matches_aircrack_wire():
    # aircrack PR #2724 test asserts this exact pattern: id 37, len 3, mode 1, ch 14, count 0.
    assert csa_ie(14).hex() == "2503010e00"


def test_ecsa_ie_shape():
    # id 60 (0x3c), len 4, mode 1, operating class 81, ch 6, count 0.
    assert ecsa_ie(6, operating_class=81).hex() == "3c0401510600"


def test_same_band_switch_carries_csa_and_ecsa():
    beacon = _BODY + ssid_ie("Net") + rates_ie() + ds_param_ie(6)
    out = build_csa_beacon(beacon, 11, from_channel=6)         # 2.4 -> 2.4
    assert out[:22] == _BODY[:22] and out[24:36] == _BODY[24:36]
    assert out.endswith(csa_ie(11) + ecsa_ie(11, operating_class=81) + _SCO)
    assert ssid_ie("Net") in out and ds_param_ie(6) in out


def test_band_switch_is_ecsa_only():
    beacon = _BODY + ssid_ie("Net")
    out = build_csa_beacon(beacon, 36, from_channel=6)         # 2.4 -> 5 GHz
    assert out.endswith(ecsa_ie(36, operating_class=115) + _SCO)
    assert csa_ie(36) not in out                               # a bare CSA channel is ambiguous across bands
    assert out.count(bytes([0x25])) == 0                       # no CSA element id present


def test_sequence_control_is_zeroed_for_hw_restamp():
    out = build_csa_beacon(_BODY + ssid_ie("Net"), 11, from_channel=6)
    assert out[22:24] == b"\x00\x00"


def test_replaces_stale_csa_and_ecsa_elements():
    beacon = _BODY + ssid_ie("Net") + csa_ie(6) + ecsa_ie(6, operating_class=81) + ds_param_ie(6)
    out = build_csa_beacon(beacon, 11, from_channel=6)
    assert out.count(bytes([0x25])) == 1                       # one CSA element id survives
    assert out.count(bytes([0x3C])) == 1                       # one ECSA element id survives
    assert out.endswith(csa_ie(11) + ecsa_ie(11, operating_class=81) + _SCO)


def test_rejects_a_beacon_too_short_for_the_fixed_body():
    with pytest.raises(ValueError):
        build_csa_beacon(bytes(20), 14, from_channel=6)


def test_switch_count_is_carried_into_both_elements():
    out = build_csa_beacon(_BODY + ssid_ie("Net"), 6, from_channel=11, count=3)
    assert out.endswith(csa_ie(6, count=3) + ecsa_ie(6, operating_class=81, count=3) + _SCO)
