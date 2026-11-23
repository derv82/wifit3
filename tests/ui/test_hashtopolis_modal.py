import pytest
from textual.app import App
from textual.widgets import Button, Input, Label, Select

import wifit3.ui.vault.modals.hashtopolis as mod
from wifit3.persist.config import Config
from wifit3.ui.vault.modals.hashtopolis import HashtopolisConfigModal
from wifit3.vault.tools.hashtopolis_api import AccessGroup, ServerFile
from tests.ui.wait_until import wait_until


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""


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
    result = app.screen.query_one("#hashtopolis-test-result", Label)
    before = str(result.render())
    app.screen.query_one("#hashtopolis-test", Button).press()
    await wait_until(pilot, lambda: str(result.render()) != before)     # the worker has started
    await app.workers.wait_for_complete()
    await pilot.pause(0)


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
