"""Hashcat ``-m 22000`` hashline formatter tests."""

from wifit3.crack.hc22000_format import (
    eapol_hashlines,
    pmkid_hashline,
)
from wifit3.models import HandshakeMessage, Handshake


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


# ---- EAPOL hashlines ------------------------------------------------------------


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
