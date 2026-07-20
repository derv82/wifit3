"""Hashcat ``-m 22000`` hashline formatter tests."""

from wifit3.crack.hc22000_format import (
    eapol_hashline,
    eapol_hashlines,
    format_ap_hashlines,
    pmkid_hashline,
)
from wifit3.persist.hc22000_write import write_hc22000
from wifit3.models import AccessPoint, HandshakeMessage, Handshake


# ---- Test fixtures --------------------------------------------------------------


def _eapol_payload(mic: bytes = b"\xFF" * 16, key_data_len: int = 0) -> bytes:
    """Build a 99-byte (+ key_data) EAPOL payload with a non-zero MIC so we
    can verify the writer zeros it before hex-encoding."""
    pl = bytearray(99 + key_data_len)
    pl[0] = 0x02   # 802.1X version 2
    pl[1] = 0x03   # 802.1X type = EAPOL-Key
    pl[2:4] = (95 + key_data_len).to_bytes(2, "big")
    pl[4] = 0x02   # Key Desc Type = RSN
    pl[5:7] = b"\x00\x8a"  # Key Info (M1-ish, doesn't matter for format tests)
    pl[81:97] = mic
    pl[97:99] = key_data_len.to_bytes(2, "big")
    return bytes(pl)


def _ef(
    msg_num: int,
    replay: int = 0,
    nonce: bytes = None,
    mic: bytes = None,
    key_data_len: int = 0,
    payload_mic: bytes = None,
) -> HandshakeMessage:
    nonce = nonce if nonce is not None else bytes(range(32))
    mic = mic if mic is not None else b"\xAA" * 16
    payload_mic = payload_mic if payload_mic is not None else mic
    return HandshakeMessage(
        raw=b"\x00" * 24,
        msg_num=msg_num,
        replay_hex=replay.to_bytes(8, "big").hex(),
        nonce=nonce,
        mic=mic,
        key_data_len=key_data_len,
        eapol_payload=_eapol_payload(mic=payload_mic, key_data_len=key_data_len),
    )


def _hs(ssid: str = "TestNet", *frames, with_beacon: bool = True, pmkid: bytes = None) -> Handshake:
    hs = Handshake(
        bssid="aa:bb:cc:dd:ee:ff",
        client_mac="11:22:33:44:55:66",
        beacon_frame=b"BEACON" if with_beacon else None,
        pmkid=pmkid,
    )
    hs.messages.extend(frames)
    return hs


# ---- PMKID hashline -------------------------------------------------------------


def test_pmkid_hashline_basic():
    pmkid = bytes.fromhex("ad2fad48da558cdfeb19cea25e2ce5af")
    hs = _hs(pmkid=pmkid)
    line = pmkid_hashline("MyWiFi", hs)
    expected = (
        "WPA*01"
        f"*{pmkid.hex()}"
        "*aabbccddeeff"
        "*112233445566"
        f"*{b'MyWiFi'.hex()}"
        "***"
    )
    assert line == expected


def test_pmkid_hashline_none_when_no_pmkid():
    hs = _hs()
    assert pmkid_hashline("X", hs) is None


def test_pmkid_hashline_none_when_no_ssid():
    hs = _hs(pmkid=b"\x00" * 16)
    assert pmkid_hashline("", hs) is None


def test_pmkid_hashline_rejects_wrong_length():
    hs = _hs(pmkid=b"\x00" * 15)  # malformed
    assert pmkid_hashline("X", hs) is None


# ---- EAPOL hashline -------------------------------------------------------------


def test_eapol_hashline_m1_m2():
    anonce = bytes(b"\xA0" + b"\x00" * 31)
    snonce = bytes(b"\xB0" + b"\x00" * 31)
    mic = b"\xCC" * 16
    m1 = _ef(1, replay=5, nonce=anonce, payload_mic=b"\x00" * 16)
    m2 = _ef(2, replay=5, nonce=snonce, mic=mic, key_data_len=22)
    hs = _hs("Net", m1, m2)

    line = eapol_hashline("Net", hs)
    assert line is not None
    fields = line.split("*")
    # WPA*02*MIC*MACAP*MACSTA*ESSID*ANONCE*EAPOL*PAIR  → 9 fields
    assert fields[0] == "WPA"
    assert fields[1] == "02"
    assert fields[2] == "cc" * 16                       # MIC
    assert fields[3] == "aabbccddeeff"                  # MACAP
    assert fields[4] == "112233445566"                  # MACSTA
    assert fields[5] == b"Net".hex()                    # ESSID
    assert fields[6] == anonce.hex()                    # ANonce from M1
    assert fields[8] == "80"                            # M1+M2 (0x00) | NC bit (0x80)


def test_eapol_hashline_zeros_mic_in_payload():
    """The MIC bytes inside the EAPOL hex field must be zero: hashcat
    expects to fill them itself when verifying a candidate."""
    mic = b"\xCC" * 16
    m1 = _ef(1, replay=5, payload_mic=b"\x00" * 16)
    m2 = _ef(2, replay=5, mic=mic, key_data_len=22, payload_mic=mic)
    hs = _hs("Net", m1, m2)

    line = eapol_hashline("Net", hs)
    eapol_hex = line.split("*")[7]
    eapol_bytes = bytes.fromhex(eapol_hex)
    # MIC slot in the payload is bytes [81:97]
    assert eapol_bytes[81:97] == b"\x00" * 16
    # And the MIC field of the hashline still carries the real MIC
    assert line.split("*")[2] == mic.hex()


def test_eapol_hashline_m2_m3_pair_byte():
    m2 = _ef(2, replay=5, key_data_len=22)
    m3 = _ef(3, replay=6, key_data_len=56)
    hs = _hs("Net", m2, m3)
    line = eapol_hashline("Net", hs)
    assert line is not None
    assert line.split("*")[8] == "82"  # M2+M3 (0x02) | NC bit (0x80)


def test_eapol_hashline_m3_m4_pair_byte():
    m3 = _ef(3, replay=7, key_data_len=56)
    m4 = _ef(4, replay=7, key_data_len=0)
    hs = _hs("Net", m3, m4)
    line = eapol_hashline("Net", hs)
    assert line.split("*")[8] == "85"  # M3+M4 (0x05) | NC bit (0x80)


def test_eapol_hashline_m1_m4_pair_byte():
    m1 = _ef(1, replay=8)
    m4 = _ef(4, replay=9, key_data_len=0)
    hs = _hs("Net", m1, m4)
    line = eapol_hashline("Net", hs)
    assert line.split("*")[8] == "81"  # M1+M4 (0x01) | NC bit (0x80)


def test_eapol_hashline_no_valid_pair_returns_none():
    # Two M1 retries: the very bug we just fixed in is_complete
    m1a = _ef(1, replay=5)
    m1b = _ef(1, replay=5)
    hs = _hs("Net", m1a, m1b)
    assert eapol_hashline("Net", hs) is None


def test_eapol_hashlines_one_line_per_instance():
    """A client that completes the 4-way twice (distinct ANonce / replay base)
    yields two independently-crackable WPA*02 lines, not one."""
    a1 = b"\xA0" + b"\x00" * 31
    a2 = b"\xB0" + b"\x00" * 31
    hs = _hs(
        "Net",
        _ef(1, replay=5, nonce=a1),
        _ef(2, replay=5, nonce=b"\x11" + b"\x00" * 31, key_data_len=22),
        _ef(1, replay=9, nonce=a2),
        _ef(2, replay=9, nonce=b"\x22" + b"\x00" * 31, key_data_len=22),
    )
    lines = eapol_hashlines("Net", hs)
    assert len(lines) == 2
    assert {ln.split("*")[6] for ln in lines} == {a1.hex(), a2.hex()}  # ANonces
    # Every line is structurally valid: WPA, type 02, 9 *-separated fields.
    for ln in lines:
        fields = ln.split("*")
        assert fields[0] == "WPA" and fields[1] == "02" and len(fields) == 9


def test_eapol_hashline_hidden_ssid_returns_none():
    m1 = _ef(1, replay=5)
    m2 = _ef(2, replay=5, key_data_len=22)
    hs = _hs("", m1, m2)
    assert eapol_hashline("", hs) is None


def test_eapol_hashline_truncated_payload_returns_none():
    m1 = _ef(1, replay=5)
    m2 = _ef(2, replay=5, key_data_len=22)
    m2.eapol_payload = b""  # simulate truncated capture
    hs = _hs("Net", m1, m2)
    assert eapol_hashline("Net", hs) is None


# ---- AP-level + file writer -----------------------------------------------------


def test_format_ap_hashlines_emits_both_pmkid_and_eapol():
    pmkid = b"\x11" * 16
    m1 = _ef(1, replay=1)
    m2 = _ef(2, replay=1, key_data_len=22)
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
    hs = Handshake(
        bssid=ap.bssid,
        client_mac="11:22:33:44:55:66",
        beacon_frame=b"BEACON",
        pmkid=pmkid,
    )
    hs.messages.extend([m1, m2])
    ap.handshakes[hs.client_mac] = hs

    lines = format_ap_hashlines(ap)
    assert len(lines) == 2
    assert lines[0].startswith("WPA*01*")
    assert lines[1].startswith("WPA*02*")


def test_format_ap_hashlines_hidden_ssid_yields_empty():
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid=None)
    hs = Handshake(
        bssid=ap.bssid,
        client_mac="11:22:33:44:55:66",
        beacon_frame=b"BEACON",
        pmkid=b"\x11" * 16,
    )
    ap.handshakes[hs.client_mac] = hs
    assert format_ap_hashlines(ap) == []


def test_write_hc22000_creates_file(tmp_path):
    m1 = _ef(1, replay=1)
    m2 = _ef(2, replay=1, key_data_len=22)
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
    hs = Handshake(
        bssid=ap.bssid,
        client_mac="11:22:33:44:55:66",
        beacon_frame=b"BEACON",
    )
    hs.messages.extend([m1, m2])
    ap.handshakes[hs.client_mac] = hs

    path = tmp_path / "x.hc22000"
    n = write_hc22000(path, ap)
    assert n == 1
    content = path.read_text(encoding="utf-8")
    assert content.startswith("WPA*02*")
    assert content.endswith("\n")
    assert content.count("\n") == 1  # single line + trailing newline


def test_write_hc22000_no_hashlines_creates_no_file(tmp_path):
    # AP with hidden SSID + a handshake → no hashlines emittable
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid=None)
    hs = Handshake(
        bssid=ap.bssid,
        client_mac="11:22:33:44:55:66",
        beacon_frame=b"BEACON",
    )
    hs.messages.extend([_ef(1, 1), _ef(2, 1, key_data_len=22)])
    ap.handshakes[hs.client_mac] = hs

    path = tmp_path / "should_not_exist.hc22000"
    n = write_hc22000(path, ap)
    assert n == 0
    assert not path.exists()
