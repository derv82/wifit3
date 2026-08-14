"""Pure-spec tests for the CSA IE + beacon-rewrite builders."""
import pytest

from wifit3.dot11.ie import csa_ie, ssid_ie, rates_ie, ds_param_ie
from wifit3.dot11.csa import build_csa_beacon

_BODY = bytes(range(36))          # stand-in 24B header + 12B fixed; only its bytes must survive


def test_csa_ie_matches_aircrack_wire():
    # aircrack PR #2724 test asserts this exact pattern: id 37, len 3, mode 1, ch 14, count 0.
    assert csa_ie(14).hex() == "2503010e00"


def test_appends_csa_and_preserves_header_and_tags():
    beacon = _BODY + ssid_ie("Net") + rates_ie() + ds_param_ie(6)
    out = build_csa_beacon(beacon, 14)
    assert out[:22] == _BODY[:22] and out[24:36] == _BODY[24:36]
    assert out.endswith(csa_ie(14))
    assert ssid_ie("Net") in out and ds_param_ie(6) in out


def test_sequence_control_is_zeroed_for_hw_restamp():
    out = build_csa_beacon(_BODY + ssid_ie("Net"), 14)
    assert out[22:24] == b"\x00\x00"


def test_replaces_a_stale_csa_element():
    beacon = _BODY + ssid_ie("Net") + csa_ie(6) + ds_param_ie(6)
    out = build_csa_beacon(beacon, 14)
    assert out.count(bytes([0x25])) == 1            # only one CSA element id survives
    assert out.endswith(csa_ie(14))
    assert csa_ie(6) not in out


def test_trailing_junk_past_last_ie_is_dropped():
    beacon = _BODY + ssid_ie("Net") + b"\x25\x7f\x01"   # a tag claiming 127 bytes, only 1 present
    out = build_csa_beacon(beacon, 14)
    expected = bytearray(_BODY)
    expected[22:24] = b"\x00\x00"
    assert out == bytes(expected) + ssid_ie("Net") + csa_ie(14)


def test_rejects_a_beacon_too_short_for_the_fixed_body():
    with pytest.raises(ValueError):
        build_csa_beacon(bytes(20), 14)
