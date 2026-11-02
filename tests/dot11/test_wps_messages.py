"""Offline tests for the WSC message codec + EAP/EAPOL framing.

Self-consistency: build registrar messages, then parse them / decrypt their
Encrypted Settings back. The full two-halves attack is exercised in
test_wps_registrar.py; on-air correctness is proven by wps_probe.py.
"""

import struct

from wifit3.dot11.wsc import messages as M
from wifit3.dot11.wsc import crypto as wc


def test_tlv_roundtrip():
    blob = M.tlv(M.ATTR_SSID, b"hello") + M.tlv_u16(M.ATTR_CONFIG_ERROR, 0) + M.tlv_u8(M.ATTR_VERSION, 0x10)
    attrs = M.parse_tlvs(blob)
    assert attrs[M.ATTR_SSID] == b"hello"
    assert attrs[M.ATTR_CONFIG_ERROR] == b"\x00\x00"
    assert attrs[M.ATTR_VERSION] == b"\x10"


def test_build_m2_structure():
    nonce_e = b"\xAA" * 16
    nonce_r = b"\xBB" * 16
    uuid_r = b"\xCC" * 16
    pkr = b"\xDD" * 192
    authkey = b"\x01" * 32
    m1_attrs = M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M1)

    m2 = M.build_m2(nonce_e, nonce_r, uuid_r, pkr, authkey, m1_attrs, M.RF_BAND_24GHZ, False)
    attrs = M.parse_tlvs(m2)
    assert attrs[M.ATTR_MSG_TYPE] == bytes([M.WPS_M2])
    assert attrs[M.ATTR_ENROLLEE_NONCE] == nonce_e
    assert attrs[M.ATTR_REGISTRAR_NONCE] == nonce_r
    assert attrs[M.ATTR_PUBLIC_KEY] == pkr
    assert len(attrs[M.ATTR_AUTHENTICATOR]) == wc.AUTHENTICATOR_LEN
    # Authenticator must cover M1 || M2-without-authenticator.
    body = m2[: -(4 + wc.AUTHENTICATOR_LEN)]
    assert attrs[M.ATTR_AUTHENTICATOR] == wc.authenticator(authkey, m1_attrs, body)


def test_build_m4_encrypted_settings_roundtrip():
    authkey = b"\x02" * 32
    keywrapkey = b"\x03" * 16
    pke, pkr = b"\x04" * 192, b"\x05" * 192
    nonce_e = b"\x06" * 16
    r_s1, r_s2 = b"\x07" * 16, b"\x08" * 16
    psk1, psk2 = b"\x09" * 16, b"\x0a" * 16
    m3 = M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M3)

    m4 = M.build_m4(nonce_e, r_s1, r_s2, psk1, psk2, pke, pkr, authkey, keywrapkey, m3, False)
    attrs = M.parse_tlvs(m4)
    assert attrs[M.ATTR_R_HASH1] == wc.e_or_r_hash(authkey, r_s1, psk1, pke, pkr)
    assert attrs[M.ATTR_R_HASH2] == wc.e_or_r_hash(authkey, r_s2, psk2, pke, pkr)

    # Decrypt the Encrypted Settings → should reveal R-SNonce1 + a valid KWA.
    enc = attrs[M.ATTR_ENCR_SETTINGS]
    iv, ct = enc[:16], enc[16:]
    plain = wc.pkcs5_unpad(wc.aes128_cbc_decrypt(keywrapkey, iv, ct))
    inner = M.parse_tlvs(plain)
    assert inner[M.ATTR_R_SNONCE1] == r_s1
    # KWA covers the plaintext before the KWA TLV.
    kwa_off = plain.index(struct.pack(">HH", M.ATTR_KEY_WRAP_AUTH, wc.KWA_LEN))
    assert inner[M.ATTR_KEY_WRAP_AUTH] == wc.key_wrap_authenticator(authkey, plain[:kwa_off])


def test_eapol_framing_lengths():
    start = M.eapol_start()
    assert start == bytes([1, 1, 0, 0])               # ver=1, EAPOL-Start, len 0

    ident = M.eap_identity_response(0x42)
    # 802.1X(4: ver,type,len) + EAP(4: code,id,len) + type(1) + identity
    assert ident[:2] == bytes([1, 0])                 # ver=1, EAP-Packet
    assert ident[4] == M.EAP_RESPONSE and ident[5] == 0x42
    assert ident[8] == M.EAP_TYPE_IDENTITY
    assert ident.endswith(M.REGISTRAR_IDENTITY)


def _enrollee_request_frame(opcode, wsc_attrs, eap_id=0x10, bssid=b"\x34" * 6, sta=b"\x02" * 6,
                            flags=0x00, total_len=None):
    """Frame a WSC message as the AP (enrollee=authenticator) would: EAP-Request,
    FromDS data frame. Mirror of messages.build_data_frame but EAP_REQUEST.

    ``flags`` is the EAP-WSC flags byte; when the Length-Field bit (0x02) is set a
    2-byte total length is inserted between the flags and the WSC attributes.
    """
    lf = struct.pack(">H", len(wsc_attrs) if total_len is None else total_len) if flags & 0x02 else b""
    expanded = (
        bytes([M.EAP_TYPE_EXPANDED]) + M.WFA_VENDOR_ID + M.WFA_VENDOR_TYPE_SIMPLECONFIG
        + bytes([opcode, flags]) + lf + wsc_attrs
    )
    eap = struct.pack(">BBH", M.EAP_REQUEST, eap_id, 4 + len(expanded)) + expanded
    x = struct.pack(">BBH", 1, 0, len(eap)) + eap
    fc = b"\x08\x02"                                  # data, FromDS
    hdr = fc + b"\x00\x00" + sta + bssid + bssid + b"\x00\x00"
    return hdr + M.LLC_SNAP_EAPOL + x


def test_parse_rx_m1_request():
    m1_attrs = (
        M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M1)
        + M.tlv(M.ATTR_ENROLLEE_NONCE, b"\xEE" * 16) + M.tlv(M.ATTR_PUBLIC_KEY, b"\xFF" * 192)
    )
    frame = _enrollee_request_frame(M.WSC_MSG, m1_attrs, eap_id=0x55)
    p = M.parse_rx_frame(frame)
    assert p is not None
    assert p.eap_code == M.EAP_REQUEST and p.eap_id == 0x55
    assert p.wsc_opcode == M.WSC_MSG and p.wsc_msg_type == M.WPS_M1
    assert p.attrs[M.ATTR_ENROLLEE_NONCE] == b"\xEE" * 16
    assert p.raw_wsc_attrs == m1_attrs


def test_parse_rx_strips_trailing_fcs():
    # Cards append a 4-byte FCS; it must NOT leak into raw_wsc_attrs (which the
    # next Authenticator HMACs). This was the bug that made every M2 rejected.
    m1_attrs = (
        M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M1)
        + M.tlv(M.ATTR_ENROLLEE_NONCE, b"\xEE" * 16) + M.tlv(M.ATTR_PUBLIC_KEY, b"\xFF" * 192)
    )
    frame = _enrollee_request_frame(M.WSC_MSG, m1_attrs) + b"\xde\xad\xbe\xef"  # + FCS
    p = M.parse_rx_frame(frame)
    assert p is not None and p.wsc_msg_type == M.WPS_M1
    assert p.raw_wsc_attrs == m1_attrs          # FCS excluded
    assert b"\xde\xad\xbe\xef" not in p.raw_wsc_attrs


def test_parse_rx_honors_length_field_flag():
    # EAP-WSC flags bit 0x02 (Length Field) inserts a 2-byte total length between
    # the flags byte and the WSC attributes. The parser must skip it; otherwise the
    # length bytes are misread as the first TLV and the whole message parses wrong.
    m1_attrs = (
        M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M1)
        + M.tlv(M.ATTR_ENROLLEE_NONCE, b"\xEE" * 16)
    )
    frame = _enrollee_request_frame(M.WSC_MSG, m1_attrs, flags=0x02)
    p = M.parse_rx_frame(frame)
    assert p is not None
    assert p.wsc_msg_type == M.WPS_M1
    assert p.attrs[M.ATTR_ENROLLEE_NONCE] == b"\xEE" * 16
    assert p.raw_wsc_attrs == m1_attrs


def test_parse_rx_length_field_with_trailing_fcs():
    # Length Field set AND a card-appended FCS: attrs start after the 2-byte length,
    # and raw_wsc_attrs must still exclude the trailing FCS (bounded by EAP length).
    m1_attrs = (
        M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M1)
        + M.tlv(M.ATTR_ENROLLEE_NONCE, b"\xEE" * 16)
    )
    frame = _enrollee_request_frame(M.WSC_MSG, m1_attrs, flags=0x02) + b"\xde\xad\xbe\xef"
    p = M.parse_rx_frame(frame)
    assert p is not None and p.wsc_msg_type == M.WPS_M1
    assert p.raw_wsc_attrs == m1_attrs


def test_parse_rx_identity_request():
    fc = b"\x08\x02"
    eap = struct.pack(">BBH", M.EAP_REQUEST, 1, 4 + 1) + bytes([M.EAP_TYPE_IDENTITY])
    x = struct.pack(">BBH", 1, 0, len(eap)) + eap
    frame = fc + b"\x00\x00" + b"\x02" * 6 + b"\x34" * 6 + b"\x34" * 6 + b"\x00\x00" + M.LLC_SNAP_EAPOL + x
    p = M.parse_rx_frame(frame)
    assert p is not None and p.is_identity_request and p.eap_id == 1


def test_extract_m7_credentials():
    keywrapkey = b"\x0b" * 16
    authkey = b"\x0c" * 32
    # Build an M7-style Encrypted Settings: SSID + Network Key + KWA.
    inner = M.tlv(M.ATTR_SSID, b"TestNet") + M.tlv(M.ATTR_NETWORK_KEY, b"s3cr3tpassword")
    kwa = wc.key_wrap_authenticator(authkey, inner)
    plain = wc.pkcs5_pad(inner + M.tlv(M.ATTR_KEY_WRAP_AUTH, kwa))
    iv = b"\x0d" * 16
    enc = iv + wc.aes128_cbc_encrypt(keywrapkey, iv, plain)

    creds = M.extract_m7_credentials(enc, keywrapkey)
    assert creds is not None
    assert creds["ssid"] == b"TestNet"
    assert creds["network_key"] == b"s3cr3tpassword"


_M2_ARGS = (b"A" * 16, b"B" * 16, b"C" * 16, b"D" * 192, b"E" * 32,
            M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M1))


def test_rf_bands_follows_the_operating_channel():
    assert M.rf_band_for_channel(1) == M.RF_BAND_24GHZ
    assert M.rf_band_for_channel(14) == M.RF_BAND_24GHZ
    assert M.rf_band_for_channel(36) == M.RF_BAND_5GHZ
    assert M.rf_band_for_channel(165) == M.RF_BAND_5GHZ

    m2 = M.build_m2(*_M2_ARGS, M.rf_band_for_channel(44), False)
    assert M.parse_tlvs(m2)[M.ATTR_RF_BANDS] == bytes([M.RF_BAND_5GHZ])
    m1 = M.build_m1(b"U" * 16, b"M" * 6, b"N" * 16, b"P" * 192,
                    M.rf_band_for_channel(6), False)
    assert M.parse_tlvs(m1)[M.ATTR_RF_BANDS] == bytes([M.RF_BAND_24GHZ])


def test_wfa_version2_ext_bytes():
    # Vendor Extension 0x1049, len 6: WFA OUI 00:37:2A, subelement 0 (Version2) len 1 = 0x20.
    assert M.wfa_version2_ext(True).hex() == "1049000600372a000120"
    assert M.wfa_version2_ext(False) == b""


def test_wsc_2_0_rides_in_every_registrar_message():
    ext = M.wfa_version2_ext(True)
    m2 = M.build_m2(*_M2_ARGS, M.RF_BAND_24GHZ, True)
    m4 = M.build_m4(b"A" * 16, b"1" * 16, b"2" * 16, b"p" * 16, b"q" * 16, b"k" * 192,
                    b"r" * 192, b"E" * 32, b"W" * 16, b"", True)
    m6 = M.build_m6(b"A" * 16, b"2" * 16, b"E" * 32, b"W" * 16, b"", True)
    tail = 4 + wc.AUTHENTICATOR_LEN                  # the trailing Authenticator attribute
    for msg in (m2, m4, m6):
        # Inside the body the Authenticator covers, i.e. ahead of that trailing attribute.
        assert 0 <= msg.index(ext) <= len(msg) - tail - len(ext)
    assert ext not in M.build_m2(*_M2_ARGS, M.RF_BAND_24GHZ, False)


def test_assoc_ie_advertises_wsc_2_0_only_when_asked():
    from wifit3.dot11.wsc.assoc_ie import WPS_REQ_REGISTRAR, wps_assoc_ie
    v1 = wps_assoc_ie(WPS_REQ_REGISTRAR, False)
    v2 = wps_assoc_ie(WPS_REQ_REGISTRAR, True)
    assert v1.hex() == "dd0e0050f204104a000110103a000102"
    assert v2[1] == len(v2) - 2                      # tag length still covers the whole body
    assert v2[2:].hex() == v1[2:].hex() + "1049000600372a000120"


def test_registrar_and_enrollee_take_rf_bands_from_their_channel():
    from wifit3.campaigns.wps.enrollee import WpsEnrollee
    from wifit3.campaigns.wps.registrar import WpsRegistrar
    b, sta = b"B" * 6, b"S" * 6
    assert WpsRegistrar(None, b, sta, channel=44, wsc_2_0=True).rf_bands == M.RF_BAND_5GHZ
    assert WpsRegistrar(None, b, sta, channel=6, wsc_2_0=False).rf_bands == M.RF_BAND_24GHZ
    assert WpsEnrollee(None, b, sta, channel=44, wsc_2_0=False).rf_bands == M.RF_BAND_5GHZ



def test_m1_advertises_display_so_hostapd_sends_the_passphrase_not_the_hex_psk():
    m1 = M.build_m1(b"U" * 16, b"M" * 6, b"N" * 16, b"P" * 192,
                    rf_bands=M.RF_BAND_24GHZ, wsc_2_0=True)
    config_methods = struct.unpack(">H", M.parse_tlvs(m1)[M.ATTR_CONFIG_METHODS])[0]
    assert config_methods & 0x0008
