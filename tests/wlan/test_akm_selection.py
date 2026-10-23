import hashlib
import hmac
import struct

import pytest

from wifit3.crack.handshake import (
    crackable_pairs, handshake_uncrackable_label, pmkid_crackable, withheld_capture_label,
)
from wifit3.crack.hc22000_format import eapol_hashlines, parse_hc22000, pmkid_hashline
from wifit3.crack.wpa_psk import psk_matches
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.wpa import derive_ft_pmk_r0, ft_pmk_r1_name
from wifit3.wlan.sink import WlanSink


_AP = bytes.fromhex("020000000001")
_STA = bytes.fromhex("020000000002")
_BSSID = "02:00:00:00:00:01"
_CLIENT = "02:00:00:00:00:02"
_LLC = bytes.fromhex("aaaa03000000888e")
_PMK = hashlib.pbkdf2_hmac("sha1", b"synthetic-password", b"Lab", 4096, 32)
_PMKID = hmac.new(_PMK, b"PMK Name" + _AP + _STA, hashlib.sha1).digest()[:16]
_KDE = bytes.fromhex("dd14000fac04") + _PMKID


def _rsn(*akms):
    selectors = b"".join(bytes.fromhex("000fac") + bytes([a]) if isinstance(a, int)
                          else a for a in akms)
    body = bytes.fromhex("0100000fac040100000fac04")
    body += struct.pack("<H", len(akms)) + selectors + b"\x00\x00"
    return bytes([48, len(body)]) + body


def _beacon(rsn):
    header = b"\x80\x00\x00\x00" + b"\xff" * 6 + _AP + _AP + bytes(2)
    fixed = bytes(8) + bytes.fromhex("64001100")
    return header + fixed + b"\x00\x03Lab\x01\x01\x82" + rsn


def _sink(*akms):
    sink = WlanSink()
    _ingest(sink, _beacon(_rsn(*akms)))
    return sink


def _key(msg, *, data=b"", replay=7, nonce=None, flags=None, from_ap=None,
         descriptor=2, mic=None, client=_STA):
    if from_ap is None:
        from_ap = msg in (1, 3)
    if flags is None:
        flags = {1: 0x008A, 2: 0x010A, 3: 0x03CA, 4: 0x030A}[msg]
    if nonce is None:
        nonce = bytes([msg]) * 32
    if mic is None:
        mic = bytes(16) if msg == 1 else b"\x11" * 16
    header = (b"\x08\x02\x00\x00" + client + _AP + _AP if from_ap
              else b"\x08\x01\x00\x00" + _AP + client + _AP) + bytes(2)
    body = bytes([descriptor]) + struct.pack(">HHQ", flags, 16, replay)
    body += nonce + bytes(32) + mic + struct.pack(">H", len(data)) + data
    return header + _LLC + b"\x02\x03" + struct.pack(">H", len(body)) + body


def _assoc(*akms, subtype=0):
    header = bytes([subtype << 4, 0, 0, 0]) + _AP + _STA + _AP + bytes(2)
    fixed = bytes.fromhex("11000100") + (_AP if subtype == 2 else b"")
    return header + fixed + b"\x00\x03Lab\x01\x01\x82" + _rsn(*akms)


def _ingest(sink, frame):
    parsed = WlanFrameParser.parse_80211_frame(frame, -40)
    assert parsed is not None
    sink.update(parsed, "test")
    return parsed


def _handshake(sink):
    return sink.access_points[_BSSID].handshakes[_CLIENT]


@pytest.mark.parametrize("tail", [b"\xc0", b"\xc0\x00\x00",
                                  b"\xc0\x00\x00\x00\x00\x0f\xac"])
def test_truncated_rsn_tail_cannot_replace_required_pmf(tail):
    sink = WlanSink()
    valid = _rsn(5)[:-2] + b"\xc0\x00"
    _ingest(sink, _beacon(valid))
    body = _rsn(5)[2:-2] + tail
    invalid = bytes([48, len(body)]) + body
    parsed = _ingest(sink, _beacon(invalid))
    assert not parsed.rsn_valid
    ap = sink.access_points[_BSSID]
    assert ap.rsn_valid and ap.pmf_capable and ap.pmf_required
    assert ap.rsn_ie == valid


@pytest.mark.parametrize("offered", [(2,), (1, 2)])
@pytest.mark.parametrize("associated", [False, True])
@pytest.mark.parametrize("key_data", [
    _rsn(1, 2), _rsn(2, 1), _rsn(2, bytes.fromhex("00112299")),
    _rsn(bytes.fromhex("00112299")), b"\x30", b"\x30\x14\x01\x00",
    bytes.fromhex("30150100000fac040100000fac040100000fac02000000"),
])
def test_rejected_m2_selection_never_falls_back_to_psk(offered, associated, key_data):
    sink = _sink(*offered)
    if associated:
        _ingest(sink, _assoc(2))
    _ingest(sink, _key(1))
    parsed = _ingest(sink, _key(2, data=key_data))
    hs = _handshake(sink)
    assert parsed.akm_selection_rejected
    assert hs.messages[-1].akm is None and hs.messages[-1].akm_selection_rejected
    assert crackable_pairs(hs) == [] and eapol_hashlines("Lab", hs) == []
    assert withheld_capture_label(hs) == "Unsupported AKM"
    assert handshake_uncrackable_label(hs) == "Unsupported AKM"


@pytest.mark.parametrize("associated, key_data", [(False, _rsn(2)), (True, b"\xdd\x00")])
def test_valid_selection_and_absent_legacy_selection_remain_usable(associated, key_data):
    sink = _sink(1, 2)
    if associated:
        _ingest(sink, _assoc(2))
    _ingest(sink, _key(1))
    _ingest(sink, _key(2, data=key_data))
    assert len(crackable_pairs(_handshake(sink))) == 1


def test_rejected_m2_cannot_consume_anonce_needed_by_valid_m2():
    sink = _sink(2)
    _ingest(sink, _key(1))
    _ingest(sink, _key(2, data=_rsn(1, 2), replay=8))
    _ingest(sink, _key(2, data=_rsn(2), replay=8))
    pairs = crackable_pairs(_handshake(sink))
    assert len(pairs) == 1 and pairs[0].mic_frame.akm == 2


@pytest.mark.parametrize("akm, m1_flags, m2_flags, digest", [
    (2, 0x008A, 0x010A, hashlib.sha1), (6, 0x008B, 0x010B, hashlib.sha256),
])
def test_matching_m2_confirms_pmkid_akm_and_hashline(akm, m1_flags, m2_flags, digest):
    sink = _sink(1, akm)
    pmkid = hmac.new(_PMK, b"PMK Name" + _AP + _STA, digest).digest()[:16]
    _ingest(sink, _key(1, flags=m1_flags, data=bytes.fromhex("dd14000fac04") + pmkid))
    hs = _handshake(sink)
    assert not pmkid_crackable(hs) and pmkid_hashline("Lab", hs) is None
    _ingest(sink, _key(2, flags=m2_flags, data=_rsn(akm)))
    assert hs.pmkid_akm == akm and pmkid_crackable(hs)
    hashline = pmkid_hashline("Lab", hs)
    assert hashline == (
        f"WPA*01*{pmkid.hex()}*020000000001*020000000002*4c6162***"
        + ("03" if akm == 6 else ""))
    assert psk_matches(_PMK, parse_hc22000(hashline))
    assert not psk_matches(bytes(32), parse_hc22000(hashline))


def test_matching_ft_psk_m2_confirms_pmkid_with_its_ft_identifiers():
    sink = _sink(1, 4)
    mdid, r0khid = b"\x12\x34", b"test-r0kh"
    _, r0_name = derive_ft_pmk_r0(_PMK, b"Lab", mdid, r0khid, _STA)
    pmkid = ft_pmk_r1_name(r0_name, _AP, _STA)
    _ingest(sink, _key(1, flags=0x008B, data=bytes.fromhex("dd14000fac04") + pmkid))
    fte = bytes(82) + b"\x01\x06" + _AP + bytes([3, len(r0khid)]) + r0khid
    key_data = _rsn(4) + b"\x36\x03" + mdid + b"\x01" + bytes([55, len(fte)]) + fte
    _ingest(sink, _key(2, flags=0x010B, data=key_data))
    hs = _handshake(sink)
    assert hs.pmkid_akm == 4 and pmkid_crackable(hs)
    hashline = parse_hc22000(pmkid_hashline("Lab", hs))
    assert hashline.kind == "03" and psk_matches(_PMK, hashline)
    assert not psk_matches(bytes(32), hashline)


@pytest.mark.parametrize("m1_overrides, m2_overrides", [
    ({}, {"replay": 8}), ({}, {"from_ap": True}), ({"from_ap": False}, {}),
    ({"flags": 0x0082}, {}), ({}, {"flags": 0x0102}),
    ({"flags": 0x088A}, {}), ({}, {"flags": 0x090A}),
    ({"flags": 0x048A}, {}), ({}, {"flags": 0x050A}),
    ({"descriptor": 1}, {}), ({}, {"descriptor": 1}),
    ({"nonce": bytes(32)}, {}), ({}, {"nonce": bytes(32)}),
    ({"mic": b"\x11" * 16}, {}), ({}, {"mic": bytes(16)}),
    ({"flags": 0x008B}, {}), ({}, {"flags": 0x010B}),
    ({}, {"data": _rsn(1)}), ({}, {"data": _rsn(1, 2)}),
    ({}, {"data": _rsn(bytes.fromhex("00112299"))}), ({}, {"data": b"\xdd\x00"}),
])
def test_pmkid_selection_requires_matching_pairwise_messages(m1_overrides, m2_overrides):
    sink = _sink(1, 2)
    _ingest(sink, _key(1, **{"data": _KDE, **m1_overrides}))
    _ingest(sink, _key(2, **{"data": _rsn(2), **m2_overrides}))
    hs = _handshake(sink)
    assert hs.pmkid == _PMKID and hs.pmkid_akm is None
    assert not pmkid_crackable(hs) and pmkid_hashline("Lab", hs) is None


@pytest.mark.parametrize("interruption", [
    _assoc(1), _assoc(1, subtype=2),
    b"\xc0\x00\x00\x00" + _STA + _AP + _AP + bytes(2) + b"\x03\x00",
    b"\xa0\x00\x00\x00" + _STA + _AP + _AP + bytes(2) + b"\x03\x00",
    b"\xc0\x00\x00\x00" + b"\xff" * 6 + _AP + _AP + bytes(2) + b"\x03\x00",
    b"\xa0\x00\x00\x00" + b"\xff" * 6 + _AP + _AP + bytes(2) + b"\x03\x00",
    b"\x10\x00\x00\x00" + _STA + _AP + _AP + bytes(2) + bytes.fromhex("1100010001c0"),
    b"\x30\x00\x00\x00" + _STA + _AP + _AP + bytes(2) + bytes.fromhex("1100010001c0"),
    _key(1, nonce=b"\x99" * 32), _key(3), _key(4),
])
def test_new_exchange_cannot_confirm_an_older_pmkid(interruption):
    sink = _sink(1, 2)
    _ingest(sink, _key(1, data=_KDE))
    _ingest(sink, interruption)
    _ingest(sink, _key(2, data=_rsn(2)))
    hs = _handshake(sink)
    assert hs.pmkid_akm is None and not pmkid_crackable(hs)


def test_new_m1_with_same_pmkid_can_bind_its_own_m2():
    sink = _sink(1, 2)
    _ingest(sink, _key(1, data=_KDE))
    _ingest(sink, _key(1, data=_KDE, nonce=b"\x99" * 32, replay=10))
    _ingest(sink, _key(2, data=_rsn(2), replay=10))
    assert pmkid_crackable(_handshake(sink))


def test_new_m1_with_different_pmkid_does_not_relabel_retained_pmkid():
    sink = _sink(1, 2)
    _ingest(sink, _key(1, data=_KDE))
    _ingest(sink, _key(1, data=bytes.fromhex("dd14000fac04") + b"\x99" * 16))
    _ingest(sink, _key(2, data=_rsn(2)))
    hs = _handshake(sink)
    assert hs.pmkid == _PMKID and not pmkid_crackable(hs)


def test_confirmed_enterprise_pmkid_is_not_reclassified_by_later_psk_m2():
    sink = _sink(1, 2)
    _ingest(sink, _assoc(1))
    _ingest(sink, _key(1, data=_KDE))
    _ingest(sink, _key(2, data=_rsn(2)))
    hs = _handshake(sink)
    assert hs.pmkid_akm == 1 and not pmkid_crackable(hs)


def test_other_clients_m2_cannot_confirm_pmkid():
    sink = _sink(1, 2)
    _ingest(sink, _key(1, data=_KDE))
    _ingest(sink, _key(2, data=_rsn(2), client=bytes.fromhex("020000000003")))
    assert not pmkid_crackable(_handshake(sink))
    _ingest(sink, _key(2, data=_rsn(2)))
    assert pmkid_crackable(_handshake(sink))


def test_roaming_invalidates_pending_pmkid_selection():
    sink = _sink(1, 2)
    _ingest(sink, _key(1, data=_KDE))
    other_ap = bytes.fromhex("020000000004")
    _ingest(sink, b"\x08\x01\x00\x00" + other_ap + _STA + other_ap + bytes(2))
    _ingest(sink, _key(2, data=_rsn(2)))
    assert not pmkid_crackable(_handshake(sink))


def test_earlier_m2_cannot_confirm_later_m1():
    sink = _sink(1, 2)
    _ingest(sink, _key(2, data=_rsn(2)))
    _ingest(sink, _key(1, data=_KDE))
    assert not pmkid_crackable(_handshake(sink))


@pytest.mark.parametrize("message", [1, 2])
def test_incomplete_eapol_pdu_cannot_confirm_pmkid(message):
    sink = _sink(1, 2)
    m1 = _key(1, data=_KDE + b"\xdd\x02\x00\x00")
    m2 = _key(2, data=_rsn(2) + b"\xdd\x02\x00\x00")
    _ingest(sink, m1[:-1] if message == 1 else m1)
    _ingest(sink, m2[:-1] if message == 2 else m2)
    hs = _handshake(sink)
    assert hs.pmkid == _PMKID and not pmkid_crackable(hs)
