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


def test_wps_pbc_active_detection():
    ap = AccessPoint(bssid="00:11:22:33:44:55", wps=True)
    assert ap.wps_pbc_active is False                       # no registrar window

    # A live Push-Button walk window: PBC dev-pw-id + selected registrar.
    ap.wps_selected_registrar = True
    ap.wps_device_password_id = 0x0004
    assert ap.wps_pbc_active is True

    # A PIN-method registrar window (dev-pw-id default) is NOT PBC.
    ap.wps_device_password_id = 0x0000
    assert ap.wps_pbc_active is False

    # Selected registrar cleared (window closed) → not active.
    ap.wps_device_password_id = 0x0004
    ap.wps_selected_registrar = False
    assert ap.wps_pbc_active is False


def _eapol(msg_num: int, replay: int) -> EapolFrame:
    """A *usable* EAPOL frame: non-zero nonce, real MIC, complete 802.1X payload
    — so M2/M4 qualify as MIC keystones and M1/M3 as ANonce donors."""
    return EapolFrame(
        raw=bytes([msg_num, replay & 0xFF]),
        msg_num=msg_num,
        replay_hex=replay.to_bytes(8, "big").hex(),
        nonce=bytes([msg_num]) * 32,
        mic=b"\x11" * 16,
        key_data_len=0,
        eapol_payload=bytes(120),
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
