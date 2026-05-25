"""Focus View must surface PASSIVELY-captured WPA handshakes/PMKIDs with no
attack running — just parking on the target should reflect frames the radio
hears. Drives a real WlanInterface (mock driver) end to end: beacon → target →
Focus → feed M1(+PMKID)/M2 → assert the CAPTURE panel + event log update."""
import pytest
from textual.widgets import Label, Button, RichLog

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.focus import FocusView
from wifit3.wlan.interface import WlanInterface


class MockDriver:
    async def set_channel(self, ch):
        return True

    def register_rx_callback(self, cb):
        pass


def _beacon(bssid, ssid, ch):
    return {
        "type": "beacon", "bssid": bssid, "ssid": ssid, "channel": ch,
        "rssi": -40, "encryption": "WPA2", "akms": ["PSK"],
        "pairwise_cipher": "CCMP", "raw": b"\xff-beacon-raw",
    }


def _eapol(bssid, client, msg_num, replay, *, to_ap, pmkid=None):
    """to_ap=True → client→AP (dest=bssid); else AP→client (dest=client)."""
    return {
        "type": "eapol", "bssid": bssid, "rssi": -40,
        "source": client if to_ap else bssid,
        "dest": bssid if to_ap else client,
        "raw": bytes([msg_num]) + b"-eapol-" + replay,
        "eapol_replay_counter": replay,
        "eapol_msg_num": msg_num,
        "eapol_nonce": b"\x01" * 32,
        "eapol_mic": b"\x02" * 16,
        "eapol_key_data_len": 0,
        "eapol_payload": b"",
        "eapol_pmkid": pmkid,
    }


def _log_text(log: RichLog) -> str:
    return "\n".join(strip.text for strip in log.lines)


@pytest.mark.asyncio
async def test_focus_surfaces_passive_handshake_and_pmkid():
    app = WifiteApp()
    async with app.run_test() as pilot:
        iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
        app.active_interface = iface

        bssid = "aa:bb:cc:dd:ee:01"
        client = "04:2e:c1:51:43:b8"
        # Discover the AP from a beacon — this is the live registry object the
        # interface mutates, and the same object Focus polls.
        iface._on_frame_parsed(_beacon(bssid, "TESTNET", 1))
        ap = iface.access_points[bssid]
        app.target_ap = ap

        app.push_screen("focus")
        await pilot.pause()
        focus = app.screen
        assert isinstance(focus, FocusView)

        hs_label = focus.query_one("#lbl-handshake", Label)
        pmkid_label = focus.query_one("#lbl-pmkid", Label)
        assert "Not captured" in str(hs_label.render())
        assert "Not captured" in str(pmkid_label.render())

        # Phone connects: M1 (carries a PMKID KDE) then M2, same replay → a
        # hashcat-valid M1+M2 pair. No attack button pressed.
        replay = b"\x00" * 8
        iface._on_frame_parsed(_eapol(bssid, client, 1, replay, to_ap=False, pmkid=b"\xaa" * 16))
        iface._on_frame_parsed(_eapol(bssid, client, 2, replay, to_ap=True))

        # Drive the same per-tick refresh the 10 Hz timer runs — called directly
        # so the test doesn't race the wall-clock timer under a busy event loop.
        focus.update_ui()
        await pilot.pause()

        assert "Captured" in str(hs_label.render()), str(hs_label.render())
        assert "Captured" in str(pmkid_label.render()), str(pmkid_label.render())
        assert focus.query_one("#btn-save", Button).disabled is False

        log_text = _log_text(focus.query_one("#focus-event-log", RichLog))
        assert "HANDSHAKE COMPLETE" in log_text, log_text
        assert "PMKID" in log_text, log_text
