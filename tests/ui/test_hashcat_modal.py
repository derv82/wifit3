from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, Input, Label

import wifit3.ui.vault.modals.hashcat as mod
from wifit3.persist.config import Config, ConfigError
from wifit3.ui.vault.modals.cpu_crack import CpuCrackConfigModal
from wifit3.ui.vault.modals.hashcat import HashcatConfigModal, _default_hashcat_path
from wifit3.ui.vault.modals.wordlists import _RemoveButton


@pytest.fixture(autouse=True)
def _unset_saved_paths(monkeypatch):
    """These live on Config as process-wide state; pin them so test order can't matter."""
    monkeypatch.setattr(Config, "hashcat_path", None)
    monkeypatch.setattr(Config, "wordlist_paths", [])
    monkeypatch.setattr(Config, "hashcat_args", None)


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""


def test_default_path_prefers_which(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/somewhere/hashcat")
    assert _default_hashcat_path() == "/somewhere/hashcat"


def test_default_path_falls_back_per_os(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert _default_hashcat_path() == "/usr/bin/hashcat"
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    assert _default_hashcat_path() == "/opt/homebrew/bin/hashcat"
    monkeypatch.setattr(mod.sys, "platform", "win32")
    assert _default_hashcat_path() == r"C:\hashcat\hashcat.exe"


def test_saved_path_outranks_a_hashcat_found_on_path(monkeypatch):
    """Someone who points wifit3 at a specific build must not be silently overridden by
    whatever happens to be on $PATH."""
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/somewhere/hashcat")
    monkeypatch.setattr(Config, "hashcat_path", r"D:\tools\hashcat\hashcat.exe")
    assert _default_hashcat_path() == r"D:\tools\hashcat\hashcat.exe"


def test_launching_remembers_the_launch_settings():
    """Straight through the real Config.save(): tests/ui/conftest.py points it at a tmp file."""
    HashcatConfigModal()._remember({"hashcat_exe": r"D:\hashcat.exe",
                                    "wordlists": [r"D:\rockyou.txt", r"D:\top29m.txt"],
                                    "hashcat_args": "-w 3"})
    Config.hashcat_path = Config.hashcat_args = None
    Config.wordlist_paths = []
    Config.load()

    assert (Config.hashcat_path, Config.wordlist_paths, Config.hashcat_args) == (
        r"D:\hashcat.exe", [r"D:\rockyou.txt", r"D:\top29m.txt"], "-w 3")


def test_an_unwritable_config_warns_but_keeps_the_paths(monkeypatch):
    """A failed save must not cost the user their launch."""
    def _boom(cls):
        raise ConfigError("disk full")
    monkeypatch.setattr(Config, "save", classmethod(_boom))

    modal = HashcatConfigModal()
    warnings = []
    monkeypatch.setattr(modal, "notify", lambda *a, **k: warnings.append((a, k)))
    modal._remember({"hashcat_exe": r"D:\hashcat.exe", "wordlists": [r"D:\rockyou.txt"],
                     "hashcat_args": ""})

    assert Config.hashcat_path == r"D:\hashcat.exe"
    assert warnings and warnings[0][1].get("title") == "Config Error"


async def test_the_self_test_checkbox_edits_the_args_text():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashcatConfigModal())
        await pilot.pause(0)
        args = app.screen.query_one("#hashcat-args", Input)
        args.value = "-w 3"
        checkbox = app.screen.query_one("#hashcat-self-test-disable", Checkbox)

        checkbox.value = True
        await pilot.pause(0)
        assert args.value == "-w 3 --self-test-disable"

        checkbox.value = False
        await pilot.pause(0)
        assert args.value == "-w 3"


async def test_saved_args_prefill_the_modal_and_come_back_on_launch():
    Config.hashcat_args = "-w 3 --self-test-disable"
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause(0)
        assert app.screen.query_one("#hashcat-self-test-disable", Checkbox).value
        app.screen.query_one("#hashcat-exe", Input).value = r"D:\hashcat.exe"
        await app.screen._wordlists_chosen([Path(r"D:\rockyou.txt")])
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

    assert dismissed == [{"hashcat_exe": r"D:\hashcat.exe", "wordlists": [r"D:\rockyou.txt"],
                          "hashcat_args": "-w 3 --self-test-disable"}]


async def test_escape_dismisses_without_saving():
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause(0)
        app.screen.query_one("#hashcat-args", Input).value = "-w 3"
        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, HashcatConfigModal)

    assert dismissed == [None]
    assert Config.hashcat_args is None


async def test_the_modal_reads_back_the_wordlists_it_was_given():
    Config.wordlist_paths = [r"D:\a.txt", r"D:\b.txt"]
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashcatConfigModal())
        await pilot.pause(0)
        shown = [str(label.content) for label in app.screen.query(".wordlist-name")]
        assert len(shown) == 2
        assert "1:" in shown[0] and "a.txt" in shown[0]
        assert "2:" in shown[1] and "b.txt" in shown[1]

        await app.screen._wordlists_chosen(None)        # a cancelled picker keeps the selection
        await pilot.pause(0)
        assert app.screen._wordlists == [r"D:\a.txt", r"D:\b.txt"]


async def test_launch_is_refused_without_a_wordlist():
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause(0)
        refusals = []
        app.screen.notify = lambda *a, **k: refusals.append(a)
        app.screen.query_one("#hashcat-exe", Input).value = r"D:\hashcat.exe"
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

        assert refusals and not dismissed
        assert isinstance(app.screen, HashcatConfigModal)


async def test_x_removes_one_wordlist_and_clear_removes_them_all(tmp_path):
    Config.wordlist_paths = [str(tmp_path / "a.txt"), str(tmp_path / "b.txt"), str(tmp_path / "c.txt")]
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause()
        modal = app.screen
        rows = modal.query_one("#wordlists")
        clear = modal.query_one("#wordlist-clear", Button)
        assert clear.display

        next(b for b in modal.query(_RemoveButton) if b.path.endswith("b.txt")).press()
        await pilot.pause()
        names = [str(label.content) for label in rows.query(".wordlist-name")]
        assert len(names) == 2
        assert names[0].startswith("[bold]1:[/bold] a.txt") and names[1].startswith("[bold]2:[/bold] c.txt")
        assert str(tmp_path) in names[0], "the parent directory tells same-named files apart"

        clear.press()
        await pilot.pause()
        assert modal._wordlists == []
        assert not clear.display
        assert "none chosen" in str(rows.query_one(Label).content)
        assert Config.wordlist_paths == [str(tmp_path / "a.txt"), str(tmp_path / "b.txt"),
                                         str(tmp_path / "c.txt")], "nothing is saved before Launch"


async def test_launch_saves_what_is_left_after_a_removal(tmp_path):
    Config.wordlist_paths = [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")]
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause()
        modal = app.screen
        modal.query_one("#hashcat-exe", Input).value = str(tmp_path / "hashcat")
        modal.query(_RemoveButton).first().press()
        await pilot.pause()
        modal.query_one("#wordlist-launch", Button).press()
        await pilot.pause()

    assert dismissed[0]["wordlists"] == [str(tmp_path / "b.txt")]
    assert Config.wordlist_paths == [str(tmp_path / "b.txt")]


async def test_clear_is_hidden_when_no_wordlist_is_chosen():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashcatConfigModal())
        await pilot.pause(0)
        assert not app.screen.query_one("#wordlist-clear", Button).display


async def test_launch_is_refused_without_the_executable():
    Config.wordlist_paths = [r"D:\rockyou.txt"]
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause(0)
        refusals = []
        app.screen.notify = lambda *a, **k: refusals.append(a)
        app.screen.query_one("#hashcat-exe", Input).value = ""
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

        assert refusals and not dismissed


async def test_cpu_crack_launches_with_only_the_wordlists_and_remembers_them():
    Config.wordlist_paths = [r"D:\a.txt"]
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(CpuCrackConfigModal(), dismissed.append)
        await pilot.pause()
        assert str(app.screen.query_one("#wordlist-title", Label).content) == "Launch CPU Crack"
        assert not app.screen.query("#hashcat-exe")
        await app.screen._wordlists_chosen([Path(r"D:\a.txt"), Path(r"D:\b.txt")])
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause()

    assert dismissed == [{"wordlists": [r"D:\a.txt", r"D:\b.txt"]}]
    assert Config.wordlist_paths == [r"D:\a.txt", r"D:\b.txt"]
    assert Config.hashcat_path is None


async def test_cpu_crack_refuses_to_launch_without_a_wordlist():
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(CpuCrackConfigModal(), dismissed.append)
        await pilot.pause(0)
        refusals = []
        app.screen.notify = lambda *a, **k: refusals.append(a)
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

        assert refusals and not dismissed


class _HostWithGlobalButtonWidth(App):
    CSS = "Button { min-width: 12; }"


async def test_remove_and_clear_shrink_below_the_global_button_width_in_both_modals():
    Config.wordlist_paths = [r"D:\a.txt"]
    for modal_type in (HashcatConfigModal, CpuCrackConfigModal):
        app = _HostWithGlobalButtonWidth()
        async with app.run_test() as pilot:
            app.push_screen(modal_type())
            await pilot.pause()
            assert app.screen.query_one(_RemoveButton).size.width == 3
            assert app.screen.query_one("#wordlist-clear", Button).size.width == 7
