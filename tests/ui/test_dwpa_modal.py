import json

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Label

import wifit3.persist.config as cfg
from wifit3.vault.tools.dwpa_api import PotfileEntry, network_hash
from wifit3.models import HandshakeMessage
from wifit3.persist.config import Config, DwpaSettings
from wifit3.persist.pcap import FourWayHandshake
from wifit3.ui.vault.modals.dwpa import DwpaConfigModal
from tests.ui.wait_until import wait_until

_KEY = "0123456789abcdef0123456789abcdef"
_BSSID = "aa:bb:cc:dd:ee:ff"
_SSID = "HomeNet"


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "_PATH", tmp_path / "config.toml")
    saved = Config.dwpa
    Config.dwpa = DwpaSettings()
    yield
    Config.dwpa = saved


@pytest.fixture
def handshake():
    m2 = HandshakeMessage(raw=b"m2 frame", msg_num=2, replay_hex="00" * 8, nonce=b"" * 32,
                          mic=b"" * 16, timestamp=1700000000.0)
    return FourWayHandshake(bssid=_BSSID, client_mac="02:00:00:00:00:02", ssid=_SSID, beacon=b"beacon",
                            messages=(m2,), authorized=False, hashline=None)


async def _open(app, pilot, handshake):
    dismissed = []
    app.push_screen(DwpaConfigModal(handshake, _BSSID, _SSID), dismissed.append)
    await pilot.pause(0)
    return dismissed


async def test_the_form_starts_empty_and_names_the_handshake(handshake):
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, handshake)

        assert app.screen.query_one("#dwpa-url", Input).value == ""
        assert app.screen.query_one("#dwpa-key", Input).value == ""
        named = str(app.screen.query_one("#dwpa-handshake", Label).render())
        assert "M2" in named and "…00:02" in named
        assert str(app.screen.query_one("#dwpa-upload", Button).label) == "Upload"
        assert str(app.screen.query_one("#dwpa-cancel", Button).label) == "Cancel"


async def test_the_dialog_fits_the_viewport_and_scrolls_its_fields(handshake):
    app = _Host()
    async with app.run_test(size=(40, 12)) as pilot:
        await _open(app, pilot, handshake)

        dialog = app.screen.query_one("#dwpa-dialog")
        body = app.screen.query_one("#dwpa-body", VerticalScroll)
        buttons = app.screen.query_one("#dwpa-buttons")
        assert dialog.region.x >= 0 and dialog.region.right <= app.size.width
        assert dialog.region.y >= 0 and dialog.region.bottom <= app.size.height
        assert body.virtual_size.height > body.size.height
        assert buttons.parent is body
        body.scroll_end(animate=False)
        await pilot.pause(0)
        assert buttons.region.bottom <= body.content_region.bottom


async def test_upload_saves_the_settings_and_dismisses(handshake, dwpa_service):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        app.screen.query_one("#dwpa-upload", Button).press()
        await wait_until(pilot, lambda: bool(dismissed))

        [request] = dwpa_service.requests
        assert (request.method, request.path) == ("POST", "/?submit=")
        assert request.header("Cookie") == f"key={_KEY}"
        assert b'filename="handshake.pcap"' in request.body
        assert b"beacon" in request.body and b"m2 frame" in request.body
        assert dismissed == [True]
        assert Config.dwpa == DwpaSettings(url=dwpa_service.url, user_key=_KEY)

        Config.dwpa = DwpaSettings()
        Config.load()
        assert Config.dwpa == DwpaSettings(url=dwpa_service.url, user_key=_KEY)


async def test_an_empty_key_is_not_sent(handshake, dwpa_service):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        app.screen.query_one("#dwpa-key", Input).value = ""
        app.screen.query_one("#dwpa-upload", Button).press()
        await pilot.pause(0)

        assert dwpa_service.requests == []
        assert dismissed == []


async def test_a_missing_url_is_not_sent(handshake, dwpa_service):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        app.screen.query_one("#dwpa-upload", Button).press()
        await pilot.pause(0)

        assert dwpa_service.requests == []
        assert dismissed == []


async def test_a_rejected_capture_shows_the_message_and_frees_the_form(handshake, dwpa_service):
    dwpa_service.answer(b"<pre>Not a valid capture file. We support pcap and pcapng.</pre>")
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        app.screen.query_one("#dwpa-upload", Button).press()
        result = app.screen.query_one("#dwpa-result", Label)
        await wait_until(pilot, lambda: "Not a valid capture file." in str(result.render()))

        assert dismissed == []
        assert not app.screen.query_one("#dwpa-upload", Button).disabled
        assert Config.dwpa == DwpaSettings(url=dwpa_service.url, user_key=_KEY)
        Config.dwpa = DwpaSettings()
        Config.load()
        assert Config.dwpa == DwpaSettings(url=dwpa_service.url, user_key=_KEY)


async def test_cancel_dismisses_without_uploading(handshake, dwpa_service):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-cancel", Button).press()
        await pilot.pause(0)
    assert dismissed == [None]
    assert dwpa_service.requests == []


async def _query(app, pilot, result, expected: str) -> None:
    app.screen.query_one("#dwpa-query", Button).press()
    await wait_until(pilot, lambda: expected in str(result.render()))


async def test_the_query_asks_about_the_networks_bssid_and_ssid(handshake, dwpa_service):
    digest = network_hash(_BSSID, _SSID)
    dwpa_service.answer(json.dumps({digest[:4]: [digest[-8:]]}).encode())
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _query(app, pilot, result, "cracked")

        [request] = dwpa_service.requests
        assert (request.method, request.path) == ("POST", "/bmacssid")
        assert json.loads(request.body) == [digest[:4]]
        assert Config.dwpa == DwpaSettings(url=dwpa_service.url)
        assert "Possibly already cracked on DWPA" in str(result.render())


async def test_a_network_without_an_ssid_cannot_be_queried(handshake, dwpa_service):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(DwpaConfigModal(handshake, _BSSID, None))
        await pilot.pause(0)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        app.screen.query_one("#dwpa-query", Button).press()
        await pilot.pause(0)

        assert dwpa_service.requests == []


async def test_a_failed_query_shows_the_servers_message(handshake, dwpa_service):
    dwpa_service.answer(b"", status=500)
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        result = app.screen.query_one("#dwpa-result", Label)
        await _query(app, pilot, result, "HTTP 500")

        assert "Possibly already cracked" not in str(result.render())
        assert not app.screen.query_one("#dwpa-query", Button).disabled


class _StoreKeys:
    def __init__(self) -> None:
        self.networks = []

    def __call__(self, networks):
        self.networks = list(networks)


async def _retrieve(app, pilot, result, expected: str) -> None:
    app.screen.query_one("#dwpa-retrieve", Button).press()
    await wait_until(pilot, lambda: expected in str(result.render()))


async def test_retrieve_hands_the_cracked_keys_to_the_vault(handshake, dwpa_service):
    dwpa_service.answer(b"aabbccddeeff:020000000003:TestAP-DWPA:88888888\n"
                        b"020000000002:020000000003:Elsewhere:hunter22\n")
    store = _StoreKeys()
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(DwpaConfigModal(handshake, _BSSID, _SSID, store))
        await pilot.pause(0)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "2 cracked keys on DWPA")

        [request] = dwpa_service.requests
        assert (request.method, request.path) == ("GET", "/?api&dl=1")
        assert request.header("Cookie") == f"key={_KEY}"
        assert store.networks == [
            PotfileEntry("aa:bb:cc:dd:ee:ff", "02:00:00:00:00:03", "TestAP-DWPA", "88888888"),
            PotfileEntry("02:00:00:00:00:02", "02:00:00:00:00:03", "Elsewhere", "hunter22")]
        assert Config.dwpa == DwpaSettings(url=dwpa_service.url, user_key=_KEY)


async def test_a_downloaded_key_for_the_selected_handshake_is_announced(handshake, dwpa_service,
                                                                        monkeypatch):
    dwpa_service.answer(b"aabbccddeeff:020000000003:HomeNet:88888888\n"
                        b"020000000002:020000000003:Elsewhere:hunter22\n")
    app = _Host()
    async with app.run_test() as pilot:
        announced = []
        monkeypatch.setattr(app, "notify", lambda message, *a, **k: announced.append(str(message)))
        await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "cracked keys on DWPA")

        assert any("88888888" in message and "HomeNet" in message for message in announced)
        assert not any("hunter22" in message for message in announced)


class _NotifiedHost(_Host):
    def __init__(self) -> None:
        super().__init__()
        self.notified: list[str] = []

    def notify(self, message, **kwargs) -> None:
        self.notified.append(str(message))


async def test_the_announced_ssid_and_psk_are_not_read_as_markup(handshake, dwpa_service):
    dwpa_service.answer(b"aabbccddeeff:020000000003:[b]HomeNet:pass[/]word\n")
    app = _NotifiedHost()
    async with app.run_test() as pilot:
        await _open(app, pilot, handshake)
        app.screen.query_one("#dwpa-url", Input).value = dwpa_service.url
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "cracked key on DWPA")

        assert app.notified == [r"DWPA recovered the PSK for \[b]HomeNet: pass\[/]word"]
