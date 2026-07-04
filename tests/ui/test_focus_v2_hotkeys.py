"""Focus v2 command-bar (footer hotkey) behaviour.

The footer keys are driven off the SAME state as the top-bar buttons: check_action
translates each into Textual's tri-state (False → hidden, None → greyed, True →
active). Covers the deauth-clients gate, the campaign keys mirroring derive_buttons
per encryption family, the shared WPS-PBC toggle, and the PBC auto-capture gate.
Driven by a real WlanInterface (mock driver), no hardware.
"""
import pytest
from textual.app import App
from textual.widgets._footer import FooterKey

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.clients_list import ClientsList
from wifit3.ui.screens.focus_v2.log_band import LogBand
from wifit3.wlan.interface import WlanInterface

from tests.frames import pkt


class MockDriver:
    async def set_channel(self, ch, scan=False):
        return True

    def register_rx_callback(self, cb):
        pass


def _wpa2_beacon(bssid, ssid, ch):
    return pkt({
        "type": "beacon", "bssid": bssid, "ssid": ssid, "channel": ch,
        "rssi": -40, "encryption": "WPA2", "akms": ["PSK"], "akm_suites": [2],
        "pairwise_cipher": "CCMP", "raw": b"\xff-beacon-raw",
    })


def _client_data(bssid, client):
    return pkt({"type": "data", "bssid": bssid, "source": client,
                "dest": bssid, "rssi": -60, "raw": b"d"})


def _log_text(focus) -> str:
    from textual.widgets import RichLog
    rich = focus.query_one("#log", LogBand).query_one("#log-rich", RichLog)
    return "\n".join(strip.text for strip in rich.lines)


class _Host(App):
    """Minimal host wiring interface + target like WifiteApp, incl. the shared
    ``pbc_enabled`` flag Focus reads/toggles."""
    def __init__(self, iface, ap):
        super().__init__()
        self.active_interface = iface
        self.target_ap = ap
        self.pbc_enabled = True

    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


def _wpa2_target(bssid="aa:bb:cc:dd:ee:01"):
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    iface._on_frame_parsed(_wpa2_beacon(bssid, "TESTNET", 1))
    return iface, iface.access_points[bssid]


# ----- deauth (item 1) -------------------------------------------------------


@pytest.mark.asyncio
async def test_deauth_hotkey_gated_on_clients():
    """'d' is hidden with no clients (False), active once a client appears (True),
    and greyed when the AP requires PMF (None)."""
    bssid, client = "aa:bb:cc:dd:ee:01", "9c:b6:d0:1a:2b:3c"
    iface, ap = _wpa2_target(bssid)
    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        focus._tick()
        assert focus.check_action("deauth_all", ()) is False       # no clients → hidden

        iface._on_frame_parsed(_client_data(bssid, client))
        focus._tick()
        assert focus.check_action("deauth_all", ()) is True        # a client → active

        ap.pmf_required = True
        focus._tick()
        assert focus.check_action("deauth_all", ()) is None        # PMF → greyed


@pytest.mark.asyncio
async def test_deauth_broadcast_button_hidden_without_clients():
    """The panel's pinned 'Deauth all' button follows the same rule as the 'd'
    key — hidden with no clients, shown once one appears."""
    bssid, client = "aa:bb:cc:dd:ee:02", "9c:b6:d0:1a:2b:3c"
    iface, ap = _wpa2_target(bssid)
    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        focus._tick()
        await pilot.pause()
        bcast = focus.query_one("#clients", ClientsList).query_one("#deauth-all")
        assert bcast.display is False

        iface._on_frame_parsed(_client_data(bssid, client))
        focus._tick()
        await pilot.pause()
        assert bcast.display is True


# ----- campaign hotkeys mirror the buttons (item 3) --------------------------


@pytest.mark.asyncio
async def test_campaign_hotkeys_mirror_buttons_wpa2():
    """On a plain WPA2 AP (no WPS, not WPA3): PMKID is the only plausible attack,
    so 'p' is active and every other campaign key is hidden — exactly the button
    row's visibility (test_v2_button_wiring)."""
    iface, ap = _wpa2_target()
    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        focus._tick()
        assert focus.check_action("campaign", ("pmkid",)) is True
        for camp in ("wep", "chop", "wps", "wpa3down"):
            assert focus.check_action("campaign", (camp,)) is False, camp


@pytest.mark.asyncio
async def test_campaign_hotkeys_wep_chop_greyed_until_replay():
    """On a WEP AP: 'r' (Replay) is active, 'c' (ChopChop) is greyed until the
    replay campaign owns the radio, and 'p' (PMKID) is hidden (wrong family)."""
    bssid = "aa:bb:cc:dd:ee:06"
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    iface._on_frame_parsed(_wpa2_beacon(bssid, "dd-wrt", 6))
    ap = iface.access_points[bssid]
    ap.encryption = "WEP"
    ap.akm_suites = []          # a real WEP AP carries no PSK AKM → PMKID hidden
    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        focus._tick()
        assert focus.check_action("campaign", ("wep",)) is True
        assert focus.check_action("campaign", ("chop",)) is None    # visible, disabled
        assert focus.check_action("campaign", ("pmkid",)) is False  # hidden


@pytest.mark.asyncio
async def test_campaign_and_deauth_keys_hidden_with_no_target():
    """The demo / no-target path (geometry tests) must hide every conditional key
    rather than crash — check_action short-circuits on a null target."""
    iface, ap = _wpa2_target()
    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        focus._target_ap = None
        assert focus.check_action("campaign", ("pmkid",)) is False
        assert focus.check_action("deauth_all", ()) is False
        assert focus.check_action("wps_pbc_mode", ()) is True       # non-conditional


@pytest.mark.asyncio
async def test_footer_shows_campaign_keys_per_family():
    """End to end: the rendered footer carries only the family-relevant attack
    keys — 'p' for WPA2 (not 'r'/'c'); 'r' + greyed 'c' for WEP (not 'p')."""
    iface, ap = _wpa2_target()
    app = _Host(iface, ap)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        focus._tick()
        await pilot.pause()
        keys = {k.key for k in focus.query(FooterKey)}
        assert "p" in keys
        assert "r" not in keys and "c" not in keys
        # 'd' still hidden (no clients); 'w' always available.
        assert "w" in keys and "d" not in keys


@pytest.mark.asyncio
async def test_action_campaign_dispatches_to_toggle():
    """action_campaign routes a key to its campaign's toggle via the dispatch map
    (the button's twin) — verified without launching a real campaign."""
    iface, ap = _wpa2_target()
    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        fired = []
        focus._campaign_toggles["pmkid"] = lambda: fired.append("pmkid")
        focus.action_campaign("pmkid")
        assert fired == ["pmkid"]


# ----- WPS PBC toggle shared across screens (item 2) -------------------------


def test_wifite_app_defaults_pbc_enabled_on():
    """The shared flag lives on the app, on by default (the one active-TX
    exception to passive-by-default)."""
    assert WifiteApp().pbc_enabled is True


@pytest.mark.asyncio
async def test_w_toggles_shared_pbc_flag(tmp_path, monkeypatch):
    """Focus 'w' flips app.pbc_enabled (the same setting Scanner toggles) and logs
    the new state."""
    monkeypatch.chdir(tmp_path)
    iface, ap = _wpa2_target()
    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        assert app.pbc_enabled is True
        focus.action_wps_pbc_mode()
        assert app.pbc_enabled is False
        assert "disabled" in _log_text(focus)
        focus.action_wps_pbc_mode()
        assert app.pbc_enabled is True


@pytest.mark.asyncio
async def test_focus_pbc_autocapture_gated_on_flag(tmp_path, monkeypatch):
    """Focus's per-tick PBC auto-capture only fires when app.pbc_enabled is set —
    so the shared 'w' toggle actually silences the one auto-TX in Focus too."""
    monkeypatch.chdir(tmp_path)
    bssid = "aa:bb:cc:dd:ee:07"
    iface, ap = _wpa2_target(bssid)
    ap.wps = True                        # WPS present, but the walk window is closed…
    app = _Host(iface, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        focus = app.screen
        started = []
        focus._start_pbc_capture = lambda a: started.append(a)
        # …open it only now, after the recorder is in place, so nothing auto-fires
        # during mount (which would leave a real capture busy and mask the gate).
        ap.wps_selected_registrar = True
        ap.wps_device_password_id = 0x0004
        assert ap.wps_pbc_active and not ap.has_psk

        app.pbc_enabled = False
        focus._tick()
        assert started == []             # disabled → no auto-invade

        app.pbc_enabled = True
        focus._tick()
        assert started == [ap]           # enabled → auto-invade fires
