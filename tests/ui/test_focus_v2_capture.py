"""FocusViewV2 must paint the live campaign picture, driven by a real
WlanInterface (mock driver) end to end, no hardware. Mirrors
``test_focus_capture`` for v1: beacon → target → push v2 → feed M1(+PMKID)/M2,
then assert the headline, event log, client list, and that the handshake/PMKID
auto-save. Also checks the packet dashboard binds to the live interface (so its
sparklines sample real ``packet_stats``)."""
import pytest
import pytest_asyncio
from textual.app import App
from textual.widgets import Button, RichLog, Static

from wifit3.models import PersistedCapture
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.clients_list import ClientsList
from wifit3.ui.screens.focus_v2.packet_dashboard import PacketDashboard
from wifit3.ui.screens.focus_v2.log_band import LogBand
from wifit3.wlan.interface import WlanInterface, DeauthResult
from wifit3.wlan.sink import WlanSink

from tests.frames import pkt


@pytest.fixture(autouse=True)
def _isolate_captures_dir(monkeypatch, tmp_path):
    """Auto-save writes to ``Path("captures")`` (cwd-relative); park in tmp."""
    monkeypatch.chdir(tmp_path)


class MockDriver:
    async def set_channel(self, ch, scan=False):
        return True

    def register_rx_callback(self, cb):
        pass

    def register_disconnect_callback(self, cb):
        pass


def _beacon(bssid, ssid, ch):
    return pkt({
        "type": "beacon", "bssid": bssid, "ssid": ssid, "channel": ch,
        "rssi": -40, "encryption": "WPA2", "akms": ["PSK"], "akm_suites": [2],
        "pairwise_cipher": "CCMP", "raw": b"\xff-beacon-raw",
    })


def _eapol(bssid, client, msg_num, replay, *, to_ap, pmkid=None):
    return pkt({
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
    })


def _log_text(band: LogBand) -> str:
    rich = band.query_one("#log-rich", RichLog)
    return "\n".join(strip.text for strip in rich.lines)


class _FakeArray:
    """One-card WlanArray for the UI: it owns a WlanSink and feeds it from the interface's raw RX
    (so ``iface._on_frame_parsed(pkt)`` builds the picture), vends the interface as the selected
    radio, and delegates picture reads to the sink."""
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
        if self._iface.current_channel == ch:   # mirror the array's already-on-channel skip
            return True
        return await self._iface.set_channel(ch, scan=scan)

    async def stop_hopping(self):
        return await self._iface.stop_hopping()

    async def start_hopping(self, channels=None, interval=0.5):
        return await self._iface.start_hopping(channels, interval)

    def __getattr__(self, name):
        # access_points / clients / forged_macs / wep_store / packet_stats / register_forged_mac
        return getattr(self._sink, name)


class _Host(App):
    """Minimal host that wires the pool + target the way WifiteApp does,
    then pushes the v2 screen straight in."""
    def __init__(self, array, ap):
        super().__init__()
        self.array = array
        self.target_ap = ap
        self.pbc_enabled = True

    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


def _wpa2_target(bssid="aa:bb:cc:dd:ee:01", ssid="TESTNET", ch=1):
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    array = _FakeArray(iface)
    iface._on_frame_parsed(_beacon(bssid, ssid, ch))
    return iface, array, array.access_points[bssid]


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def focus_host():
    iface, array, ap = _wpa2_target()
    app = _Host(array, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        app.screen._tick_timer.stop()          # tests drive _tick() by hand
        yield app, app.screen, pilot


async def _rebind(host, array, ap):
    app, focus, pilot = host
    app.array, app.target_ap = array, ap
    await focus._enter_target()
    await pilot.pause(0)
    return focus


@pytest.mark.asyncio
async def test_v2_surfaces_passive_handshake_and_pmkid(tmp_path):
    bssid = "aa:bb:cc:dd:ee:01"
    client = "b2:c3:d4:e5:f6:07"
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    array = _FakeArray(iface)
    iface._on_frame_parsed(_beacon(bssid, "TESTNET", 1))
    ap = array.access_points[bssid]

    app = _Host(array, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        focus = app.screen
        assert isinstance(focus, FocusViewV2)

        # The packet dashboard is bound to the live interface → it samples real
        # packet_stats (not the fake generator).
        dash = focus.query_one("#dashboard", PacketDashboard)
        assert dash._array is app.array and dash._bssid == bssid

        log = focus.query_one("#log", LogBand)
        status = focus.query_one("#status", Static)
        assert "Target acquired" in _log_text(log)
        # Idle WPA target → passive listening headline.
        assert "Listening" in str(status.render())

        # Phone connects: M1 (carries a PMKID KDE), partial so far.
        replay = b"\x00" * 8
        iface._on_frame_parsed(_eapol(bssid, client, 1, replay, to_ap=False, pmkid=b"\xaa" * 16))
        focus._tick()
        await pilot.pause(0)
        text = _log_text(log)
        # M1 is buffered (deferred aggregation) so its tree isn't logged yet,
        # but PMKID is an immediate win banner.
        assert "PMKID captured" in text, text
        assert "Valid 4-Way Handshake" not in text, text

        # M2 completes a hashcat-valid M1+M2 pair → the aggregated tree flushes
        # immediately (first crackable pair), carrying the buffered M1 detail.
        iface._on_frame_parsed(_eapol(bssid, client, 2, replay, to_ap=True))
        focus._tick()
        await pilot.pause(0)
        text = _log_text(log)
        assert "Valid 4-Way Handshake" in text, text
        assert "M1" in text and "ANonce" in text and "M2" in text, text

        # Headline flips to a captured state; the client row is synced in.
        assert "Captured" in str(status.render()), str(status.render())
        clients = focus.query_one("#clients", ClientsList)
        assert client in clients._known, clients._known

        # Auto-save fires inline with the capture-event log (no keystroke).
        saved = {p.name for p in (tmp_path / "captures").iterdir()}
        assert any(n.endswith("_handshake.hc22000") for n in saved), saved
        assert any(n.endswith("_pmkid.hc22000") for n in saved), saved


@pytest.mark.asyncio
async def test_focus_resume_repins_channel_when_radio_drifted():
    """Returning to Focus on the SAME target must re-tune to the target's channel when the radio
    drifted (the Scanner hopper walks it off-channel while Focus is backgrounded). The
    same-target resume used to keep the view but skip the tune, parking us on a hop channel with
    zero beacons. A modal close (no drift) must NOT re-tune (don't disrupt an active attack)."""
    bssid = "aa:bb:cc:dd:ee:01"
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    array = _FakeArray(iface)
    iface._on_frame_parsed(_beacon(bssid, "TESTNET", 6))
    ap = array.access_points[bssid]
    app = _Host(array, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        focus = app.screen
        tuned: list = []

        async def _rec_set(ch, scan=False):
            tuned.append(ch)
            iface.current_channel = ch
            return True

        iface.set_channel = _rec_set

        # Hopper drifted the radio off-channel while we were in Scanner → resume re-pins to ch6.
        iface.current_channel = 11
        await focus.on_screen_resume()
        assert tuned == [6], tuned

        # Already on the target channel (e.g. a modal close) → no needless re-tune.
        tuned.clear()
        iface.current_channel = 6
        await focus.on_screen_resume()
        assert tuned == [], tuned


@pytest.mark.asyncio
async def test_v2_capture_wins_do_not_double_toast():
    """Focus does NOT toast handshake / PMKID wins from its detector. ScannerView sits under
    it on the screen stack, keeps polling its own detector over EVERY AP, and fires the toast
    (so wins on OTHER targets still notify while we're focused): a Focus toast would only
    duplicate it. The win still lands in Focus's own event log, so it isn't silent locally."""
    bssid = "aa:bb:cc:dd:ee:01"
    client = "b2:c3:d4:e5:f6:07"
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    array = _FakeArray(iface)
    iface._on_frame_parsed(_beacon(bssid, "TESTNET", 1))
    ap = array.access_points[bssid]
    app = _Host(array, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        focus = app.screen
        toasts: list = []
        focus.notify = lambda msg, **kw: toasts.append((kw.get("title"), msg))
        log = focus.query_one("#log", LogBand)

        replay = b"\x00" * 8
        iface._on_frame_parsed(_eapol(bssid, client, 1, replay, to_ap=False, pmkid=b"\xaa" * 16))
        iface._on_frame_parsed(_eapol(bssid, client, 2, replay, to_ap=True))
        focus._tick()
        await pilot.pause(0)

        titles = [t for t, _ in toasts]
        assert "PMKID captured" not in titles, toasts
        assert "Handshake captured" not in titles, toasts
        # …but the capture still surfaces in Focus's event log (Scanner owns the toast).
        text = _log_text(log)
        assert "PMKID captured" in text and "Valid 4-Way Handshake" in text, text


@pytest.mark.asyncio
async def test_v2_stop_pbc_button_frees_radio_and_suppresses_rearm():
    """The transient 'Stop PBC' button: hidden while idle, shown red while a PBC
    capture runs; pressing it stops the campaign and suppresses the per-tick
    auto-invade re-arm until the walk window closes (else it just restarts)."""
    from unittest.mock import Mock

    bssid = "aa:bb:cc:dd:ee:01"
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    array = _FakeArray(iface)
    iface._on_frame_parsed(_beacon(bssid, "TESTNET", 1))
    ap = array.access_points[bssid]                      # window CLOSED during mount
    app = _Host(array, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        focus = app.screen
        if focus._tick_timer:
            focus._tick_timer.stop()                     # drive _tick by hand: no auto-invade race
        focus._start_pbc_capture = Mock()                # never fire the real capture
        # Now open a PBC walk window (timer stopped, so no real capture auto-arms).
        # PBC is only stopped while the window is open; suppression holds until close.
        ap.wps = True
        ap.wps_selected_registrar = True
        ap.wps_device_password_id = 0x0004
        assert ap.wps_pbc_active
        stop_btn = focus.query_one("#btn-stop-pbc", Button)

        focus._refresh_buttons()
        assert stop_btn.display is False                 # idle → hidden

        camp = Mock()                                    # stand in for a running capture
        camp.done = False
        camp.stopped = False
        focus._pbc_campaign = camp
        focus._refresh_buttons()
        assert stop_btn.display is True and str(stop_btn.label) == "Stop PBC"

        # Stop: request_stop fired, handle KEPT (so the closing 'stopped' line lands
        # from _finish_pbc_capture once the campaign drains, not mid-logtree), re-arm
        # suppressed.
        focus._user_stop_pbc()
        camp.request_stop.assert_called_once()
        assert focus._pbc_campaign is camp
        assert focus._pbc_user_stopped is True

        # Draining (stopped, not yet done) → the button shows a disabled 'Stopping…'.
        camp.stopped = True
        focus._refresh_buttons()
        assert stop_btn.display is True and stop_btn.disabled is True
        assert "Stopping" in str(stop_btn.label)

        # Finishes → _finish_pbc_capture logs the clean closing leaf + drops the handle;
        # the button hides. Window's still open, but re-arm stays suppressed.
        camp.done = True
        focus._tick()
        assert focus._pbc_campaign is None
        assert "stopped" in _log_text(focus.query_one("#log", LogBand))
        focus._refresh_buttons()
        assert stop_btn.display is False
        focus._start_pbc_capture.assert_not_called()

        # Window closes → suppression clears so a fresh window re-invades.
        ap.wps_selected_registrar = False
        assert not ap.wps_pbc_active
        focus._tick()
        assert focus._pbc_user_stopped is False


def test_save_line_elides_bssid_and_timestamp():
    """The save note keeps the readable head (essid) + tail (kind.ext) and elides
    the BSSID + epoch middle that bloated the log."""
    import types

    from wifit3.ui.screens.focus_v2.screen import _save_line

    new = types.SimpleNamespace(was_new=True, path=types.SimpleNamespace(
        name="NETGEAR2G_aa-bb-cc-dd-ee-01_1781842298_handshake.hc22000"))
    line = _save_line(new)
    assert "saved: captures/NETGEAR2G_…_handshake.hc22000" in line
    assert "aa-bb-cc" not in line and "1781842298" not in line

    old = types.SimpleNamespace(was_new=False, path=types.SimpleNamespace(
        name="net_aa-bb-cc-dd-ee-ff_123_pmkid.hc22000"))
    assert "exists: captures/net_…_pmkid.hc22000" in _save_line(old)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")  # ui/conftest.py
async def test_default_focus_screen_is_v2():
    """``push_screen("focus")`` installs ``FocusViewV2``: the sole Focus screen."""
    app = WifiteApp()
    async with app.run_test():
        assert isinstance(app.get_screen("focus"), FocusViewV2)


@pytest.mark.asyncio(loop_scope="module")
async def test_v2_recovered_wps_psk_shows_in_status(focus_host):
    """After a WPS PBC/PIN win the recovered PSK lives on the AP; the v2 headline
    shows a terminal banner instead of decaying back to 'Listening' once the
    capture task finishes."""
    iface, array, ap = _wpa2_target()
    ap.wps_pbc_psk = "hunter2"          # as set by a successful PBC capture
    focus = await _rebind(focus_host, array, ap)
    status = str(focus.query_one("#status", Static).render())
    assert "WPS PSK recovered" in status, status


@pytest.mark.asyncio(loop_scope="module")
async def test_v2_reenter_same_target_no_duplicate_client_ids(focus_host):
    """Scanner→Focus→back→Focus on the SAME target must not crash with
    DuplicateIds. The client list reconciles in place instead of clear-then-
    remount, which raced Textual's async row removal."""
    bssid = "aa:bb:cc:dd:ee:01"
    client = "aa:bb:cc:dd:ee:03"
    rid = "cl-" + client.replace(":", "")
    iface, array, ap = _wpa2_target(bssid)
    iface._on_frame_parsed(pkt({"type": "data", "bssid": bssid, "source": client,
                                "dest": bssid, "rssi": -55, "raw": b"d"}))

    focus = await _rebind(focus_host, array, ap)
    _, _, pilot = focus_host
    focus._tick()
    await pilot.pause(0)
    assert len(focus.query(f"#{rid}")) == 1                 # mounted once

    # Re-acquire the same target (as a Scanner→Focus return does).
    await focus._enter_target()
    focus._tick()
    await pilot.pause(0)
    assert len(focus.query(f"#{rid}")) == 1                 # still one, no dup/crash
    assert client in focus.query_one("#clients", ClientsList)._known


@pytest.mark.asyncio(loop_scope="module")
async def test_v2_pmf_required_disables_deauth_and_logs(focus_host):
    """A PMF-Required AP refuses unauthenticated deauth: every deauth control
    (broadcast + per-client ✕) is greyed, and the requirement is logged."""
    bssid = "aa:bb:cc:dd:ee:01"
    client = "9c:b6:d0:1a:2b:3c"
    iface, array, ap = _wpa2_target(bssid)
    ap.pmf_required = True
    iface._on_frame_parsed(pkt({"type": "data", "bssid": bssid, "source": client,
                                "dest": bssid, "rssi": -60, "raw": b"d"}))
    focus = await _rebind(focus_host, array, ap)
    _, _, pilot = focus_host
    focus._tick()
    await pilot.pause(0)
    clients = focus.query_one("#clients", ClientsList)
    deauth_btns = list(clients.query(Button))
    assert deauth_btns and all(b.disabled for b in deauth_btns), deauth_btns
    assert "PMF Required" in _log_text(focus.query_one("#log", LogBand))


@pytest.mark.asyncio(loop_scope="module")
async def test_v2_target_acquired_log_names_encryption(focus_host):
    """The acquisition log carries the encryption family next to the name."""
    iface, array, ap = _wpa2_target()     # WPA2 beacon
    focus = await _rebind(focus_host, array, ap)
    text = _log_text(focus.query_one("#log", LogBand))
    assert "Target acquired" in text and "WPA2" in text, text


@pytest.mark.asyncio(loop_scope="module")
async def test_v2_button_wiring(focus_host):
    """The attack buttons are encryption-conditional (derive_buttons), the inline
    ✕ maps to the right client, and that mapping reaches iface.deauth_client, proving
    the trigger wiring with NO live TX (the recorder stands in for the radio)."""
    bssid = "aa:bb:cc:dd:ee:01"
    client = "9c:b6:d0:1a:2b:3c"
    iface, array, ap = _wpa2_target(bssid)
    # Register a real client (a data frame) so a ✕ row appears.
    iface._on_frame_parsed(pkt({"type": "data", "bssid": bssid, "source": client,
                                "dest": bssid, "rssi": -67, "raw": b"d"}))

    deauthed = []

    async def _record_deauth(ap_bssid, client_bssid, rounds=10):
        deauthed.append((ap_bssid, client_bssid, rounds))
        return DeauthResult(client_sent=rounds, ap_sent=rounds, measured=True)

    iface.deauth_client = _record_deauth  # stand in for the radio: no real TX

    focus = await _rebind(focus_host, array, ap)
    _, _, pilot = focus_host

    # WPA2 (no WPS, not WPA3): PMKID + Deauth + EvilTwin apply. The rest hide.
    assert focus.query_one("#btn-pmkid", Button).display is True
    assert focus.query_one("#btn-deauth", Button).display is True
    assert focus.query_one("#btn-eviltwin", Button).display is True
    for bid in ("#btn-gen-ivs", "#btn-chop", "#btn-wps-pin"):
        assert focus.query_one(bid, Button).display is False, bid

    # The inline ✕ resolves to its client, and the handler reaches deauth.
    clients = focus.query_one("#clients", ClientsList)
    focus._tick()
    await pilot.pause(0)
    btn_id = next(b for b, m in clients._by_button.items() if m == client)
    assert clients.client_mac(btn_id) == client
    await focus._run_deauth_selected(client)
    assert deauthed == [(bssid, client, 10)], deauthed


@pytest.mark.asyncio
async def test_v2_wep_initial_load_surfaces_history_and_listening():
    """An already-cracked WEP target: the event log shows the saved key chip + a
    'Listening for WEP IVs' line on load (mirrors v1's _log_persisted_history),
    and the headline reads the recovered banner while idle."""
    bssid = "aa:bb:cc:dd:ee:06"
    iface = WlanInterface(MockDriver(), "wlanX", "Mock card")
    array = _FakeArray(iface)
    iface._on_frame_parsed(_beacon(bssid, "dd-wrt", 6))
    ap = array.access_points[bssid]
    ap.encryption = "WEP"
    ap.persisted = [PersistedCapture(
        type="WEP", value="6162636465", timestamp=1748487420, path="dd-wrt_wep.txt")]

    app = _Host(array, ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        focus = app.screen
        text = _log_text(focus.query_one("#log", LogBand))
        assert "Existing captures" in text, text
        assert "WEP Key" in text and "abcde" in text, text          # the saved key chip
        assert "listening for WEP IVs" in text, text
        # Idle → recovered banner; the wep iv dashboard row is present.
        assert "WEP key recovered" in str(focus.query_one("#status", Static).render())
        dash = focus.query_one("#dashboard", PacketDashboard)
        assert "wep_iv" in {r.key for r in dash._rows}
        # The WEP status is painted as dashboard footer lines (always-on
        # usable-IV count, idle → just that one line, no fake-auth), NOT a
        # separate band, so it steals no row: the mid band still abuts the bottom
        # band directly.
        assert dash._footer is not None
        footer_text = " ".join(t.plain for t in dash._footer)
        assert "Usable IVs" in footer_text and "/10k" in footer_text
        assert focus.query_one("#mid").region.bottom == focus.query_one("#bottom").region.y
