"""Focus v2 command-bar (footer hotkey) behaviour.

The footer keys are driven off the SAME state as the top-bar buttons: check_action
translates each into Textual's tri-state (False → hidden, None → greyed, True →
active). Covers the deauth-clients screen, the campaign keys mirroring derive_buttons
per encryption family, the shared WPS-PBC toggle, and the PBC auto-capture guard.
Driven by a real WlanInterface (mock driver), no hardware.
"""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from textual.app import App
from textual.widgets._footer import FooterKey

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.clients_list import ClientsList
from wifit3.ui.screens.focus_v2.log_band import LogBand
from wifit3.wlan.interface import WlanInterface
from wifit3.wlan.sink import WlanSink

from tests.frames import pkt


class MockDriver:
    async def set_channel(self, ch, scan=False):
        return True

    def register_rx_callback(self, cb):
        pass

    def register_disconnect_callback(self, cb):
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


class _FakeArray:
    """One-card WlanArray for the UI: owns a WlanSink fed from the interface's raw RX (so
    ``iface._on_frame_parsed(pkt)`` builds the picture), vends the interface as the selected radio,
    and delegates picture reads to the sink."""
    def __init__(self, iface):
        self._iface = iface
        self._sink = WlanSink()
        iface.on_tx = self._sink.record_tx
        iface.register_rx_callback(lambda pkt: self._sink.update(pkt, iface.name))

    @property
    def members(self):
        return [self._iface]

    def select_iface(self, channel):
        return self._iface

    def get_access_points(self):
        return self._sink.get_access_points()

    async def set_channel(self, ch, scan=False):
        if self._iface.current_channel == ch:
            return True
        return await self._iface.set_channel(ch, scan=scan)

    async def stop_hopping(self):
        return await self._iface.stop_hopping()

    async def start_hopping(self, channels=None, interval=0.5):
        return await self._iface.start_hopping(channels, interval)

    def __getattr__(self, name):
        return getattr(self._sink, name)


class _Host(App):
    """Minimal host wiring the pool + target like WifiteApp, incl. the shared
    ``pbc_enabled`` flag Focus reads/toggles."""
    def __init__(self, array, ap):
        super().__init__()
        self.array = array
        self.target_ap = ap
        self.pbc_enabled = True

    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


def _wpa2_target(bssid="aa:bb:cc:dd:ee:01"):
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    array = _FakeArray(iface)
    iface._on_frame_parsed(_wpa2_beacon(bssid, "TESTNET", 1))
    return iface, array, array.access_points[bssid]


def _wep_target(bssid="aa:bb:cc:dd:ee:06"):
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    array = _FakeArray(iface)
    iface._on_frame_parsed(_wpa2_beacon(bssid, "dd-wrt", 6))
    ap = array.access_points[bssid]
    ap.encryption = "WEP"
    ap.akm_suites = []          # a real WEP AP carries no PSK AKM → PMKID hidden
    return iface, array, ap


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def focus_host():
    iface, array, ap = _wpa2_target()
    app = _Host(array, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        app.screen._tick_timer.stop()   # tests drive _tick() by hand
        yield app, app.screen, pilot


async def _rebind(host, array, ap):
    """Point the shared screen at a fresh target; full state reset."""
    app, focus, pilot = host
    app.array, app.target_ap = array, ap
    app.pbc_enabled = True          # reset the one sticky app-level flag between scenarios
    await focus._enter_target()
    await pilot.pause(0)
    return focus


# ----- deauth (item 1) -------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_deauth_keys_visible_on_psk_blocked_on_pmf(focus_host):
    """'d' is the one-shot broadcast deauth; Shift+D toggles the automatic
    deauth campaign. Both are active on WPA2-PSK and blocked once PMF protects
    deauth frames."""
    bssid, client = "aa:bb:cc:dd:ee:01", "9c:b6:d0:1a:2b:3c"
    iface, array, ap = _wpa2_target(bssid)
    focus = await _rebind(focus_host, array, ap)
    focus._tick()
    assert focus.check_action("deauth_all", ()) is True           # manual broadcast
    assert focus.check_action("campaign", ("deauth",)) is True    # automatic campaign

    iface._on_frame_parsed(_client_data(bssid, client))
    focus._tick()
    assert focus.check_action("deauth_all", ()) is True           # still active with a client
    assert focus.check_action("campaign", ("deauth",)) is True

    ap.pmf_required = True
    focus._tick()
    assert focus.check_action("deauth_all", ()) is None           # PMF → visible but greyed
    assert focus.check_action("campaign", ("deauth",)) is False   # PMF → hidden


@pytest.mark.asyncio(loop_scope="module")
async def test_deauth_broadcast_button_always_visible(focus_host):
    """The panel's pinned 'Deauth all' button is always visible: a broadcast deauth
    is valid with no known clients (it hits every associated STA)."""
    bssid, client = "aa:bb:cc:dd:ee:02", "9c:b6:d0:1a:2b:3c"
    iface, array, ap = _wpa2_target(bssid)
    focus = await _rebind(focus_host, array, ap)
    _, _, pilot = focus_host
    focus._tick()
    await pilot.pause(0)
    bcast = focus.query_one("#clients", ClientsList).query_one("#deauth-all")
    assert bcast.display is True                                # visible with no clients

    iface._on_frame_parsed(_client_data(bssid, client))
    focus._tick()
    await pilot.pause(0)
    assert bcast.display is True                                # still visible with a client


# ----- campaign hotkeys mirror the buttons (item 3) --------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_campaign_hotkeys_mirror_buttons_wpa2(focus_host):
    """On a plain WPA2 AP (no WPS, not WPA3): PMKID and automatic Deauth are
    plausible campaigns, so 'p' and Shift+D are active and every other campaign
    key is hidden, exactly the button row's visibility (test_v2_button_wiring)."""
    iface, array, ap = _wpa2_target()
    focus = await _rebind(focus_host, array, ap)
    focus._tick()
    assert focus.check_action("campaign", ("pmkid",)) is True
    assert focus.check_action("campaign", ("deauth",)) is True
    for camp in ("wep", "chop", "wps"):
        assert focus.check_action("campaign", (camp,)) is False, camp


@pytest.mark.asyncio(loop_scope="module")
async def test_campaign_hotkeys_wep_chop_greyed_until_replay(focus_host):
    """On a WEP AP: 'r' (Replay) is active, 'c' (ChopChop) is greyed until the
    replay campaign owns the radio, and 'p' (PMKID) is hidden (wrong family)."""
    iface, array, ap = _wep_target()
    focus = await _rebind(focus_host, array, ap)
    focus._tick()
    assert focus.check_action("campaign", ("wep",)) is True
    assert focus.check_action("campaign", ("chop",)) is None    # visible, disabled
    assert focus.check_action("campaign", ("pmkid",)) is False  # hidden


@pytest.mark.asyncio(loop_scope="module")
async def test_campaign_and_deauth_keys_hidden_with_no_target(focus_host):
    """The demo / no-target path (geometry tests) must hide every conditional key
    rather than crash: check_action short-circuits on a null target."""
    iface, array, ap = _wpa2_target()
    focus = await _rebind(focus_host, array, ap)
    focus._target_ap = None
    assert focus.check_action("campaign", ("pmkid",)) is False
    assert focus.check_action("campaign", ("deauth",)) is False
    assert focus.check_action("wps_pbc_mode", ()) is True       # non-conditional


@pytest.mark.asyncio
async def test_footer_shows_campaign_keys_per_family():
    """End to end: the rendered footer carries only the family-relevant attack
    keys: 'p' for WPA2 (not 'r'/'c'); 'r' + greyed 'c' for WEP (not 'p').

    Own boot: the rendered Footer builds its FooterKey children at mount; a shared
    screen's _enter_target skips that."""
    iface, array, ap = _wpa2_target()
    app = _Host(array, ap)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(0)
        focus = app.screen
        focus._tick()
        # The Footer rebuilds its FooterKey children reactively; under full-suite
        # load a single pause can race that rebuild (empty query). Wait for it.
        keys: set = set()
        for _ in range(20):
            await pilot.pause(0)
            keys = {k.key for k in focus.query(FooterKey)}
            if keys:
                break
        assert "p" in keys
        assert "r" not in keys and "c" not in keys
        # 'd' is manual broadcast deauth; Shift+D toggles the Deauth campaign.
        assert "w" in keys and "d" in keys
        assert "D" in keys


@pytest.mark.asyncio(loop_scope="module")
async def test_deauth_button_click_routes_to_campaign_toggle(focus_host, monkeypatch):
    """Clicking the Deauth campaign button toggles the campaign. Its id ('btn-deauth')
    ends with '-deauth', exactly like the inline client ✕, so on_button_pressed must
    match it BEFORE the endswith('-deauth') branch (the earlier bug: the click fell
    through to the inline path, resolved to no client, and did nothing)."""
    iface, array, ap = _wpa2_target()
    focus = await _rebind(focus_host, array, ap)
    fired = []
    monkeypatch.setattr(focus, "_toggle_deauth", lambda: fired.append("deauth"))
    await focus.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="btn-deauth")))
    assert fired == ["deauth"]


@pytest.mark.asyncio(loop_scope="module")
async def test_action_deauth_all_dispatches_manual_broadcast(focus_host, monkeypatch):
    """The plain 'd' key starts the one-shot broadcast deauth worker, not the
    automatic DeauthCampaign toggle."""
    iface, array, ap = _wpa2_target()
    focus = await _rebind(focus_host, array, ap)
    fired = []

    async def _manual():
        pass

    monkeypatch.setattr(focus, "_run_deauth_broadcast", _manual)

    def _record_worker(coro, **kwargs):
        fired.append(kwargs)
        coro.close()

    monkeypatch.setattr(focus, "run_worker", _record_worker)
    focus.action_deauth_all()
    assert fired == [{"exclusive": True}]


@pytest.mark.asyncio(loop_scope="module")
async def test_action_campaign_dispatches_to_toggle(focus_host, monkeypatch):
    """action_campaign routes a key to its campaign's toggle via the dispatch map
    (the button's twin), verified without launching a real campaign."""
    iface, array, ap = _wpa2_target()
    focus = await _rebind(focus_host, array, ap)
    fired = []
    monkeypatch.setitem(focus._campaign_toggles, "pmkid", lambda: fired.append("pmkid"))
    focus.action_campaign("pmkid")
    assert fired == ["pmkid"]


# ----- WPS PBC toggle shared across screens (item 2) -------------------------


def test_wifite_app_defaults_pbc_enabled_on():
    """The shared flag lives on the app, on by default (the one active-TX
    exception to passive-by-default)."""
    assert WifiteApp().pbc_enabled is True


@pytest.mark.asyncio(loop_scope="module")
async def test_w_toggles_shared_pbc_flag(focus_host, tmp_path, monkeypatch):
    """Focus 'w' flips app.pbc_enabled (the same setting Scanner toggles) and logs
    the new state."""
    monkeypatch.chdir(tmp_path)
    iface, array, ap = _wpa2_target()
    focus = await _rebind(focus_host, array, ap)
    app, _, _ = focus_host
    assert app.pbc_enabled is True
    focus.action_wps_pbc_mode()
    assert app.pbc_enabled is False
    assert "disabled" in _log_text(focus)
    focus.action_wps_pbc_mode()
    assert app.pbc_enabled is True


@pytest.mark.asyncio(loop_scope="module")
async def test_focus_pbc_autocapture_gated_on_flag(focus_host, tmp_path, monkeypatch):
    """Focus's per-tick PBC auto-capture only fires when app.pbc_enabled is set,
    so the shared 'w' toggle actually silences the one auto-TX in Focus too."""
    monkeypatch.chdir(tmp_path)
    bssid = "aa:bb:cc:dd:ee:07"
    iface, array, ap = _wpa2_target(bssid)
    ap.wps = True                        # WPS present, but the walk window is closed…
    focus = await _rebind(focus_host, array, ap)
    app, _, _ = focus_host
    started = []
    monkeypatch.setattr(focus, "_start_pbc_capture", lambda a: started.append(a))
    # …open it only now, after the recorder is in place, so nothing auto-fires
    # during the rebind (which would leave a real capture busy and mask the guard).
    ap.wps_selected_registrar = True
    ap.wps_device_password_id = 0x0004
    assert ap.wps_pbc_active and not ap.has_psk

    app.pbc_enabled = False
    focus._tick()
    assert started == []             # disabled → no auto-invade

    app.pbc_enabled = True
    focus._tick()
    assert started == [ap]           # enabled → auto-invade fires
