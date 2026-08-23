from wifit3.models import AccessPoint, HandshakeMessage, Handshake, PersistedCapture


def test_access_point_model_defaults():
    ap = AccessPoint(bssid="00:11:22:33:44:55", ssid="Test_WiFi", signal_by_card={"wlan0": -50})
    assert ap.bssid == "00:11:22:33:44:55"
    assert ap.ssid == "Test_WiFi"
    assert ap.signal == -50                 # property: strongest RSSI across receiving cards
    assert ap.beacons == 0
    assert ap.wpa3 is False
    assert ap.pmf_capable is False


def test_is_hidden():
    """Hidden until we hold a usable SSID: None and the '<hidden>' placeholder both count."""
    assert AccessPoint(bssid="00:11:22:33:44:55").is_hidden is True
    assert AccessPoint(bssid="00:11:22:33:44:55", ssid="<hidden>").is_hidden is True
    assert AccessPoint(bssid="00:11:22:33:44:55", ssid="").is_hidden is True
    assert AccessPoint(bssid="00:11:22:33:44:55", ssid="Rai2.4").is_hidden is False


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


def test_has_psk():
    """has_psk gates the opportunistic PBC re-invade: true once we hold the
    passphrase from any source (live PBC/PIN this session, or a prior session's
    WPS capture on disk), but a PIN with no PSK does not count."""
    ap = AccessPoint(bssid="00:11:22:33:44:55")
    assert ap.has_psk is False                              # nothing yet
    assert ap.known_psk is None

    # Live PBC win this session.
    ap.wps_pbc_psk = "hunter2pbc"
    assert ap.has_psk is True
    assert ap.known_psk == "hunter2pbc"

    # Live PIN-cracked PSK this session.
    ap2 = AccessPoint(bssid="00:11:22:33:44:66")
    ap2.wps_pin_psk = "hunter2pin"
    assert ap2.has_psk is True
    assert ap2.known_psk == "hunter2pin"

    # A bare PIN with no recovered PSK does NOT block. PBC still worth running.
    ap3 = AccessPoint(bssid="00:11:22:33:44:77")
    ap3.wps_pin = "12345670"
    assert ap3.has_psk is False
    assert ap3.known_psk is None

    # A prior session's WPS capture loaded from disk (PBC or PIN file → kind WPS).
    ap4 = AccessPoint(bssid="00:11:22:33:44:88")
    ap4.persisted = [PersistedCapture(type="WPS", timestamp=0, value="diskpsk", path="x_wps_pbc.txt")]
    assert ap4.has_psk is True
    assert ap4.known_psk == "diskpsk"

    # Other persisted kinds (HS/PMKID/WEP) are not a PSK → no block.
    ap5 = AccessPoint(bssid="00:11:22:33:44:99")
    ap5.persisted = [
        PersistedCapture(type="HS", timestamp=0, path="x_handshake.hc22000"),
        PersistedCapture(type="WEP", timestamp=0, value="abcde", path="x_wep_key.txt"),
    ]
    assert ap5.has_psk is False
    assert ap5.known_psk is None


def _eapol(msg_num: int, replay: int) -> HandshakeMessage:
    """A *usable* EAPOL frame: non-zero nonce, real MIC, complete 802.1X payload,
    so M2/M4 qualify as MIC keystones and M1/M3 as ANonce donors."""
    return HandshakeMessage(
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
    hs.messages.append(_eapol(1, replay=5))
    assert not hs.is_complete

    # A matching M2 (same replay counter) completes the pair
    hs.messages.append(_eapol(2, replay=5))
    assert hs.is_complete
