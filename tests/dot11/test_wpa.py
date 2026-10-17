from wifit3.dot11.wpa import build_eapol_m2, derive_pmk, derive_ptk
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.packet import EapolPacket


def test_derive_ptk_length():
    """Verify PTK length is 64 bytes (512 bits)."""
    pmk = derive_pmk("password", b"IEEE")
    ap_mac = bytes.fromhex("000b86289b00")
    sta_mac = bytes.fromhex("001302d1e550")
    anonce = bytes(range(32))
    snonce = bytes(range(32, 64))
    ptk = derive_ptk(pmk, ap_mac, sta_mac, anonce, snonce, 64)
    assert len(ptk) == 64
    kck = ptk[:16]
    kek = ptk[16:32]
    tk = ptk[32:48]
    assert len(kck) == 16
    assert len(kek) == 16
    assert len(tk) == 16


def test_build_eapol_m2_parsed_correctly():
    """Verify built M2 frame parses as a valid EAPOL-Key frame."""
    bssid = bytes.fromhex("000b86289b00")
    sta_mac = bytes.fromhex("001302d1e550")
    snonce = bytes(range(32, 64))
    kck = b"\xaa" * 16

    frame = build_eapol_m2(bssid, sta_mac, snonce, kck, replay=1)
    assert len(frame) > 100

    parsed = WlanFrameParser.parse_80211_frame(frame, rssi=0)
    assert isinstance(parsed, EapolPacket)
    assert parsed.type == "eapol"
    assert parsed.to_ds is True
    assert parsed.bssid == "00:0b:86:28:9b:00"
    assert parsed.source == "00:13:02:d1:e5:50"
    assert parsed.dest == "00:0b:86:28:9b:00"
    assert parsed.nonce == snonce
    assert parsed.mic is not None
    assert len(parsed.mic) == 16
