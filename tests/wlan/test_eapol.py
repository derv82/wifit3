"""Tests for EAPOL-Key parsing + 4-way handshake pair detection."""

import struct

import pytest

from wifit3.models import HandshakeMessage, Handshake
from wifit3.crack.handshake import crackable_pairs
from wifit3.crack.hc22000_format import eapol_hashlines
from wifit3.dot11.parser import WlanFrameParser


def _build_eapol_frame(
    bssid_bytes: bytes,
    client_bytes: bytes,
    key_info: int,
    replay_counter: bytes,
    nonce: bytes = b"\x00" * 32,
    mic: bytes = b"\x00" * 16,
    key_data: bytes = b"",
) -> bytes:
    """Build a minimal valid 802.11 data frame carrying an EAPOL-Key payload.

    Direction is client -> AP (to_ds=1, from_ds=0) which matches M2/M4.
    Flip to_ds/from_ds for M1/M3 if it matters (it doesn't for our parser:
    we read the BSSID from addr1 when to_ds=0, addr2 when to_ds=1, but the
    classification path doesn't care about direction).
    """
    # FC0 = data frame (type=2), subtype=0
    fc0 = 0x08
    # FC1: to_ds=1, from_ds=0
    fc1 = 0x01
    dur = b"\x00\x00"
    # to_ds=1: addr1=BSSID, addr2=SRC(client), addr3=DST
    seq = b"\x00\x00"
    mac_hdr = bytes([fc0, fc1]) + dur + bssid_bytes + client_bytes + bssid_bytes + seq

    # LLC/SNAP + EAPOL ethertype
    llc_snap = b"\xaa\xaa\x03\x00\x00\x00\x88\x8e"

    # 802.1X header: version(1), type(1)=3 EAPOL-Key, length(2)
    key_descriptor = (
        b"\x02"                                          # Key Desc Type = 2 (RSN)
        + struct.pack(">H", key_info)                    # Key Info
        + b"\x00\x10"                                    # Key Length
        + replay_counter                                 # 8 B
        + nonce                                          # 32 B
        + b"\x00" * 16                                   # Key IV
        + b"\x00" * 8                                    # Key RSC
        + b"\x00" * 8                                    # Reserved
        + mic                                            # 16 B
        + struct.pack(">H", len(key_data))               # Key Data Length
        + key_data
    )
    body_len = len(key_descriptor)
    x802_1x = b"\x01\x03" + struct.pack(">H", body_len)

    return mac_hdr + llc_snap + x802_1x + key_descriptor


# ---- Classifier unit tests ------------------------------------------------------

@pytest.mark.parametrize(
    "key_info, key_data_len, expected",
    [
        (0x0080 | 0x0008, 0, 1),                 # M1: ACK, !MIC, !INSTALL, pairwise
        (0x0100 | 0x0008, 22, 2),                # M2: MIC, !ACK, !INSTALL, key data present
        (0x01C0 | 0x0008, 56, 3),                # M3: ACK+MIC+INSTALL
        (0x0100 | 0x0008, 0, 4),                 # M4: MIC, !ACK, !INSTALL, no key data
        (0x0000, 0, 0),                          # All flags clear → not a 4-way msg
        (0x0080 | 0x0040, 0, 0),                 # ACK + INSTALL only: group rekey-ish
    ],
)
def test_classify_eapol_msg(key_info, key_data_len, expected):
    assert WlanFrameParser._classify_eapol_msg(key_info, key_data_len) == expected


# ---- EAP (EAPOL-Type-0) passive classifier --------------------------------------

_EAP_BSSID = b"\xaa\xbb\xcc\x00\x11\x22"
_EAP_CLIENT = b"\xde\xad\xbe\xef\x00\x01"


def _build_eap_frame(
    bssid_bytes: bytes,
    client_bytes: bytes,
    code: int,
    identifier: int = 1,
    eap_type=None,
    data: bytes = b"",
    *,
    from_ds: bool = True,
    qos: bool = False,
    ht_control: bool = False,
    version: int = 1,
) -> bytes:
    """Build an 802.11 data frame carrying an EAPOL-Type-0 (EAP) packet.

    from_ds=True is AP->client (Request); from_ds=False is client->AP (Response).
    qos/ht_control add the header fields the parser's LLC/SNAP window must tolerate.
    """
    fc0 = 0x88 if qos else 0x08                      # QoS Data (subtype 8) vs Data
    fc1 = 0x02 if from_ds else 0x01                  # from_ds vs to_ds
    if ht_control:
        fc1 |= 0x80                                  # Order bit -> +4 HT Control
    addrs = (client_bytes + bssid_bytes + bssid_bytes) if from_ds \
        else (bssid_bytes + client_bytes + bssid_bytes)
    mac_hdr = bytes([fc0, fc1]) + b"\x00\x00" + addrs + b"\x00\x00"
    if qos:
        mac_hdr += b"\x00\x00"
    if ht_control:
        mac_hdr += b"\x00\x00\x00\x00"
    llc_snap = b"\xaa\xaa\x03\x00\x00\x00\x88\x8e"
    has_type = eap_type is not None
    eap = struct.pack(">BBH", code, identifier, 4 + (1 if has_type else 0) + len(data))
    if has_type:
        eap += bytes([eap_type]) + data
    x802_1x = struct.pack(">BBH", version, 0, len(eap))   # 802.1X type 0 = EAP-Packet
    return mac_hdr + llc_snap + x802_1x + eap


@pytest.mark.parametrize(
    "code, identifier, eap_type",
    [
        (2, 1, 1),            # EAP-Response/Identity
        (1, 2, 13),           # EAP-TLS
        (1, 3, 25),           # PEAP
        (1, 4, 21),           # EAP-TTLS
        (1, 5, 4),            # MD5-Challenge
        (2, 6, 3),            # Nak
        (3, 7, None),         # Success: no Type byte
        (4, 8, None),         # Failure: no Type byte
        (1, 9, 99),           # unassigned Type, still an EapPacket
    ],
)
def test_eap_classification(code, identifier, eap_type):
    frame = _build_eap_frame(_EAP_BSSID, _EAP_CLIENT, code, identifier, eap_type)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed.type == "eap"
    assert parsed.eap_type == eap_type
    assert not hasattr(parsed, "msg_num")    # an EAP packet is not an EAPOL-Key


def test_eap_expanded_stays_eapol_so_wps_is_untouched():
    # EAP-Expanded (0xFE) carrying the WFA / SimpleConfig vendor tuple is the WSC envelope; it
    # must NOT become an EapPacket, so the sink's WPS path (_on_eapol_frame -> _on_wps_m1_frame)
    # keeps seeing it.
    wsc = b"\x00\x37\x2a" + b"\x00\x00\x00\x01" + b"\x04\x00"   # WFA id + SimpleConfig + opcode + flags
    frame = _build_eap_frame(_EAP_BSSID, _EAP_CLIENT, code=1, identifier=1, eap_type=0xFE, data=wsc)
    assert WlanFrameParser.parse_80211_frame(frame, -50).type == "eapol"


def test_non_wsc_expanded_eap_is_classified_as_eap():
    # A generic Expanded EAP (not WFA / SimpleConfig) is a real method, not the WSC envelope,
    # so it must be classified as EAP rather than silently kept on the EAPOL/WSC path.
    other = b"\x00\x00\x2a" + b"\x00\x00\x00\x09" + b"\x01\x00"   # non-WFA vendor id + type
    frame = _build_eap_frame(_EAP_BSSID, _EAP_CLIENT, code=1, identifier=1, eap_type=0xFE, data=other)
    assert WlanFrameParser.parse_80211_frame(frame, -50).type == "eap"


def test_eap_identity_username_is_never_parsed_into_a_field():
    frame = _build_eap_frame(_EAP_BSSID, _EAP_CLIENT, code=2, identifier=1, eap_type=1,
                             data=b"alice@corp.example", from_ds=False)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed.type == "eap" and parsed.eap_type == 1
    assert not hasattr(parsed, "identity") and not hasattr(parsed, "username")
    assert b"alice@corp.example" in parsed.raw    # only in the transient raw frame


def test_eap_repr_does_not_leak_identity_bytes():
    frame = _build_eap_frame(_EAP_BSSID, _EAP_CLIENT, code=2, identifier=1, eap_type=1,
                             data=b"alice@corp.example", from_ds=False)
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert "alice@corp.example" not in repr(parsed)
    assert b"alice@corp.example" in parsed.raw


@pytest.mark.parametrize("trim", [1, 2, 3, 4, 7])
def test_eap_truncated_frame_never_raises(trim):
    frame = _build_eap_frame(_EAP_BSSID, _EAP_CLIENT, code=1, eap_type=25, data=b"\x00" * 4)
    assert WlanFrameParser.parse_80211_frame(frame[:-trim], -50) is not None


@pytest.mark.parametrize("qos, ht_control",
                         [(False, False), (True, False), (False, True), (True, True)])
def test_eap_header_offsets_qos_and_ht(qos, ht_control):
    frame = _build_eap_frame(_EAP_BSSID, _EAP_CLIENT, code=1, eap_type=25,
                             qos=qos, ht_control=ht_control)
    assert WlanFrameParser.parse_80211_frame(frame, -50).eap_type == 25


@pytest.mark.parametrize("version", [1, 2, 3])
def test_eap_is_802_1x_version_agnostic(version):
    frame = _build_eap_frame(_EAP_BSSID, _EAP_CLIENT, code=1, eap_type=13, version=version)
    assert WlanFrameParser.parse_80211_frame(frame, -50).eap_type == 13


def test_parser_extracts_full_eapol_fields():
    bssid = bytes.fromhex("AABBCCDDEEFF")
    client = bytes.fromhex("112233445566")
    anonce = bytes(range(32))
    mic = bytes(range(16))

    # M1 frame
    frame = _build_eapol_frame(
        bssid_bytes=bssid,
        client_bytes=client,
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        nonce=anonce,
        mic=mic,
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert parsed.type == "eapol"
    assert parsed.msg_num == 1
    assert parsed.nonce == anonce
    assert parsed.mic == mic
    assert parsed.replay_counter == b"\x00" * 7 + b"\x01"
    assert parsed.key_data_len == 0


# ---- PMKID KDE extraction -------------------------------------------------------

def _pmkid_kde(pmkid: bytes) -> bytes:
    """Build a valid PMKID KDE: 0xDD <len=0x14> 00 0F AC 04 <pmkid>."""
    assert len(pmkid) == 16
    return b"\xdd\x14\x00\x0f\xac\x04" + pmkid


def test_parser_extracts_pmkid_from_m1_key_data():
    bssid = bytes.fromhex("AABBCCDDEEFF")
    client = bytes.fromhex("112233445566")
    pmkid = bytes.fromhex("ad2fad48da558cdfeb19cea25e2ce5af")

    frame = _build_eapol_frame(
        bssid_bytes=bssid,
        client_bytes=client,
        key_info=0x0080 | 0x0008,  # M1
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=_pmkid_kde(pmkid),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert parsed.msg_num == 1
    assert parsed.pmkid == pmkid


def test_parser_no_pmkid_when_key_data_empty():
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=b"",
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert parsed.pmkid is None


def test_parser_ignores_all_zero_pmkid():
    """Some APs ship a zero-filled placeholder KDE: not crackable; skip it."""
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=_pmkid_kde(b"\x00" * 16),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert parsed.pmkid is None


def test_parser_finds_pmkid_among_multiple_kdes():
    """Some APs concatenate multiple KDEs. PMKID may not be first."""
    pmkid = bytes.fromhex("11223344556677889900aabbccddeeff")
    # Bogus vendor KDE first, then the real PMKID KDE
    other_kde = b"\xdd\x06\x00\x0f\xac\x07\x00\x00"  # arbitrary 8-byte KDE
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=other_kde + _pmkid_kde(pmkid),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed.pmkid == pmkid


def test_parser_rejects_truncated_kde():
    """A KDE whose declared length runs past the buffer must not raise."""
    # Length byte says 0x14 (20 B follow) but we only provide 5.
    truncated = b"\xdd\x14\x00\x0f\xac\x04"
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=truncated,
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert parsed.pmkid is None


def _forge_body_len(frame: bytes, body_len: int) -> bytes:
    b = bytearray(frame)
    b[34:36] = struct.pack(">H", body_len)
    return bytes(b)


def test_forged_short_body_length_is_not_a_complete_eapol_key():
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        nonce=bytes(range(32)),
        mic=bytes(range(16)),
    )
    parsed = WlanFrameParser.parse_80211_frame(_forge_body_len(frame, 0), -42)
    assert parsed is not None and parsed.type == "eapol"
    assert parsed.msg_num == 0
    assert parsed.nonce is None and parsed.mic is None and parsed.replay_counter is None


def test_key_data_length_exceeding_declared_body_is_rejected():
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=_pmkid_kde(bytes(range(16))),
    )
    parsed = WlanFrameParser.parse_80211_frame(_forge_body_len(frame, 95), -42)
    assert parsed is not None and parsed.type == "eapol"
    assert parsed.pmkid is None and parsed.msg_num == 0


def test_well_formed_key_frame_still_parses_after_bound_check():
    pmkid = bytes.fromhex("ad2fad48da558cdfeb19cea25e2ce5af")
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=_pmkid_kde(pmkid),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed.msg_num == 1 and parsed.pmkid == pmkid


_BSSID = bytes.fromhex("AABBCCDDEEFF")
_CLIENT = bytes.fromhex("112233445566")


def _hmsg(parsed, akm=None) -> HandshakeMessage:
    return HandshakeMessage(
        raw=parsed.raw, msg_num=parsed.msg_num,
        replay_hex=(parsed.replay_counter or b"").hex(),
        nonce=parsed.nonce or b"", mic=parsed.mic or b"",
        key_data_len=parsed.key_data_len, eapol_payload=parsed.payload, akm=akm,
    )


def test_oversized_outer_body_length_withholds_payload():
    frame = _build_eapol_frame(_BSSID, _CLIENT, key_info=0x0100 | 0x0008,
                               replay_counter=b"\x00" * 7 + b"\x01",
                               nonce=bytes(range(32)), mic=bytes(range(16)))
    parsed = WlanFrameParser.parse_80211_frame(_forge_body_len(frame, 65535), -42)
    assert parsed.type == "eapol" and parsed.msg_num == 4
    assert parsed.nonce == bytes(range(32)) and parsed.mic == bytes(range(16))
    assert parsed.payload == b""


def test_inconsistent_body_with_complete_physical_padding_withholds_payload():
    frame = _build_eapol_frame(_BSSID, _CLIENT, key_info=0x0100 | 0x0008,
                               replay_counter=b"\x00" * 7 + b"\x01",
                               nonce=bytes(range(32)), mic=bytes(range(16)))
    padded = _forge_body_len(frame, 105) + b"\x00" * 10
    assert WlanFrameParser.parse_80211_frame(padded, -42).payload == b""


def test_oversized_keystone_yields_no_crackable_pair_or_hashline():
    m1 = WlanFrameParser.parse_80211_frame(
        _build_eapol_frame(_BSSID, _CLIENT, key_info=0x0080 | 0x0008,
                           replay_counter=b"\x00" * 7 + b"\x05", nonce=b"\xaa" * 32), -42)
    m4_frame = _build_eapol_frame(_BSSID, _CLIENT, key_info=0x0100 | 0x0008,
                                  replay_counter=b"\x00" * 7 + b"\x06",
                                  nonce=b"\x02" * 32, mic=b"\x11" * 16)

    good = _make_hs(_hmsg(m1, akm=2),
                    _hmsg(WlanFrameParser.parse_80211_frame(m4_frame, -42), akm=2))
    assert good.is_complete and len(crackable_pairs(good)) == 1
    assert eapol_hashlines("Net", good)

    oversized = _make_hs(
        _hmsg(m1, akm=2),
        _hmsg(WlanFrameParser.parse_80211_frame(_forge_body_len(m4_frame, 65535), -42), akm=2))
    assert not oversized.is_complete
    assert crackable_pairs(oversized) == []
    assert eapol_hashlines("Net", oversized) == []


def test_truncated_m1_pmkid_fragment_stays_usable_without_payload():
    pmkid = bytes.fromhex("ad2fad48da558cdfeb19cea25e2ce5af")
    frame = _build_eapol_frame(_BSSID, _CLIENT, key_info=0x0080 | 0x0008,
                               replay_counter=b"\x00" * 7 + b"\x01",
                               nonce=b"\xaa" * 32, key_data=_pmkid_kde(pmkid) + b"\x00" * 18)
    truncated = frame[:-18]
    parsed = WlanFrameParser.parse_80211_frame(truncated, -42)
    assert parsed.msg_num == 1 and parsed.pmkid == pmkid
    assert parsed.nonce == b"\xaa" * 32 and parsed.payload == b""


def test_truncated_m1_still_donates_anonce_to_a_full_m2():
    m1 = _build_eapol_frame(_BSSID, _CLIENT, key_info=0x0080 | 0x0008,
                            replay_counter=b"\x00" * 7 + b"\x05",
                            nonce=b"\xaa" * 32, key_data=_pmkid_kde(bytes(range(16))) + b"\x00" * 18)
    m1_parsed = WlanFrameParser.parse_80211_frame(m1[:-18], -42)
    m2_parsed = WlanFrameParser.parse_80211_frame(
        _m2(_rsn_keydata(0x02)), -42)
    hs = _make_hs(_hmsg(m1_parsed, akm=2), _hmsg(m2_parsed, akm=2))
    assert m1_parsed.payload == b"" and m2_parsed.payload != b""
    assert hs.is_complete and len(crackable_pairs(hs)) == 1


# ---- M2 client-AKM extraction ---------------------------------------------------

def _rsn_keydata(*akm_suites: int | bytes) -> bytes:
    """A minimal RSN IE (tag 48 + header) as the supplicant ships it in M2's
    cleartext Key Data."""
    selectors = b"".join(
        b"\x00\x0f\xac" + bytes([akm]) if isinstance(akm, int) else akm
        for akm in akm_suites
    )
    body = (
        b"\x01\x00"                              # Version = 1
        + b"\x00\x0f\xac\x04"                    # Group cipher = CCMP
        + b"\x01\x00" + b"\x00\x0f\xac\x04"      # 1 pairwise = CCMP
        + len(akm_suites).to_bytes(2, "little") + selectors
        + b"\x00\x00"                            # RSN capabilities
    )
    return bytes([48, len(body)]) + body


def _m2(key_data: bytes) -> bytes:
    return _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0100 | 0x0008,   # M2: MIC, !ACK, !INSTALL, key data present
        replay_counter=b"\x00" * 7 + b"\x01",
        nonce=bytes(range(32)),
        mic=bytes(range(16)),
        key_data=key_data,
    )


def test_parser_extracts_sae_client_akm_from_m2():
    parsed = WlanFrameParser.parse_80211_frame(_m2(_rsn_keydata(0x08)), -42)
    assert parsed.msg_num == 2
    assert parsed.akm == 0x08          # SAE: the useless case


def test_parser_extracts_psk_client_akm_from_m2():
    parsed = WlanFrameParser.parse_80211_frame(_m2(_rsn_keydata(0x02)), -42)
    assert parsed.akm == 0x02          # PSK: crackable


@pytest.mark.parametrize(
    "akms",
    [
        (0x02, 0x01),
        (0x02, bytes.fromhex("00112299")),
    ],
)
def test_parser_does_not_select_an_ambiguous_m2_akm(akms):
    parsed = WlanFrameParser.parse_80211_frame(_m2(_rsn_keydata(*akms)), -42)
    assert parsed.msg_num == 2
    assert parsed.akm is None
    assert parsed.akm_selection_rejected


@pytest.mark.parametrize("key_data", [
    b"\x30", b"\x30\x14\x01\x00", b"\xdd\x00\x30",
    _rsn_keydata(bytes.fromhex("00112299")),
    bytes.fromhex("30150100000fac040100000fac040100000fac02000000"),
])
def test_parser_rejects_unusable_m2_selection(key_data):
    parsed = WlanFrameParser.parse_80211_frame(_m2(key_data), -42)
    assert parsed.akm is None and parsed.akm_selection_rejected


@pytest.mark.parametrize("key_data, expected", [(b"\xdd\x00", None), (_rsn_keydata(2), 2)])
def test_parser_keeps_absent_and_valid_m2_selection_distinct(key_data, expected):
    parsed = WlanFrameParser.parse_80211_frame(_m2(key_data), -42)
    assert parsed.akm == expected and not parsed.akm_selection_rejected


def test_ambiguous_m2_akm_does_not_make_a_mixed_offer_crackable():
    m1 = WlanFrameParser.parse_80211_frame(
        _build_eapol_frame(
            _BSSID,
            _CLIENT,
            key_info=0x0080 | 0x0008,
            replay_counter=b"\x00" * 7 + b"\x01",
            nonce=b"\xaa" * 32,
        ),
        -42,
    )
    m2 = WlanFrameParser.parse_80211_frame(_m2(_rsn_keydata(0x02, 0x01)), -42)
    hs = _make_hs(_hmsg(m1), _hmsg(m2, akm=m2.akm))
    hs.akm_offered = [0x02, 0x01]
    assert crackable_pairs(hs) == []


def test_parser_no_client_akm_on_m1():
    """The AKM rides M2's RSN IE; M1 carries (at most) a PMKID KDE, no RSN IE."""
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,               # M1
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=_pmkid_kde(bytes(range(16))),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed.msg_num == 1
    assert parsed.akm is None


# ---- Handshake pair-detection tests --------------------------------------------

def _ef(msg_num, replay_int, raw=None, *, ts=0.0, nonce=None, mic=True, complete=True):
    """HandshakeMessage builder for the model-delegation tests. By default produces a
    *usable* frame: a real (non-zero) nonce, a real MIC, and a complete 802.1X
    payload, so M2/M4 qualify as MIC keystones and M1/M3 as ANonce donors, the
    way a clean capture looks. Pass mic=False for an M1-style no-MIC frame or
    complete=False to simulate a clipped payload.

    ts=0.0 (default) means 'unset' → the time-window check is skipped, so these
    tests pin the replay-counter + content rules without depending on timing."""
    return HandshakeMessage(
        raw=raw if raw is not None else bytes([msg_num, replay_int % 256]),
        msg_num=msg_num,
        replay_hex=(replay_int).to_bytes(8, "big").hex(),
        nonce=nonce if nonce is not None else bytes([msg_num]) * 32,
        mic=(b"\x11" * 16 if mic else b"\x00" * 16),
        key_data_len=0,
        eapol_payload=(bytes(120) if complete else b""),
        timestamp=ts,
    )


def _make_hs(*frames, with_beacon=True):
    hs = Handshake(
        bssid="aa:bb:cc:dd:ee:ff",
        client_mac="11:22:33:44:55:66",
        beacon_frame=b"BEACON" if with_beacon else None,
    )
    hs.messages.extend(frames)
    return hs


def test_no_beacon_means_incomplete():
    hs = _make_hs(_ef(1, 5), _ef(2, 5), with_beacon=False)
    assert not hs.is_complete


def test_m1_plus_m2_same_replay_is_complete():
    hs = _make_hs(_ef(1, 5), _ef(2, 5))
    assert hs.is_complete
    pair = hs.find_valid_pair()
    assert (pair[0].msg_num, pair[1].msg_num) == (1, 2)


def test_two_m1_retries_NOT_complete():
    """The user-reported bug: two M1s with identical replay counter were
    incorrectly being treated as a valid M1+M2 pair."""
    hs = _make_hs(_ef(1, 5), _ef(1, 5))
    assert not hs.is_complete
    assert hs.captured_messages == {1}


def test_m1_plus_m1_plus_m3_NOT_complete():
    """The exact scenario from Wireshark: two M1s, one M3, no M2 or M4."""
    hs = _make_hs(_ef(1, 5), _ef(1, 5), _ef(3, 6))
    assert not hs.is_complete


def test_m2_plus_m3_pair_replay_plus_one_is_complete():
    hs = _make_hs(_ef(2, 5), _ef(3, 6))
    assert hs.is_complete
    assert (hs.find_valid_pair()[0].msg_num, hs.find_valid_pair()[1].msg_num) == (2, 3)


def test_m2_plus_m3_within_nc_tolerance_is_complete():
    """M3 should be M2.replay+1, but hcxpcapngtool (and now we) tolerate a gap up
    to the NC value (8) and let hashcat fix the small drift, so replay 9 vs the
    expected 6 (gap 3) still pairs."""
    hs = _make_hs(_ef(2, 5), _ef(3, 9))
    assert hs.is_complete


def test_m2_plus_m3_replay_gap_beyond_nc_NOT_complete():
    """Past the NC tolerance (8), the replay mismatch is treated as unrelated."""
    hs = _make_hs(_ef(2, 5), _ef(3, 20))   # expected 6, got 20 → gap 14 > 8
    assert not hs.is_complete


def test_m3_plus_m4_same_replay_is_complete():
    hs = _make_hs(_ef(3, 7), _ef(4, 7))
    assert hs.is_complete


def test_m1_plus_m4_replay_plus_one_is_complete():
    hs = _make_hs(_ef(1, 5), _ef(4, 6))
    assert hs.is_complete


def test_replay_counter_wide_int_handles_8_byte_field():
    """Replay counters can be up to 64-bit. Ensure we compare them as
    integers, not as raw hex-string lexicographic ordering."""
    hs = _make_hs(_ef(2, 0xFF), _ef(3, 0x100))
    assert hs.is_complete


# ---- Same-instance binding (timestamp + ANonce) -------------------------------

def test_pair_rejected_when_frames_far_apart_in_time():
    """The reported bug: M1, then an M2 with a coincidentally-matching replay
    counter a minute later, are different associations → must NOT pair (the
    ANonce/SNonce would be from different PTKs → uncrackable)."""
    hs = _make_hs(_ef(1, 5, ts=1000.0), _ef(2, 5, ts=1060.0))
    assert not hs.is_complete


def test_pair_accepted_when_frames_within_window():
    hs = _make_hs(_ef(1, 5, ts=1000.0), _ef(2, 5, ts=1000.05))
    assert hs.is_complete
    pair = hs.find_valid_pair()
    assert (pair[0].msg_num, pair[1].msg_num) == (1, 2)


def test_m1_m2_valid_despite_unrelated_later_m3():
    """M1+M2 of one association is a valid pair (hashcat MESSAGEPAIR 0x00). An M3
    from a *different*, later association (different ANonce, outside the pairing
    window) does NOT invalidate it: only the M1 the M2 actually answered supplies
    the ANonce. Matches hcxpcapngtool, which has no global 'a conflicting M3
    anywhere vetoes this M1' rule."""
    hs = _make_hs(
        _ef(1, 5, ts=100.0, nonce=b"\xaa" * 32),   # M1 of association A
        _ef(2, 5, ts=100.1),                        # M2 answering it (same window)
        _ef(3, 6, ts=200.0, nonce=b"\xbb" * 32),   # unrelated later M3 (assoc B)
    )
    assert hs.is_complete
    pair = hs.find_valid_pair()
    assert (pair[0].msg_num, pair[1].msg_num) == (1, 2)
    assert pair[0].nonce == b"\xaa" * 32           # ANonce from assoc A's M1


def test_timestampless_frames_still_pair_on_replay():
    """Fixtures / pre-timestamp captures (ts unset) fall back to replay rules."""
    hs = _make_hs(_ef(1, 5), _ef(2, 5))
    assert hs.is_complete


def test_complete_instances_counts_distinct_anonces():
    """A client that completes the 4-way twice (re-association → fresh ANonce,
    new replay base) counts as two captured handshakes, not one."""
    hs = _make_hs(
        _ef(1, 5, ts=100.0, nonce=b"\xaa" * 32),
        _ef(2, 5, ts=100.1, nonce=b"\x11" * 32),
        _ef(1, 9, ts=200.0, nonce=b"\xbb" * 32),
        _ef(2, 9, ts=200.1, nonce=b"\x22" * 32),
    )
    assert hs.is_complete
    assert hs.complete_instances == 2


def test_complete_instances_one_4way_is_single_instance():
    """All four messages of ONE handshake share the AP's ANonce → one instance,
    even though several M-frame combos validate."""
    anonce = b"\xaa" * 32
    hs = _make_hs(
        _ef(1, 5, ts=100.0, nonce=anonce),
        _ef(2, 5, ts=100.1, nonce=b"\x11" * 32),
        _ef(3, 6, ts=100.2, nonce=anonce),
        _ef(4, 6, ts=100.3, nonce=b"\x11" * 32),
    )
    assert hs.complete_instances == 1


def test_captured_messages_set():
    hs = _make_hs(_ef(1, 5), _ef(1, 5), _ef(3, 6))
    assert hs.captured_messages == {1, 3}


def test_total_eapol_frames():
    hs = _make_hs(_ef(1, 5), _ef(1, 5), _ef(3, 6))
    assert hs.total_messages == 3
