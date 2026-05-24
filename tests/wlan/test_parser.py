import struct

import pytest

from wifit3.wlan.packet import WlanFrameParser


# ---- Beacon builder for RSN-IE tests ---------------------------------------

def _build_beacon(
    *,
    ssid: str = "TestNet",
    rsn_ie: bytes = b"",
    wpa_vendor_ie: bytes = b"",
    privacy_bit: bool = False,
) -> bytes:
    """Build a minimally valid 802.11 beacon. Tag 0 (SSID) is always first;
    Tag 1 (Supported Rates) follows because _is_valid_frame requires it."""
    fc = b"\x80\x00"
    dur = b"\x00\x00"
    addr1 = b"\xff\xff\xff\xff\xff\xff"
    addr2 = b"\x11\x22\x33\x44\x55\x66"
    addr3 = addr2
    seq = b"\x00\x00"
    mac_hdr = fc + dur + addr1 + addr2 + addr3 + seq

    # Fixed params: 8 B timestamp + 2 B beacon interval + 2 B capabilities.
    cap_info = 0x0001  # ESS (bit 0)
    if privacy_bit:
        cap_info |= 0x0010  # Privacy (bit 4)
    fixed = b"\x00" * 8 + b"\x64\x00" + struct.pack("<H", cap_info)

    ssid_bytes = ssid.encode("utf-8")
    tag_ssid = bytes([0x00, len(ssid_bytes)]) + ssid_bytes
    tag_rates = b"\x01\x04\x82\x84\x8b\x96"  # 1, 2, 5.5, 11

    return mac_hdr + fixed + tag_ssid + tag_rates + rsn_ie + wpa_vendor_ie


def _rsn_ie(
    *,
    group_cipher: int = 0x04,       # CCMP
    pairwise_ciphers=(0x04,),       # CCMP
    akms=(0x02,),                   # PSK
    rsn_caps: int = 0,
) -> bytes:
    """Build a tag-48 RSN IE (incl. the 2-byte tag header)."""
    body = b"\x01\x00"  # Version = 1
    body += b"\x00\x0f\xac" + bytes([group_cipher])
    body += struct.pack("<H", len(pairwise_ciphers))
    for c in pairwise_ciphers:
        body += b"\x00\x0f\xac" + bytes([c])
    body += struct.pack("<H", len(akms))
    for a in akms:
        body += b"\x00\x0f\xac" + bytes([a])
    body += struct.pack("<H", rsn_caps)
    return bytes([48, len(body)]) + body


def _wps_ie(*, locked: bool = False, version2: bool = False,
            configured: bool = True) -> bytes:
    """Build a WPS vendor IE (tag 221, OUI 00:50:F2, OUI-type 4) with the
    nested big-endian TLVs. AP Setup Locked is always emitted (as real APs
    do) so both the locked and unlocked decode paths are exercised."""
    def tlv(attr: int, val: bytes) -> bytes:
        return struct.pack(">HH", attr, len(val)) + val

    body = tlv(0x104A, b"\x10")                                   # Version 1.0
    body += tlv(0x1044, b"\x02" if configured else b"\x01")      # Setup State
    body += tlv(0x1057, b"\x01" if locked else b"\x00")          # AP Setup Locked
    body += tlv(0x1008, b"\x00\x84")                             # Config Methods
    body += tlv(0x1012, b"\x00\x00")                             # Device Password ID (PIN)
    if version2:
        body += tlv(0x1049, b"\x00\x37\x2a" + b"\x00\x01\x20")  # Vendor Ext → Version2
    payload = b"\x00\x50\xf2\x04" + body
    return bytes([0xDD, len(payload)]) + payload


def test_wps_open_beacon():
    r = WlanFrameParser.parse_80211_frame(
        _build_beacon(wpa_vendor_ie=_wps_ie(locked=False)), -50)
    assert r["wps"] is True
    assert r["wps_locked"] is False
    assert r["wps_version"] == "1.0"


def test_wps_locked_beacon():
    r = WlanFrameParser.parse_80211_frame(
        _build_beacon(wpa_vendor_ie=_wps_ie(locked=True)), -50)
    assert r["wps"] is True
    assert r["wps_locked"] is True


def test_wps_version2_beacon():
    r = WlanFrameParser.parse_80211_frame(
        _build_beacon(wpa_vendor_ie=_wps_ie(version2=True)), -50)
    assert r["wps"] is True
    assert r["wps_version"] == "2.0"


def test_no_wps_ie_absent():
    r = WlanFrameParser.parse_80211_frame(
        _build_beacon(rsn_ie=_rsn_ie()), -50)
    assert not r.get("wps", False)

def test_wlan_frame_parser_validates():
    # A random bunch of bytes too small to be a frame
    assert WlanFrameParser.parse_80211_frame(b'\x00\x01\x02', -50) is None

def test_wlan_frame_parser_ignores_second_ssid_ie():
    """Spec: SSID IE is mandatory and FIRST. Any later tag-0 we encounter
    while walking IEs is malformed-frame OR — more commonly — the walker
    straying into trailing bytes (unstripped FCS, hw metadata). Either way
    we must NOT overwrite the canonical SSID with that junk.

    Regression for the AR9271-FCS-leak bug: a hidden beacon would
    occasionally "decloak" to short random strings like "F" / "7" / "/:"
    because the trailing CRC32 bytes happened to look like `00 02 2f 3a`.
    """
    fc = b"\x80\x00"
    dur = b"\x00\x00"
    addr1 = b"\xff\xff\xff\xff\xff\xff"
    addr2 = b"\x11\x22\x33\x44\x55\x66"
    mac_hdr = fc + dur + addr1 + addr2 + addr2 + b"\x00\x00"
    fixed = b"\x00" * 12

    legit_hidden_ssid = b"\x00\x00"       # SSID IE: length 0 → hidden
    rates = b"\x01\x04\x82\x84\x8b\x96"
    bogus_late_ssid = b"\x00\x02/:"       # exactly the bytes that bit us

    frame = mac_hdr + fixed + legit_hidden_ssid + rates + bogus_late_ssid

    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed is not None
    # Stays "<hidden>" — the late tag-0 must be ignored.
    assert parsed["ssid"] == "<hidden>"


def test_wlan_frame_parser_extracts_ssid():
    # Construct a minimal fake beacon frame to test tag parsing
    # MAC Header (24 bytes)
    fc = b'\x80\x00' # Beacon
    dur = b'\x00\x00'
    addr1 = b'\xff\xff\xff\xff\xff\xff'
    addr2 = b'\x11\x22\x33\x44\x55\x66'
    addr3 = b'\x11\x22\x33\x44\x55\x66'
    seq = b'\x00\x00'
    mac_hdr = fc + dur + addr1 + addr2 + addr3 + seq
    
    # Fixed Params (12 bytes)
    fixed = b'\x00' * 12
    
    # Tag 0 (SSID): "Test"
    tag_ssid = b'\x00\x04Test'
    
    frame = mac_hdr + fixed + tag_ssid
    
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed is not None
    assert parsed["type"] == "beacon"
    assert parsed["bssid"] == "11:22:33:44:55:66"
    assert parsed["ssid"] == "Test"


# ---- WPA3 / PMF / encryption-label tests -----------------------------------

def test_wpa2_psk_ccmp_label():
    """Standard WPA2-PSK-CCMP — by far the most common consumer config."""
    frame = _build_beacon(rsn_ie=_rsn_ie(pairwise_ciphers=(0x04,), akms=(0x02,)))
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["encryption"] == "WPA2-PSK-CCMP"
    assert parsed["wpa3"] is False
    assert parsed["transition_mode"] is False
    assert parsed["pairwise_cipher"] == "CCMP"
    assert parsed["akms"] == ["PSK"]


def test_wpa3_sae_label_and_flags():
    """Pure WPA3-SAE — PMF must be required (per WPA3 mandate)."""
    rsn = _rsn_ie(
        pairwise_ciphers=(0x04,),
        akms=(0x08,),
        rsn_caps=0x0080 | 0x0040,  # MFPC + MFPR
    )
    frame = _build_beacon(rsn_ie=rsn)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["encryption"] == "WPA3-SAE-CCMP"
    assert parsed["wpa3"] is True
    assert parsed["transition_mode"] is False
    assert parsed["pmf_capable"] is True
    assert parsed["pmf_required"] is True
    assert parsed["akms"] == ["SAE"]


def test_wpa2_wpa3_transition_mode():
    """WPA2/WPA3 transition AP advertises both PSK and SAE AKMs.
    PMF is capable but not required (so legacy clients can still connect)."""
    rsn = _rsn_ie(
        pairwise_ciphers=(0x04,),
        akms=(0x02, 0x08),
        rsn_caps=0x0080,  # MFPC only
    )
    frame = _build_beacon(rsn_ie=rsn)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["encryption"] == "WPA2/WPA3-PSK+SAE-CCMP"
    assert parsed["wpa3"] is True
    assert parsed["transition_mode"] is True
    assert parsed["pmf_capable"] is True
    assert parsed["pmf_required"] is False


def test_wpa2_enterprise_eap():
    """WPA2-EAP (corporate / 802.1X) — AKM 0x01."""
    rsn = _rsn_ie(pairwise_ciphers=(0x04,), akms=(0x01,))
    frame = _build_beacon(rsn_ie=rsn)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["encryption"] == "WPA2-EAP-CCMP"
    assert parsed["akms"] == ["EAP"]


def test_wpa2_psk_tkip_legacy_cipher():
    """Some old routers still advertise TKIP for pairwise."""
    rsn = _rsn_ie(pairwise_ciphers=(0x02,), akms=(0x02,))
    frame = _build_beacon(rsn_ie=rsn)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["encryption"] == "WPA2-PSK-TKIP"
    assert parsed["pairwise_cipher"] == "TKIP"


def test_open_network():
    frame = _build_beacon(rsn_ie=b"")
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["encryption"] == "OPEN"
    assert parsed["wpa3"] is False


def test_wep_via_privacy_bit():
    frame = _build_beacon(rsn_ie=b"", privacy_bit=True)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["encryption"] == "WEP"


# ---- WEP Data-frame IV extraction ------------------------------------------

def _build_wep_data(
    *,
    iv: bytes = b"\x03\xff\x00",
    keyid: int = 0,
    ext_iv: bool = False,
    qos: bool = False,
    bssid: bytes = b"\x11\x22\x33\x44\x55\x66",
    client: bytes = b"\xaa\xbb\xcc\xdd\xee\xff",
    body_len: int = 16,
) -> bytes:
    """Build a protected AP→client Data frame. ``ext_iv`` sets the Key ID
    byte's ExtIV bit (0x20) to emulate TKIP/CCMP; left clear it's WEP."""
    subtype = 0x08 if qos else 0x00
    fc0 = 0x08 | (subtype << 4)        # type=data, subtype
    fc1 = 0x02 | 0x40                  # from_ds + Protected
    mac = bytes([fc0, fc1]) + b"\x00\x00"
    # from_ds: addr1=dest(client), addr2=bssid(AP), addr3=source(client)
    mac += client + bssid + client + b"\x00\x00"   # ...+ seq
    if qos:
        mac += b"\x00\x00"            # QoS Control → header is 26 B
    keyid_byte = (keyid << 6) & 0xC0
    if ext_iv:
        keyid_byte |= 0x20
    return mac + iv + bytes([keyid_byte]) + b"\x00" * body_len


def test_wep_data_extracts_iv():
    frame = _build_wep_data(iv=b"\x03\xff\x00", keyid=1)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["type"] == "wep_data"
    assert parsed["wep_iv"] == b"\x03\xff\x00"
    assert parsed["wep_keyid"] == 1
    assert parsed["bssid"] == "11:22:33:44:55:66"


def test_wep_qos_data_iv_offset():
    """QoS Data has a 26-byte header; the IV must be read past the QoS
    Control field, not at the non-QoS offset 24."""
    frame = _build_wep_data(iv=b"\xde\xad\xbe", qos=True)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["type"] == "wep_data"
    assert parsed["wep_iv"] == b"\xde\xad\xbe"


def test_ext_iv_data_is_not_wep():
    """TKIP/CCMP set the ExtIV bit — must NOT be tallied as a WEP IV."""
    frame = _build_wep_data(ext_iv=True)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["type"] == "data"
    assert "wep_iv" not in parsed


def test_unprotected_data_is_not_wep():
    """A cleartext Data frame (Protected bit clear) is never WEP."""
    frame = bytearray(_build_wep_data())
    frame[1] &= ~0x40  # clear Protected bit
    parsed = WlanFrameParser.parse_80211_frame(bytes(frame), -50)
    assert parsed["type"] == "data"
    assert "wep_iv" not in parsed


def test_wpa3_keys_propagate_through_parse_80211_frame():
    """Regression: pre-fix these keys were set on the tags dict but dropped
    in parse_80211_frame, never reaching _on_frame_parsed."""
    rsn = _rsn_ie(akms=(0x08,), rsn_caps=0x00C0)
    frame = _build_beacon(rsn_ie=rsn)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)

    for key in (
        "wpa3",
        "transition_mode",
        "pmf_capable",
        "pmf_required",
        "pairwise_cipher",
        "akms",
        "rsn_ie_raw",
    ):
        assert key in parsed, f"parse_80211_frame did not propagate '{key}'"


def test_rsn_ie_raw_round_trip():
    """The harvester echoes rsn_ie_raw into its Assoc Req. The bytes must be
    exactly the IE as advertised, including the 2-byte tag header."""
    rsn = _rsn_ie(akms=(0x02,))
    frame = _build_beacon(rsn_ie=rsn)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["rsn_ie_raw"] == rsn


# ---- Channel parsing (2.4 GHz DS Param + 5 GHz HT Op + VHT Op) -------------

def _ds_param_ie(channel: int) -> bytes:
    """Tag 3 (DS Parameter Set) — 1-byte channel. Present on 2.4 GHz
    beacons; vendor-optional on 5 GHz (most APs omit it there)."""
    return bytes([3, 1, channel])


def _ht_op_ie(primary_channel: int) -> bytes:
    """Tag 61 (HT Operation) — 22-byte body, first byte = primary channel.
    Present on every 802.11n/ac AP regardless of band."""
    body = bytes([primary_channel]) + b"\x00" * 21
    return bytes([61, len(body)]) + body


def _vht_op_ie(center_seg0: int) -> bytes:
    """Tag 192 (VHT Operation) — 5-byte body. Byte 1 is Channel Center
    Frequency Segment 0 = primary channel for 20 MHz BSSes."""
    body = bytes([0x00, center_seg0, 0x00, 0x00, 0x00])
    return bytes([192, len(body)]) + body


def test_channel_from_ds_param_ie_2ghz():
    """2.4 GHz beacon → DS Param IE present, sets channel."""
    frame = _build_beacon() + _ds_param_ie(6)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["channel"] == 6


def test_channel_from_ht_op_ie_when_ds_param_missing():
    """5 GHz beacon with HT Op IE but no DS Param IE → channel from HT Op."""
    frame = _build_beacon() + _ht_op_ie(153)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["channel"] == 153


def test_channel_ds_param_wins_over_ht_op():
    """When both are present and disagree (shouldn't happen on a sane AP),
    DS Param IE wins — it's the authoritative 802.11-2020 9.4.2.3 source."""
    # HT Op IE before DS Param IE in tag order.
    frame = _build_beacon() + _ht_op_ie(36) + _ds_param_ie(40)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["channel"] == 40
    # And the other way round — DS Param IE first.
    frame2 = _build_beacon() + _ds_param_ie(40) + _ht_op_ie(36)
    parsed2 = WlanFrameParser.parse_80211_frame(frame2, -50)
    assert parsed2["channel"] == 40


def test_channel_vht_op_used_as_last_resort():
    """When neither DS Param nor HT Op is present, VHT Op IE fills in."""
    frame = _build_beacon() + _vht_op_ie(149)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed["channel"] == 149


def test_channel_absent_when_no_ie_provides_it():
    """Beacon without any channel-bearing IE → parser does NOT synthesise
    channel=1 (pre-fix behaviour that mis-tagged 5 GHz APs missing DS Param
    IE as channel 1). Caller falls back to chip's current_channel."""
    frame = _build_beacon()
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert "channel" not in parsed
