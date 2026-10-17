from wifit3.models import AccessPoint, HandshakeMessage, Handshake
from wifit3.models.jobs import JobAction, ToolResult, ToolStatus


def test_tool_result_reads_as_one_line_in_the_log():
    result = ToolResult(status=ToolStatus.RUNNING, value="Running (4.13%)", percent=4.13,
                        detail="rockyou.txt", actions=JobAction.STOP | JobAction.SKIP)
    assert str(result) == ("status=RUNNING, value=Running (4.13%), percent=4.13, "
                           "detail=rockyou.txt, key=None, actions=STOP|SKIP")


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


def test_handshake_add_deduplicates_and_bounds_history():
    hs = Handshake(
        bssid="00:11:22:33:44:55",
        client_mac="AA:BB:CC:DD:EE:FF",
        beacon_frame=b"beacon",
    )
    donor = _eapol(1, replay=5)
    keystone = _eapol(2, replay=5)
    assert hs.add_message(donor) is True
    assert hs.add_message(donor) is False
    assert hs.add_message(keystone) is True

    for index in range(Handshake.MAX_MESSAGES + 50):
        hs.add_message(HandshakeMessage(
            raw=b"junk" + index.to_bytes(4, "big"),
            msg_num=0,
            replay_hex=index.to_bytes(8, "big").hex(),
            nonce=b"",
            mic=b"",
            key_data_len=0,
        ))

    assert Handshake.RETAINED_MESSAGES <= len(hs.messages) <= Handshake.MAX_MESSAGES
    assert donor in hs.messages
    assert keystone in hs.messages
    assert hs.is_complete
    assert hs.has_message(b"junk" + (Handshake.MAX_MESSAGES + 49).to_bytes(4, "big"))
    assert not hs.has_message(b"junk" + (0).to_bytes(4, "big"))


def test_handshake_prune_keeps_the_best_pair_over_newer_ones():
    def frame(msg_num: int, replay: int, nonce: int) -> HandshakeMessage:
        return HandshakeMessage(
            raw=bytes([msg_num]) + replay.to_bytes(8, "big"),
            msg_num=msg_num,
            replay_hex=replay.to_bytes(8, "big").hex(),
            nonce=nonce.to_bytes(32, "big"),
            mic=b"\x11" * 16,
            key_data_len=0,
            eapol_payload=bytes(120),
        )

    hs = Handshake(bssid="00:11:22:33:44:55", client_mac="AA:BB:CC:DD:EE:FF", beacon_frame=b"beacon")
    hs.add_message(frame(1, replay=1000, nonce=1))
    hs.add_message(frame(2, replay=1000, nonce=2))
    best = hs.find_valid_pair()
    for instance in range(2, Handshake.MAX_COMPLETE_INSTANCES + 10):
        hs.add_message(frame(1, replay=instance * 1000, nonce=instance * 2 + 1))
        hs.add_message(frame(2, replay=instance * 1000 + 1, nonce=instance * 2 + 2))
    for index in range(Handshake.MAX_MESSAGES):
        hs.add_message(frame(0, replay=10**9 + index, nonce=0))

    assert len(hs.messages) <= Handshake.MAX_MESSAGES
    assert hs.find_valid_pair() == best


def test_handshake_index_tracks_direct_fixture_mutations():
    hs = Handshake(bssid="00:11:22:33:44:55", client_mac="AA:BB:CC:DD:EE:FF")
    message = _eapol(1, replay=5)
    hs.messages.append(message)

    assert hs.has_message(message.raw)
    assert hs.add_message(message) is False


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
