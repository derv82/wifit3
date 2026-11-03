import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Label

import wifit3.persist.config as cfg
import wifit3.ui.vault.modals.dwpa as mod
from wifit3.vault.tools.dwpa_api import DwpaError, PotfileEntry
from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.config import Config, DwpaSettings
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
def capture(tmp_path):
    path = tmp_path / "handshake.pcap"
    path.write_bytes(b"capture")
    return PersistedCapture(type=CaptureType.HS, timestamp=0, path=str(path),
                            bssid="02:00:00:00:00:01", ssid="test")


@pytest.fixture
def uploads(monkeypatch):
    calls = []

    def fake_upload(url, key, capture_path):
        calls.append((url, key, capture_path))
        return ""

    monkeypatch.setattr(mod, "upload_capture", fake_upload)
    return calls


async def _open(app, pilot, capture):
    dismissed = []
    app.push_screen(DwpaConfigModal(capture, _BSSID, _SSID), dismissed.append)
    await pilot.pause(0)
    return dismissed


async def test_the_form_starts_empty_and_names_the_capture(capture):
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, capture)

        assert app.screen.query_one("#dwpa-url", Input).value == ""
        assert app.screen.query_one("#dwpa-key", Input).value == ""
        assert "handshake.pcap" in str(app.screen.query_one("#dwpa-capture", Label).render())
        assert str(app.screen.query_one("#dwpa-upload", Button).label) == "Upload"
        assert str(app.screen.query_one("#dwpa-cancel", Button).label) == "Cancel"


async def test_the_dialog_fits_the_viewport_and_scrolls_its_fields(capture):
    app = _Host()
    async with app.run_test(size=(40, 12)) as pilot:
        await _open(app, pilot, capture)

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


async def test_upload_saves_the_settings_and_dismisses(capture, uploads):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        app.screen.query_one("#dwpa-upload", Button).press()
        await wait_until(pilot, lambda: bool(dismissed))

        assert uploads == [("https://dwpa.example", _KEY, capture.path)]
        assert dismissed == [True]
        assert Config.dwpa == DwpaSettings(url="https://dwpa.example", user_key=_KEY)

        Config.dwpa = DwpaSettings()
        Config.load()
        assert Config.dwpa == DwpaSettings(url="https://dwpa.example", user_key=_KEY)


@pytest.mark.parametrize("key", ["", _KEY.upper(), "0123", "zz" * 16])
async def test_an_invalid_key_is_not_sent(capture, uploads, key):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = key
        app.screen.query_one("#dwpa-upload", Button).press()
        await pilot.pause(0)

        assert uploads == []
        assert dismissed == []


async def test_a_missing_url_is_not_sent(capture, uploads):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        app.screen.query_one("#dwpa-upload", Button).press()
        await pilot.pause(0)

        assert uploads == []
        assert dismissed == []


async def test_a_rejected_capture_shows_the_message_and_frees_the_form(capture, monkeypatch):
    def fail(url, key, capture_path):
        raise DwpaError("Not a valid capture file.")

    monkeypatch.setattr(mod, "upload_capture", fail)
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        app.screen.query_one("#dwpa-upload", Button).press()
        result = app.screen.query_one("#dwpa-result", Label)
        await wait_until(pilot, lambda: "Not a valid capture file." in str(result.render()))

        assert dismissed == []
        assert not app.screen.query_one("#dwpa-upload", Button).disabled
        assert Config.dwpa == DwpaSettings(url="https://dwpa.example", user_key=_KEY)
        Config.dwpa = DwpaSettings()
        Config.load()
        assert Config.dwpa == DwpaSettings(url="https://dwpa.example", user_key=_KEY)


async def test_cancel_dismisses_without_uploading(capture, uploads):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-cancel", Button).press()
        await pilot.pause(0)
    assert dismissed == [None]
    assert uploads == []


@pytest.fixture
def queries(monkeypatch):
    calls = []
    found = [False]

    def fake_query(url, bssid, ssid):
        calls.append((url, bssid, ssid))
        return found[0]

    monkeypatch.setattr(mod, "network_leaked", fake_query)
    return calls, found


async def _query(app, pilot, result, expected: str) -> None:
    app.screen.query_one("#dwpa-query", Button).press()
    await wait_until(pilot, lambda: expected in str(result.render()))


async def test_the_query_asks_about_the_networks_bssid_and_ssid(capture, queries):
    calls, found = queries
    found[0] = True
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _query(app, pilot, result, "cracked")

        assert calls == [("https://dwpa.example", _BSSID, _SSID)]
        assert Config.dwpa == DwpaSettings(url="https://dwpa.example")
        assert "Possibly already cracked on DWPA" in str(result.render())


async def test_a_network_the_server_does_not_know_gets_no_warning(capture, queries):
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        result = app.screen.query_one("#dwpa-result", Label)
        await _query(app, pilot, result, "Not found")

        assert "Possibly already cracked" not in str(result.render())
        assert not app.screen.query_one("#dwpa-query", Button).disabled


async def test_the_query_needs_a_url(capture, queries):
    calls, _ = queries
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-query", Button).press()
        await pilot.pause(0)

        assert calls == []
        assert app.screen.query_one("#dwpa-query", Button).disabled is False


async def test_a_network_without_an_ssid_cannot_be_queried(capture, queries):
    calls, _ = queries
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(DwpaConfigModal(capture, _BSSID, None))
        await pilot.pause(0)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-query", Button).press()
        await pilot.pause(0)

        assert calls == []


async def test_a_failed_query_shows_the_servers_message(capture, monkeypatch):
    def fail(url, bssid, ssid):
        raise DwpaError("Cannot reach https://dwpa.example/bmacssid: refused")

    monkeypatch.setattr(mod, "network_leaked", fail)
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        result = app.screen.query_one("#dwpa-result", Label)
        await _query(app, pilot, result, "Cannot reach")

        assert "Possibly already cracked" not in str(result.render())
        assert not app.screen.query_one("#dwpa-query", Button).disabled


class _StoreKeys:
    def __init__(self) -> None:
        self.networks = []

    def __call__(self, networks):
        self.networks = list(networks)


@pytest.fixture
def retrievals(monkeypatch):
    calls, networks = [], []

    def fake_retrieve(url, key):
        calls.append((url, key))
        return networks

    monkeypatch.setattr(mod, "retrieve_cracked", fake_retrieve)
    return calls, networks


async def _retrieve(app, pilot, result, expected: str) -> None:
    app.screen.query_one("#dwpa-retrieve", Button).press()
    await wait_until(pilot, lambda: expected in str(result.render()))


async def test_retrieve_hands_the_cracked_keys_to_the_vault(capture, retrievals):
    calls, networks = retrievals
    networks.extend([PotfileEntry("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66", "TestAP-DWPA", "88888888"),
                     PotfileEntry("8e:6a:8d:a6:db:51", "00:11:22:33:44:55", "E36W126F-Guest1", "hunter22")])
    store = _StoreKeys()
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(DwpaConfigModal(capture, _BSSID, _SSID, store))
        await pilot.pause(0)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "2 cracked keys on DWPA")

        assert calls == [("https://dwpa.example", _KEY)]
        assert store.networks == networks
        assert Config.dwpa == DwpaSettings(url="https://dwpa.example", user_key=_KEY)


async def test_retrieve_with_nothing_cracked_says_so(capture, retrievals):
    calls, networks = retrievals
    store = _StoreKeys()
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(DwpaConfigModal(capture, _BSSID, _SSID, store))
        await pilot.pause(0)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "No cracked keys")

        assert len(calls) == 1 and networks == []
        assert store.networks == []


async def test_retrieve_needs_a_url_and_a_key(capture, retrievals):
    calls, _ = retrievals
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        app.screen.query_one("#dwpa-retrieve", Button).press()
        await pilot.pause(0)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = "not-a-key"
        app.screen.query_one("#dwpa-retrieve", Button).press()
        await pilot.pause(0)

        assert calls == []
        assert dismissed == []


async def test_a_failed_retrieve_shows_the_message(capture, monkeypatch):
    def fail(url, key):
        raise DwpaError("Cannot reach https://dwpa.example/?api&dl=1: refused")

    monkeypatch.setattr(mod, "retrieve_cracked", fail)
    app = _Host()
    async with app.run_test() as pilot:
        await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "Cannot reach")

        assert not app.screen.query_one("#dwpa-retrieve", Button).disabled


async def test_a_downloaded_key_for_the_selected_handshake_is_announced(capture, retrievals,
                                                                        monkeypatch):
    calls, networks = retrievals
    networks.extend([PotfileEntry(_BSSID, "11:22:33:44:55:66", "HomeNet", "88888888"),
                     PotfileEntry("8e:6a:8d:a6:db:51", "00:11:22:33:44:55", "Elsewhere", "hunter22")])
    app = _Host()
    async with app.run_test() as pilot:
        announced = []
        monkeypatch.setattr(app, "notify", lambda message, *a, **k: announced.append(str(message)))
        await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "cracked keys on DWPA")

        assert any("88888888" in message and "HomeNet" in message for message in announced)
        assert not any("hunter22" in message for message in announced)


async def test_keys_for_other_networks_are_not_announced(capture, retrievals, monkeypatch):
    calls, networks = retrievals
    networks.append(PotfileEntry("8e:6a:8d:a6:db:51", "00:11:22:33:44:55", "Elsewhere", "hunter22"))
    app = _Host()
    async with app.run_test() as pilot:
        announced = []
        monkeypatch.setattr(app, "notify", lambda message, *a, **k: announced.append(str(message)))
        await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "cracked key on DWPA")

        assert announced == []


class _NotifiedHost(_Host):
    def __init__(self) -> None:
        super().__init__()
        self.notified: list[str] = []

    def notify(self, message, **kwargs) -> None:
        self.notified.append(str(message))


async def test_the_announced_ssid_and_psk_are_not_read_as_markup(capture, retrievals):
    calls, networks = retrievals
    networks.append(PotfileEntry(_BSSID, "02:00:00:00:00:03", "[b]HomeNet", "pass[/]word"))
    app = _NotifiedHost()
    async with app.run_test() as pilot:
        await _open(app, pilot, capture)
        app.screen.query_one("#dwpa-url", Input).value = "https://dwpa.example"
        app.screen.query_one("#dwpa-key", Input).value = _KEY
        result = app.screen.query_one("#dwpa-result", Label)
        await _retrieve(app, pilot, result, "cracked key on DWPA")

        assert app.notified == [r"DWPA recovered the PSK for \[b]HomeNet: pass\[/]word"]
