"""The file/directory picker (ui/path_picker.py).

Path decisions are plain functions, so most of this file needs no running app. The async
tests are deliberate: one journey per mode, each walking the whole path in a single app,
because the behaviour under test *is* the interaction (a modal pushed onto a modal, a
threaded tree load, a reactive re-root). An app per assertion costs ~0.15s and buys nothing.
"""
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Tree

from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.ui.path_picker import (
    PathInput, PathPickerModal, _DirectoriesOnlyTree, _nearest_dir, picker_roots,
    relative_to_cwd,
)
from wifit3.ui.pref import PreferencesModal


@pytest.fixture
def tree_fixture(tmp_path):
    (tmp_path / "wordlists").mkdir()
    (tmp_path / "wordlists" / "rockyou.txt").write_text("password\n", encoding="utf-8")
    (tmp_path / "captures").mkdir()
    return tmp_path


# --- path logic (no app) -----------------------------------------------------

def test_relative_to_cwd_only_rewrites_paths_inside_the_tree(tmp_path):
    assert relative_to_cwd(Path.cwd() / "captures" / "loot.hc22000") == Path("captures/loot.hc22000")
    # a sibling tree must stay absolute rather than grow a chain of ".."
    assert relative_to_cwd(tmp_path / "elsewhere") == tmp_path / "elsewhere"


def test_nearest_dir_climbs_to_something_that_exists(tree_fixture):
    assert _nearest_dir(tree_fixture / "wordlists") == tree_fixture / "wordlists"
    assert _nearest_dir(tree_fixture / "wordlists" / "rockyou.txt") == tree_fixture / "wordlists"
    assert _nearest_dir(tree_fixture / "gone" / "deeper" / "still") == tree_fixture


def test_directory_mode_filters_files_out(tree_fixture):
    entries = [tree_fixture / "wordlists", tree_fixture / "wordlists" / "rockyou.txt"]
    kept = _DirectoriesOnlyTree.filter_paths(None, entries)     # never touches self
    assert [p.name for p in kept] == ["wordlists"]


def test_roots_are_real_directories_and_include_home():
    labelled = picker_roots()
    assert labelled, "the strip should never be empty"
    assert all(path.is_dir() for _, path in labelled)
    assert Path.home() in [path for _, path in labelled]


# --- the journeys ------------------------------------------------------------

class _Host(App):
    def __init__(self):
        super().__init__()
        self.vault = Vault()


class _HostWithInput(_Host):
    def __init__(self, value, directories_only=False):
        super().__init__()
        self._value = str(value)
        self._directories_only = directories_only

    def compose(self) -> ComposeResult:
        yield PathInput(self._value, directories_only=self._directories_only, id="target")


@pytest.mark.asyncio
async def test_deferred_sync_is_safe_after_picker_is_dismissed(tree_fixture):
    app = _HostWithInput(tree_fixture / "wordlists")
    async with app.run_test() as pilot:
        app.query_one("#browse", Button).press()
        await pilot.pause()
        picker = app.screen
        picker.call_after_refresh(picker._sync_root)
        picker.dismiss(None)
        await pilot.pause()
        assert app.screen is not picker


@pytest.mark.asyncio
async def test_directory_journey_from_preferences(tree_fixture, monkeypatch):
    """Preferences is itself a ModalScreen, so this also settles the stacking question: the
    picker pushes on top of it, owns escape, and hands a value back on the way out."""
    monkeypatch.setattr(Config, "captures_dir", str(tree_fixture / "captures"))
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause()
        prefs = app.screen

        prefs.query_one("#browse", Button).press()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PathPickerModal)
        assert picker.is_modal and prefs.is_modal
        assert app.screen_stack[-2] is prefs, "Preferences must stay on the stack beneath"
        assert picker.query_one("#picker-path", Input).value == str(tree_fixture / "captures")

        # Escape belongs to the picker alone: Textual truncates the binding chain at the last
        # modal, so Preferences' own escape binding must not also fire.
        picker.query_one("#picker-path", Input).value = str(tree_fixture / "wordlists")
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is prefs
        assert prefs.query_one("#captures_dir", Input).value == str(tree_fixture / "captures")

        prefs.query_one("#browse", Button).press()              # Cancel backs out the same way
        await pilot.pause()
        app.screen.query_one("#picker-path", Input).value = str(tree_fixture / "wordlists")
        app.screen.query_one("#picker-cancel", Button).press()
        await pilot.pause()
        assert app.screen is prefs
        assert prefs.query_one("#captures_dir", Input).value == str(tree_fixture / "captures")

        prefs.query_one("#browse", Button).press()
        await pilot.pause()
        picker = app.screen
        tree = picker.query_one("#picker-tree")
        box = picker.query_one("#picker-path", Input)
        parent = picker.query_one("#picker-parent", Button)

        parent.press()                                          # ".." climbs, the box follows
        await pilot.pause()
        assert Path(tree.path) == tree_fixture
        assert box.value == str(tree_fixture)

        anchor = Path(tree_fixture.anchor)                      # the strip jumps to a drive or "/"
        next(b for b in picker.query(".picker-root") if b.target == anchor).press()
        await pilot.pause()
        assert Path(tree.path) == anchor
        assert box.value == str(anchor)
        assert tree.root.label.plain == str(anchor), "an unnamed root must not render blank"
        assert parent.disabled, '".." is dead at the top'

        box.value = str(tree_fixture / "wordlists")
        picker.query_one("#picker-select", Button).press()
        await pilot.pause()
        assert app.screen is prefs, "picker pops, Preferences stays"
        assert prefs.query_one("#captures_dir", Input).value == str(tree_fixture / "wordlists")


@pytest.mark.asyncio
async def test_file_journey(tree_fixture):
    app = _HostWithInput(tree_fixture / "wordlists")
    async with app.run_test() as pilot:
        app.query_one("#browse", Button).press()
        await pilot.pause()
        picker = app.screen
        tree = picker.query_one("#picker-tree")
        box = picker.query_one("#picker-path", Input)
        toasts = []
        picker.notify = lambda *a, **k: toasts.append(a)

        await tree.reload()
        await pilot.pause()
        assert "rockyou.txt" in {n.data.path.name for n in tree.root.children}, "files are shown"

        wordlist = next(n for n in tree.root.children if n.data.path.name == "rockyou.txt")
        tree.post_message(Tree.NodeHighlighted(wordlist))       # the cursor writes into the box
        await pilot.pause()
        assert box.value == str(wordlist.data.path)

        # The tree load is a threaded worker; its root highlight must not wipe out typing.
        typed = str(tree_fixture / "wordlists" / "rockyou.txt")
        box.value = typed
        await tree.reload()
        await pilot.pause()
        assert box.value == typed

        box.value = str(tree_fixture)                           # a directory re-roots, never selects
        await box.action_submit()
        await pilot.pause()
        assert app.screen is picker
        assert Path(tree.path) == tree_fixture

        toasts.clear()                                          # Enter rejects what isn't there
        box.value = str(tree_fixture / "nope" / "typo.txt")
        await box.action_submit()
        await pilot.pause()
        assert app.screen is picker and toasts

        toasts.clear()                                          # ...and Select agrees with Enter
        picker.query_one("#picker-select", Button).press()
        await pilot.pause()
        assert app.screen is picker and toasts

        box.value = typed
        picker.query_one("#picker-select", Button).press()
        await pilot.pause()
        assert app.query_one("#target", Input).value == typed


@pytest.mark.asyncio
async def test_preferences_stores_a_relative_capture_dir(monkeypatch):
    """Picking the project's own captures/ must come back as 'captures', not an absolute path:
    the save-line and the vault both read this value straight out of Config."""
    (Path.cwd() / "captures").mkdir(exist_ok=True)
    monkeypatch.setattr(Config, "captures_dir", "captures")
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause()
        prefs = app.screen
        prefs.query_one("#browse", Button).press()
        await pilot.pause()
        app.screen.query_one("#picker-select", Button).press()
        await pilot.pause()

        assert prefs.query_one("#captures_dir", Input).value == "captures"
