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

from wifit3.persist.config import Config
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.clients_list import ClientsList, ClientWidget
from wifit3.ui.screens.focus_v2.log_band import LogBand
from wifit3.wlan.interface import WlanInterface, DeauthResult
from wifit3.wlan.sink import WlanSink

from tests.frames import pkt


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


def test_save_line_elides_bssid_and_timestamp():
    """The save note keeps the readable head (essid) + tail (kind.ext) and elides
    the BSSID + epoch middle that bloated the log."""
    import types

    from wifit3.ui.screens.focus_v2.screen import _save_line

    new = types.SimpleNamespace(was_new=True, path=types.SimpleNamespace(
        name="NETGEAR2G_aa-bb-cc-dd-ee-01_1781842298_handshake.hc22000"))
    line = _save_line(new)
    assert f"saved: {Config.captures_dir}/NETGEAR2G_…_handshake.hc22000" in line
    assert "aa-bb-cc" not in line and "1781842298" not in line

    old = types.SimpleNamespace(was_new=False, path=types.SimpleNamespace(
        name="net_aa-bb-cc-dd-ee-ff_123_pmkid.hc22000"))
    assert f"exists: {Config.captures_dir}/net_…_pmkid.hc22000" in _save_line(old)


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
    assert client in focus.query_one("#clients", ClientsList)._rows


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

    # The inline ✕ posts DeauthRequested(client).
    clients = focus.query_one("#clients", ClientsList)
    focus._tick()
    await pilot.pause(0)
    assert isinstance(clients._rows[client], ClientWidget)
    await focus._run_deauth_selected(client)
    assert deauthed == [(bssid, client, 10)], deauthed
