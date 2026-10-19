"""Pure-spec tests for the AP-side response builders + the shared EAPOL-Key assembly."""
import struct

import pytest

from wifit3.dot11.ap import auth_resp, assoc_resp, eapol_m1, beacon_clone
from wifit3.dot11.eapol import (
    data_header, eapol_key, set_mic, LLC_SNAP_EAPOL, MIC_OFFSET, MIC_LEN, NONCE_LEN,
)
from wifit3.dot11.ie import ssid_ie, rates_ie, ext_rates_ie, ds_param_ie

_BSSID = bytes.fromhex("112233445566")
_CLIENT = bytes.fromhex("aabbccddeeff")
_ANONCE = bytes(range(32))

# A transition-mode RSN IE: CCMP group+pairwise, AKMs = PSK(2) + SAE(8), MFPC set.
_RSN_PSK_SAE = bytes.fromhex("30180100000fac040100000fac040200000fac02000fac088000")
_RSNXE = bytes.fromhex("f40120")   # SAE hash-to-element advert


def test_auth_resp_is_open_system_seq2_status0():
    f = auth_resp(_BSSID, _CLIENT)
    assert f[0:2] == b"\xb0\x00"
    assert f[4:10] == _CLIENT and f[10:16] == _BSSID and f[16:22] == _BSSID
    assert f[24:] == b"\x00\x00\x02\x00\x00\x00"      # alg 0, seq 2, status 0


def test_assoc_resp_success_with_privacy_and_rates():
    f = assoc_resp(_BSSID, _CLIENT, aid=1)
    assert f[0:2] == b"\x10\x00"
    assert f[24:26] == b"\x11\x00"                    # ESS + Privacy
    assert f[26:28] == b"\x00\x00"                    # status success
    assert f[28:30] == b"\x01\xc0"                    # AID 1, two MSBs set
    assert rates_ie() in f


def test_reassoc_resp_frame_type():
    assert assoc_resp(_BSSID, _CLIENT, reassoc=True)[0:2] == b"\x30\x00"


def test_assoc_resp_copies_beacon_elements_in_hostapd_order():
    wmm = bytes.fromhex("dd07" "0050f202" "0101" "00")
    wps = bytes.fromhex("dd05" "0050f204" "00")
    ext_cap = bytes.fromhex("7f03" "040000")
    ht_op = bytes.fromhex("3d03" "010000")
    ht_cap = bytes.fromhex("2d02" "6e01")             # cap info 0x016e: 40 MHz bit set
    he_cap = bytes.fromhex("ff03" "230102")
    beacon = bytes(36) + ssid_ie("x") + wmm + wps + ext_cap + he_cap + ht_op + ht_cap
    f = assoc_resp(_BSSID, _CLIENT, twin_beacon=beacon)
    tail = f[30 + len(rates_ie() + ext_rates_ie()):]
    assert tail == bytes.fromhex("2d02" "6c01") + ht_op + he_cap + ext_cap + wmm


def test_assoc_resp_rates_follow_the_operating_channel():
    five = assoc_resp(_BSSID, _CLIENT, channel=36)
    assert rates_ie(36) in five and rates_ie(1) not in five
    assert ext_rates_ie(1) not in five                # Extended Rates is 2.4 GHz-only
    two = assoc_resp(_BSSID, _CLIENT, channel=6)
    assert rates_ie(6) in two and ext_rates_ie(6) in two


def test_eapol_m1_layout_fromds_no_mic():
    f = eapol_m1(_BSSID, _CLIENT, _ANONCE, replay=1)
    assert f[0:2] == b"\x08\x02"                      # Data, FromDS
    assert f[4:10] == _CLIENT and f[10:16] == _BSSID and f[16:22] == _BSSID
    assert f[24:32] == LLC_SNAP_EAPOL
    p = f[32:]
    assert p[0:2] == bytes([2, 3])                    # 802.1X v2, EAPOL-Key
    assert p[2:4] == struct.pack(">H", len(p) - 4)    # length covers the descriptor
    assert p[4] == 2                                  # RSN key descriptor
    assert p[5:7] == struct.pack(">H", 0x008A)        # key info: pairwise + ack + keyver2
    assert p[7:9] == struct.pack(">H", 16)            # CCMP key length
    assert p[9:17] == b"\x00\x00\x00\x00\x00\x00\x00\x01"
    assert p[17:49] == _ANONCE
    assert p[MIC_OFFSET:MIC_OFFSET + MIC_LEN] == bytes(MIC_LEN)


def test_beacon_clone_strips_sae_keeps_psk():
    beacon = bytes(36) + ssid_ie("Net") + rates_ie() + ds_param_ie(11) + _RSN_PSK_SAE + _RSNXE
    out = beacon_clone(beacon, 1)
    assert out[22:24] == b"\x00\x00"                  # sequence zeroed for HW restamp
    assert ssid_ie("Net") in out
    assert ds_param_ie(1) in out and ds_param_ie(11) not in out
    assert b"\x00\x0f\xac\x08" not in out             # SAE AKM gone
    assert b"\x01\x00\x00\x0f\xac\x02" in out         # single PSK AKM
    assert _RSNXE not in out                          # RSN Extended Caps dropped


def test_beacon_clone_rejects_short_beacon():
    with pytest.raises(ValueError):
        beacon_clone(bytes(20), 1)


def test_data_header_direction():
    to_ap = data_header(to_ds=True, bssid=_BSSID, client=_CLIENT)
    assert to_ap[0:2] == b"\x08\x01" and to_ap[4:10] == _BSSID and to_ap[10:16] == _CLIENT
    from_ap = data_header(to_ds=False, bssid=_BSSID, client=_CLIENT)
    assert from_ap[0:2] == b"\x08\x02" and from_ap[4:10] == _CLIENT and from_ap[10:16] == _BSSID


def test_eapol_key_mic_offset_and_length_field():
    p = eapol_key(key_info=0x010A, key_len=0, replay=1, nonce=_ANONCE, key_data=b"\x30\x02\x01\x00")
    assert p[2:4] == struct.pack(">H", len(p) - 4)
    assert p[MIC_OFFSET:MIC_OFFSET + MIC_LEN] == bytes(MIC_LEN)
    assert p[17:49] == _ANONCE
    assert p.endswith(b"\x30\x02\x01\x00")


def test_eapol_key_rejects_bad_nonce():
    with pytest.raises(ValueError):
        eapol_key(key_info=0, key_len=0, replay=1, nonce=bytes(NONCE_LEN - 1))


def test_set_mic_splices_at_offset():
    p = eapol_key(key_info=0x010A, key_len=0, replay=1, nonce=_ANONCE)
    mic = bytes(range(16))
    out = set_mic(p, mic)
    assert out[MIC_OFFSET:MIC_OFFSET + MIC_LEN] == mic
    assert out[:MIC_OFFSET] == p[:MIC_OFFSET]
    assert out[MIC_OFFSET + MIC_LEN:] == p[MIC_OFFSET + MIC_LEN:]
