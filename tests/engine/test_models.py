import pytest
from wifit3.engine.models import AccessPoint, Client, EapolFrame, Handshake


def test_access_point_model_defaults():
    ap = AccessPoint(bssid="00:11:22:33:44:55", ssid="Test_WiFi", signal=-50)
    assert ap.bssid == "00:11:22:33:44:55"
    assert ap.ssid == "Test_WiFi"
    assert ap.signal == -50
    assert ap.beacons == 0
    assert ap.wpa3 is False
    assert ap.pmf_capable is False


def test_wep_key_counts_as_capture():
    ap = AccessPoint(bssid="00:11:22:33:44:55", encryption="WEP")
    assert ap.wep_key is None
    assert ap.has_capture is False
    ap.wep_key = bytes.fromhex("6162636465")
    assert ap.has_capture is True   # gates the Save button + WEP save path


def _eapol(msg_num: int, replay: int) -> EapolFrame:
    return EapolFrame(
        raw=bytes([msg_num, replay & 0xFF]),
        msg_num=msg_num,
        replay_hex=replay.to_bytes(8, "big").hex(),
        nonce=b"\x00" * 32,
        mic=b"\x00" * 16,
        key_data_len=0,
    )


def test_handshake_is_complete():
    hs = Handshake(bssid="00:11:22:33:44:55", client_mac="AA:BB:CC:DD:EE:FF")
    assert not hs.is_complete

    hs.beacon_frame = b"fake_beacon"
    assert not hs.is_complete

    # Single M1 alone → not yet a pair
    hs.eapol_frames.append(_eapol(1, replay=5))
    assert not hs.is_complete

    # A matching M2 (same replay counter) completes the pair
    hs.eapol_frames.append(_eapol(2, replay=5))
    assert hs.is_complete
