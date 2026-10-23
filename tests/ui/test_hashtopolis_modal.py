import threading

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Label, Select

import wifit3.ui.vault.modals.hashtopolis as mod
from wifit3.persist.config import Config
from wifit3.ui.vault.modals.hashtopolis import HashtopolisConfigModal
from wifit3.vault.tools.hashtopolis_api import AccessGroup, HashtopolisError, ServerFile


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""


@pytest.mark.asyncio
async def test_modal_fits_viewport_and_scrolls_fields_inside():
    app = _Host()
    async with app.run_test(size=(40, 12)) as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause()

        dialog = app.screen.query_one("#hashtopolis-dialog")
        body = app.screen.query_one("#hashtopolis-body", VerticalScroll)
        title = app.screen.query_one("#hashtopolis-title", Label)
        buttons = app.screen.query_one("#hashtopolis-buttons")
        assert dialog.region.x >= 0 and dialog.region.right <= app.size.width
        assert dialog.region.y >= 0 and dialog.region.bottom <= app.size.height
        assert body.virtual_size.height > body.size.height
        assert body.parent is dialog
        assert title.parent is body and buttons.parent is body
        body.scroll_end(animate=False)
        await pilot.pause()
        assert buttons.region.bottom <= body.content_region.bottom


class FakeApi:
    wordlist_files = [ServerFile(1, "rockyou.txt"), ServerFile(2, "top1k.txt")]
    access_error: Exception | None = None

    def __init__(self, url: str, access_key: str) -> None:
        self.url, self.access_key = url, access_key

    def check_access(self) -> None:
        if self.access_error:
            raise self.access_error

    def wordlists(self) -> list[ServerFile]:
        return self.wordlist_files

    def access_groups(self) -> list[AccessGroup]:
        return [AccessGroup(1, "Default Group")]


@pytest.fixture
def server(monkeypatch):
    FakeApi.access_error = None
    FakeApi.wordlist_files = [ServerFile(1, "rockyou.txt"), ServerFile(2, "top1k.txt")]
    monkeypatch.setattr(mod, "HashtopolisApi", FakeApi)
    return FakeApi


async def _test_connection(app, pilot) -> None:
    app.screen.query_one("#hashtopolis-url", Input).value = "http://server:8080"
    app.screen.query_one("#hashtopolis-access-key", Input).value = "key"
    app.screen.query_one("#hashtopolis-test", Button).press()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause(0)


async def test_test_fills_wordlists_and_picks_the_only_access_group(server):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause(0)
        await _test_connection(app, pilot)

        wordlists = app.screen.query_one("#hashtopolis-wordlist", Select)
        assert [option[1] for option in wordlists._options if option[1] != Select.NULL] == [1, 2]
        assert app.screen.query_one("#hashtopolis-access-group", Select).value == 1


async def test_test_shows_the_server_message(server):
    server.access_error = HashtopolisError("API key was not found!")
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause(0)
        await _test_connection(app, pilot)

        result = app.screen.query_one("#hashtopolis-test-result", Label)
        assert "API key was not found!" in str(result.render())


async def test_server_without_wordlists_says_so(server):
    server.wordlist_files = []
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause(0)
        await _test_connection(app, pilot)

        assert "no wordlists found" in str(
            app.screen.query_one("#hashtopolis-test-result", Label).render())


async def test_confirm_saves_settings_and_keeps_the_key_out_of_the_job(server):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashtopolisConfigModal(confirm_label="Save"), dismissed.append)
        await pilot.pause(0)
        await _test_connection(app, pilot)
        app.screen.query_one("#hashtopolis-wordlist", Select).value = 2
        confirm = app.screen.query_one("#hashtopolis-confirm", Button)
        assert str(confirm.label) == "Save"
        confirm.press()
        await pilot.pause(0)

    assert dismissed == [{"wordlist_id": 2, "wordlist_name": "top1k.txt", "access_group_id": 1}]
    assert (Config.hashtopolis.url, Config.hashtopolis.access_key,
            Config.hashtopolis.wordlist_name) == ("http://server:8080", "key", "top1k.txt")


async def test_confirm_without_a_wordlist_stays_open(server):
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashtopolisConfigModal(), dismissed.append)
        await pilot.pause(0)
        app.screen.query_one("#hashtopolis-url", Input).value = "http://server:8080"
        app.screen.query_one("#hashtopolis-access-key", Input).value = "key"
        app.screen.query_one("#hashtopolis-confirm", Button).press()
        await pilot.pause(0)

        assert dismissed == []
        assert isinstance(app.screen, HashtopolisConfigModal)


@pytest.mark.asyncio
async def test_test_finishing_after_cancel_leaves_the_app_running(server, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(FakeApi, "check_access", lambda self: release.wait(5))
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause()
        app.screen.query_one("#hashtopolis-test", Button).press()
        await pilot.pause()
        app.screen.query_one("#hashtopolis-cancel", Button).press()
        await pilot.pause()
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert not isinstance(app.screen, HashtopolisConfigModal)


async def test_saved_wordlist_and_group_show_by_name_before_a_refresh(server):
    Config.hashtopolis.wordlist_id, Config.hashtopolis.wordlist_name = 2, "top1k.txt"
    Config.hashtopolis.access_group_id = 1
    Config.hashtopolis.access_group_name = "Default Group"
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause()

        assert app.screen.query_one("#hashtopolis-wordlist", Select).value == 2
        group = app.screen.query_one("#hashtopolis-access-group", Select)
        assert group.value == 1 and ("Default Group", 1) in group._options
        assert str(app.screen.query_one("#hashtopolis-test-result", Label).render()) == ""


async def test_status_asks_for_a_refresh_until_wordlists_are_known(server):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause()

        assert "Refresh to list wordlists" in str(
            app.screen.query_one("#hashtopolis-test-result", Label).render())


async def test_status_counts_wordlists_and_groups(server):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause(0)
        await _test_connection(app, pilot)

        assert "Connected · 2 wordlists · 1 group" in str(
            app.screen.query_one("#hashtopolis-test-result", Label).render())


async def test_reveal_toggles_the_access_key(server):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal())
        await pilot.pause()
        access_key = app.screen.query_one("#hashtopolis-access-key", Input)
        reveal = app.screen.query_one("#hashtopolis-reveal", Button)

        reveal.press()
        await pilot.pause()
        assert not access_key.password and str(reveal.label) == "hide"
        reveal.press()
        await pilot.pause()
        assert access_key.password and str(reveal.label) == "reveal"


async def test_confirm_saves_the_access_group_name(server):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashtopolisConfigModal(confirm_label="Save"))
        await pilot.pause(0)
        await _test_connection(app, pilot)
        app.screen.query_one("#hashtopolis-wordlist", Select).value = 1
        app.screen.query_one("#hashtopolis-confirm", Button).press()
        await pilot.pause(0)

    assert Config.hashtopolis.access_group_name == "Default Group"
