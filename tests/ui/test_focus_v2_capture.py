"""FocusViewV2 must paint the live campaign picture — driven by a real
WlanInterface (mock driver) end to end, no hardware. Mirrors
``test_focus_capture`` for v1: beacon → target → push v2 → feed M1(+PMKID)/M2,
then assert the headline, event log, client list, and that the handshake/PMKID
auto-save. Also checks the flow channel binds to the live interface (so its
sparklines sample real ``packet_stats``)."""
import pytest
from textual.app import App
from textual.widgets import RichLog, Static

from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.clients_list import ClientsList
from wifit3.ui.screens.focus_v2.flow_channel import FlowChannel
from wifit3.ui.screens.focus_v2.log_band import LogBand
from wifit3.wlan.interface import WlanInterface


@pytest.fixture(autouse=True)
def _isolate_captures_dir(monkeypatch, tmp_path):
    """Auto-save writes to ``Path("captures")`` (cwd-relative); park in tmp."""
    monkeypatch.chdir(tmp_path)


class MockDriver:
    async def set_channel(self, ch, scan=False):
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
        "eapol_payload": bytes(120),
        "eapol_pmkid": pmkid,
    }


def _log_text(band: LogBand) -> str:
    rich = band.query_one("#log-rich", RichLog)
    return "\n".join(strip.text for strip in rich.lines)


class _Host(App):
    """Minimal host that wires the interface + target the way WifiteApp does,
    then pushes the v2 screen straight in."""
    def __init__(self, iface, ap):
        super().__init__()
        self.active_interface = iface
        self.target_ap = ap

    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


@pytest.mark.asyncio
async def test_v2_surfaces_passive_handshake_and_pmkid(tmp_path):
    bssid = "aa:bb:cc:dd:ee:01"
    client = "04:2e:c1:51:43:b8"
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    iface._on_frame_parsed(_beacon(bssid, "TESTNET", 1))
    ap = iface.access_points[bssid]

    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        assert isinstance(focus, FocusViewV2)

        # The flow channel is bound to the live interface → it samples real
        # packet_stats (not the fake generator).
        flow = focus.query_one("#flow", FlowChannel)
        assert flow._iface is iface and flow._bssid == bssid

        log = focus.query_one("#log", LogBand)
        status = focus.query_one("#status", Static)
        assert "Target acquired" in _log_text(log)
        # Idle WPA target → passive listening headline.
        assert "Listening" in str(status.render())

        # Phone connects: M1 (carries a PMKID KDE) — partial so far.
        replay = b"\x00" * 8
        iface._on_frame_parsed(_eapol(bssid, client, 1, replay, to_ap=False, pmkid=b"\xaa" * 16))
        focus._tick()
        await pilot.pause()
        text = _log_text(log)
        assert "M1" in text and "ANonce" in text, text
        assert "PMKID captured" in text, text

        # M2 completes a hashcat-valid M1+M2 pair.
        iface._on_frame_parsed(_eapol(bssid, client, 2, replay, to_ap=True))
        focus._tick()
        await pilot.pause()
        text = _log_text(log)
        assert "Valid 4-Way Handshake" in text, text

        # Headline flips to a captured state; the client row is synced in.
        assert "Captured" in str(status.render()), str(status.render())
        clients = focus.query_one("#clients", ClientsList)
        assert client in clients._known, clients._known

        # Auto-save fires inline with the capture-event log (no keystroke).
        saved = {p.name for p in (tmp_path / "captures").iterdir()}
        assert any(n.endswith("_handshake.hc22000") for n in saved), saved
        assert any(n.endswith("_pmkid.hc22000") for n in saved), saved
