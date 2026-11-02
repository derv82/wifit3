"""The file/directory picker (ui/path_picker.py).

Path decisions are plain functions, so most of this file needs no running app. The async
tests are deliberate: one journey per mode, each walking the whole path in a single app,
because the behaviour under test *is* the interaction (a modal pushed onto a modal, a
threaded tree load, a reactive re-root). An app per assertion costs ~0.15s and buys nothing.
``pilot.pause(0)`` and ``wait_until`` stand in for ``pilot.pause()``, whose idle wait costs ~50 ms on Windows.
"""
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Tree

from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.ui.path_picker import (
    CHECK_GLYPH, PathInput, PathPickerModal, _DirectoriesOnlyTree, _nearest_dir, picker_roots,
    relative_to_cwd,
)
from wifit3.ui.pref import PreferencesModal
from tests.ui.wait_until import wait_until


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
async def test_directory_journey_from_preferences(tree_fixture, monkeypatch):
    """Preferences is itself a ModalScreen, so this also settles the stacking question: the
    picker pushes on top of it, owns escape, and hands a value back on the way out."""
    monkeypatch.setattr(Config, "captures_dir", str(tree_fixture / "captures"))
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        prefs = app.screen

        prefs.query_one("#browse", Button).press()
        await pilot.pause(0)
        picker = app.screen
        assert isinstance(picker, PathPickerModal)
        assert picker.is_modal and prefs.is_modal
        assert app.screen_stack[-2] is prefs, "Preferences must stay on the stack beneath"
        assert picker.query_one("#picker-path", Input).value == str(tree_fixture / "captures")

        # Escape belongs to the picker alone: Textual truncates the binding chain at the last
        # modal, so Preferences' own escape binding must not also fire.
        picker.query_one("#picker-path", Input).value = str(tree_fixture / "wordlists")
        await pilot.press("escape")
        await pilot.pause(0)
        assert app.screen is prefs
        assert prefs.query_one("#captures_dir", Input).value == str(tree_fixture / "captures")

        prefs.query_one("#browse", Button).press()              # Cancel backs out the same way
        await pilot.pause(0)
        app.screen.query_one("#picker-path", Input).value = str(tree_fixture / "wordlists")
        app.screen.query_one("#picker-cancel", Button).press()
        await pilot.pause(0)
        assert app.screen is prefs
        assert prefs.query_one("#captures_dir", Input).value == str(tree_fixture / "captures")

        prefs.query_one("#browse", Button).press()
        await pilot.pause(0)
        picker = app.screen
        tree = picker.query_one("#picker-tree")
        box = picker.query_one("#picker-path", Input)
        parent = picker.query_one("#picker-parent", Button)

        parent.press()                                          # ".." climbs, the box follows
        await pilot.pause(0)
        assert Path(tree.path) == tree_fixture
        assert box.value == str(tree_fixture)

        anchor = Path(tree_fixture.anchor)                      # the strip jumps to a drive or "/"
        next(b for b in picker.query(".picker-root") if b.target == anchor).press()
        await pilot.pause(0)
        assert Path(tree.path) == anchor
        assert box.value == str(anchor)
        assert tree.root.label.plain == str(anchor), "an unnamed root must not render blank"
        assert parent.disabled, '".." is dead at the top'

        box.value = str(tree_fixture / "wordlists")
        picker.query_one("#picker-select", Button).press()
        await pilot.pause(0)
        assert app.screen is prefs, "picker pops, Preferences stays"
        assert prefs.query_one("#captures_dir", Input).value == str(tree_fixture / "wordlists")


@pytest.mark.asyncio
async def test_file_journey(tree_fixture):
    app = _HostWithInput(tree_fixture / "wordlists")
    async with app.run_test() as pilot:
        app.query_one("#browse", Button).press()
        await pilot.pause(0)
        picker = app.screen
        tree = picker.query_one("#picker-tree")
        box = picker.query_one("#picker-path", Input)
        toasts = []
        picker.notify = lambda *a, **k: toasts.append(a)
        await wait_until(pilot, lambda: bool(tree.root.children))

        assert "rockyou.txt" in {n.data.path.name for n in tree.root.children}, "files are shown"

        wordlist = next(n for n in tree.root.children if n.data.path.name == "rockyou.txt")
        tree.post_message(Tree.NodeHighlighted(wordlist))       # the cursor writes into the box
        await pilot.pause(0)
        assert box.value == str(wordlist.data.path)

        # The tree load is a threaded worker; its root highlight must not wipe out typing.
        typed = str(tree_fixture / "wordlists" / "rockyou.txt")
        box.value = typed
        await tree.reload()
        await pilot.pause()     # idle: lets the reload's highlight land before checking it changed nothing
        assert box.value == typed

        box.value = str(tree_fixture)                           # a directory re-roots, never selects
        await box.action_submit()
        await pilot.pause(0)
        assert app.screen is picker
        assert Path(tree.path) == tree_fixture

        toasts.clear()                                          # Enter rejects what isn't there
        box.value = str(tree_fixture / "nope" / "typo.txt")
        await box.action_submit()
        await pilot.pause(0)
        assert app.screen is picker and toasts

        toasts.clear()                                          # ...and Select agrees with Enter
        picker.query_one("#picker-select", Button).press()
        await pilot.pause(0)
        assert app.screen is picker and toasts

        box.value = typed
        picker.query_one("#picker-select", Button).press()
        await pilot.pause(0)
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
        await pilot.pause(0)
        prefs = app.screen
        prefs.query_one("#browse", Button).press()
        await pilot.pause(0)
        app.screen.query_one("#picker-select", Button).press()
        await pilot.pause(0)

        assert prefs.query_one("#captures_dir", Input).value == "captures"


@pytest.mark.asyncio
async def test_multi_select_journey(tree_fixture):
    """Several files selected: the marks are visible, the count shows on the button, and
    Select hands back each one in the order it was chosen."""
    (tree_fixture / "wordlists" / "top29m.txt").write_text("hunter2\n", encoding="utf-8")
    app = _Host()
    async with app.run_test() as pilot:
        picked = []
        app.push_screen(PathPickerModal(tree_fixture / "wordlists", title="Select wordlists",
                                        multiple=True), picked.append)
        await pilot.pause(0)
        picker = app.screen
        tree = picker.query_one("#picker-tree")
        select = picker.query_one("#picker-select", Button)
        await wait_until(pilot, lambda: bool(tree.root.children))
        assert str(select.label) == "Select"
        await wait_until(pilot, lambda: tree.has_focus)     # spacebar selects, so the tree owns the keyboard
        assert "spacebar" in str(picker.query_one("#picker-hint").content), "the key is not guessable"

        by_name = {n.data.path.name: n for n in tree.root.children}
        tree.cursor_line = by_name["top29m.txt"].line
        painted = by_name["top29m.txt"].line
        assert CHECK_GLYPH not in tree.render_line(painted).text  # paints the row, and caches it
        await pilot.press("space")                          # the real keypress, not the action
        await pilot.pause(0)
        assert str(select.label) == "Select (1)"
        # Straight off the painted line: the tree caches rendered rows, so a mark that only
        # appears on the next mouse move is the failure mode here.
        assert CHECK_GLYPH in tree.render_line(painted).text

        tree.cursor_line = by_name["rockyou.txt"].line
        picker.action_toggle_check()
        await pilot.pause(0)
        assert str(select.label) == "Select (2)"
        assert CHECK_GLYPH in str(tree.render_label(by_name["rockyou.txt"], None, None))

        tree.cursor_line = by_name["top29m.txt"].line       # a second press deselects
        picker.action_toggle_check()
        await pilot.pause(0)
        assert str(select.label) == "Select (1)"

        tree.cursor_line = by_name["top29m.txt"].line
        picker.action_toggle_check()
        select.press()
        await pilot.pause(0)

    assert picked == [[tree_fixture / "wordlists" / "rockyou.txt",
                       tree_fixture / "wordlists" / "top29m.txt"]]


@pytest.mark.asyncio
async def test_multi_select_starts_from_what_is_already_chosen(tree_fixture):
    wordlist = tree_fixture / "wordlists" / "rockyou.txt"
    app = _Host()
    async with app.run_test() as pilot:
        picked = []
        app.push_screen(PathPickerModal(wordlist, multiple=True, checked=[wordlist]), picked.append)
        await pilot.pause(0)
        picker = app.screen
        assert str(picker.query_one("#picker-select", Button).label) == "Select (1)"

        picker.query_one("#picker-select", Button).press()
        await pilot.pause(0)

    assert picked == [[wordlist]]


@pytest.mark.asyncio
async def test_a_single_file_picker_still_hands_back_one_path(tree_fixture):
    """The single-select contract the rest of the app uses: one path, not a list."""
    wordlist = tree_fixture / "wordlists" / "rockyou.txt"
    app = _Host()
    async with app.run_test() as pilot:
        picked = []
        app.push_screen(PathPickerModal(wordlist), picked.append)
        await pilot.pause(0)
        app.screen.query_one("#picker-path", Input).value = str(wordlist)
        app.screen.query_one("#picker-select", Button).press()
        await pilot.pause(0)

    assert picked == [[wordlist]]


@pytest.mark.asyncio
async def test_clicking_a_file_selects_it(tree_fixture):
    """Mouse users never find the spacebar, so a click has to select too."""
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PathPickerModal(tree_fixture / "wordlists", multiple=True))
        await pilot.pause(0)
        picker = app.screen
        tree = picker.query_one("#picker-tree")
        select = picker.query_one("#picker-select", Button)
        await wait_until(pilot, lambda: bool(tree.root.children))
        wordlist = next(n for n in tree.root.children if n.data.path.name == "rockyou.txt")

        await pilot.click(tree, offset=(8, wordlist.line + 1))      # +1 for the tree's border
        await pilot.pause(0)
        assert str(select.label) == "Select (1)"

        await pilot.click(tree, offset=(8, wordlist.line + 1))
        await pilot.pause(0)
        assert str(select.label) == "Select", "clicking again deselects"


@pytest.mark.asyncio
async def test_enter_selects_a_file(tree_fixture):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PathPickerModal(tree_fixture / "wordlists", multiple=True))
        await pilot.pause(0)
        picker = app.screen
        tree = picker.query_one("#picker-tree")
        await wait_until(pilot, lambda: bool(tree.root.children))
        tree.cursor_line = next(n for n in tree.root.children
                                if n.data.path.name == "rockyou.txt").line

        await pilot.press("enter")
        await pilot.pause(0)

        assert str(picker.query_one("#picker-select", Button).label) == "Select (1)"


@pytest.mark.asyncio
async def test_spacebar_on_a_directory_still_expands_it(tree_fixture):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PathPickerModal(tree_fixture, multiple=True))
        await pilot.pause(0)
        tree = app.screen.query_one("#picker-tree")
        await wait_until(pilot, lambda: bool(tree.root.children))
        folder = next(n for n in tree.root.children if n.data.path.name == "wordlists")
        tree.cursor_line = folder.line
        assert not folder.is_expanded

        await pilot.press("space")
        await pilot.pause(0)

        assert folder.is_expanded
        assert str(app.screen.query_one("#picker-select", Button).label) == "Select"


async def test_a_typed_glob_adds_its_matching_files(tree_fixture):
    """Enter on a pattern checks every file it matches, keeps what was already checked, and
    skips a directory whose name matches."""
    wordlists = tree_fixture / "wordlists"
    (wordlists / "top29m.txt").write_text("hunter2\n", encoding="utf-8")
    (wordlists / "words.lst").write_text("letmein\n", encoding="utf-8")
    (wordlists / "nested.txt").mkdir()
    rockyou = wordlists / "rockyou.txt"
    app = _Host()
    async with app.run_test() as pilot:
        picked = []
        app.push_screen(PathPickerModal(wordlists, multiple=True, checked=[rockyou]), picked.append)
        await pilot.pause(0)
        picker = app.screen
        box = picker.query_one("#picker-path", Input)
        select = picker.query_one("#picker-select", Button)
        toasts = []
        picker.notify = lambda *a, **k: toasts.append(a)

        box.value = str(wordlists / "*.txt")
        await box.action_submit()
        await pilot.pause(0)
        assert app.screen is picker, "a glob adds, it doesn't dismiss"
        assert str(select.label) == "Select (2)"
        assert toasts == [("Added 1 files",)]

        toasts.clear()
        box.value = str(wordlists / "*.zip")
        await box.action_submit()
        await pilot.pause(0)
        assert toasts == [(f"No files match: {wordlists / '*.zip'}",)]
        assert str(select.label) == "Select (2)"

        select.press()
        await pilot.pause(0)

    assert picked == [[rockyou, wordlists / "top29m.txt"]]
