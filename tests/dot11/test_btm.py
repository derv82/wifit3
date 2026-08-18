"""Spec tests for the 802.11v BTM Request + Neighbor Report builders (IEEE 802.11-2020)."""
import pytest

from wifit3.dot11.btm import (
    build_btm_request, neighbor_report_ie, channel_operating_class,
    BTM_PREFERRED_CANDIDATE_LIST, BTM_ABRIDGED, BTM_DISASSOC_IMMINENT,
)

_CLIENT = bytes.fromhex("aabbccddeeff")
_AP = bytes.fromhex("001122334455")
_TWIN = bytes.fromhex("001122334456")


def test_channel_operating_class_2ghz_and_5ghz():
    assert channel_operating_class(1) == 81
    assert channel_operating_class(13) == 81
    assert channel_operating_class(14) == 82
    assert channel_operating_class(36) == 115
    assert channel_operating_class(64) == 118
    assert channel_operating_class(100) == 121
    assert channel_operating_class(157) == 125


def test_channel_operating_class_rejects_unmapped():
    with pytest.raises(ValueError):
        channel_operating_class(200)


def test_neighbor_report_ie_shape_with_preference():
    # id 52, len 16, BSSID, BSSID-info 0x00000003 LE (reachable, security bit clear), op-class 81,
    # ch 6, PHY HT(7), then Candidate Preference subelement: id 3, len 1, value 255.
    ie = neighbor_report_ie(_TWIN, 81, 6, preference=255)
    assert ie.hex() == "3410001122334456030000005106070301ff"
    assert ie[0] == 0x34 and ie[1] == len(ie) - 2 == 16


def test_neighbor_report_ie_omits_subelement_when_no_preference():
    ie = neighbor_report_ie(_TWIN, 81, 6)
    # body = 6 BSSID + 4 info + opclass + ch + phy = 13 exactly; no candidate-preference subelement.
    assert ie[1] == 13 and len(ie) == 15


def test_neighbor_report_ie_rejects_bad_bssid():
    with pytest.raises(ValueError):
        neighbor_report_ie(b"\x00\x11\x22", 81, 6)


def test_btm_request_header_is_a_wnm_action_frame():
    f = build_btm_request(_CLIENT, _AP, _AP, candidate_bssid=_TWIN, candidate_channel=6)
    assert f[0] == 0xD0 and f[1] == 0x00          # FC: mgmt Action, no flags
    assert f[2:4] == b"\x00\x00"                   # duration
    assert f[4:10] == _CLIENT and f[10:16] == _AP and f[16:22] == _AP   # a1/a2/a3
    assert f[22:24] == b"\x00\x00"                 # seq control zeroed for HW restamp
    assert f[24] == 0x0A and f[25] == 0x07         # category WNM, action BTM Request


def test_btm_request_body_fields_and_default_mode():
    f = build_btm_request(_CLIENT, _AP, _AP, candidate_bssid=_TWIN, candidate_channel=6,
                          dialog_token=0x2A, disassoc_timer=5, validity_interval=255)
    assert f[26] == 0x2A                           # dialog token
    assert f[27] == BTM_PREFERRED_CANDIDATE_LIST | BTM_ABRIDGED | BTM_DISASSOC_IMMINENT == 0x07
    assert f[28:30] == (5).to_bytes(2, "little")   # disassociation timer, LE
    assert f[30] == 0xFF                           # validity interval
    assert f[31:] == neighbor_report_ie(_TWIN, 81, 6, preference=255)   # candidate list


def test_btm_request_mode_bits_are_optional():
    f = build_btm_request(_CLIENT, _AP, _AP, candidate_bssid=_TWIN, candidate_channel=6,
                          disassoc_imminent=False, abridged=False)
    assert f[27] == BTM_PREFERRED_CANDIDATE_LIST   # 0x01: candidate list always, no abridged/imminent


def test_btm_request_derives_operating_class_from_channel():
    f = build_btm_request(_CLIENT, _AP, _AP, candidate_bssid=_TWIN, candidate_channel=157)
    assert f[31:] == neighbor_report_ie(_TWIN, 125, 157, preference=255)   # 5 GHz → class 125


def test_btm_request_operating_class_override_wins():
    f = build_btm_request(_CLIENT, _AP, _AP, candidate_bssid=_TWIN, candidate_channel=6,
                          operating_class=115)
    assert f[31:] == neighbor_report_ie(_TWIN, 115, 6, preference=255)
