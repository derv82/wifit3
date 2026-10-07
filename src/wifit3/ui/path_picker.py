"""Modal file/directory picker, and the Input+button pair that opens it."""
from __future__ import annotations

import ctypes
import getpass
import string
import sys
from pathlib import Path
from typing import Iterable, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label, Tree

BROWSE_GLYPH = "\U0001F4C1"  # folder emoji/icon


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _nearest_dir(path: Path) -> Path:
    """The deepest existing directory at or above ``path``; full path otherwise."""
    for candidate in (path, *path.parents):
        if _is_dir(candidate):
            return candidate
    return Path(path.anchor or Path.cwd())


# Removable/extra filesystems on Linux/MacOS
_MOUNT_PARENTS = ("/mnt", "/media", "/run/media", "/Volumes")


def _children(directory: Path) -> list[Path]:
    try:
        return sorted((child for child in directory.iterdir() if _is_dir(child)),
                      key=lambda child: child.name.lower())
    except OSError:
        return []


def _windows_drives() -> list[Path]:
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return [Path(Path.cwd().anchor)]
    return [Path(letter + ":/") for index, letter in enumerate(string.ascii_uppercase)
            if mask >> index & 1]


def _mount_points() -> list[Path]:
    mounts = []
    for parent in _MOUNT_PARENTS:
        for child in _children(Path(parent)):
            mounts.extend(_children(child) if child.name == _current_user() else [child])
    return mounts


def _current_user() -> str:
    try:
        return getpass.getuser()
    except OSError:
        return ""


def relative_to_cwd(path: Path) -> Path:
    """``path`` rewritten relative to the working directory when it sits inside it, else
    unchanged. Never climbs out with ``..``: a sibling tree stays absolute."""
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def picker_roots() -> list[tuple[str, Path]]:
    """Jump targets for the picker strip: home, then drives on Windows / root + mounts elsewhere."""
    roots = [("~", Path.home())]
    if sys.platform == "win32":
        roots += [(str(drive)[:2], drive) for drive in _windows_drives()]
    else:
        roots.append(("/", Path("/")))
        roots += [(mount.name, mount) for mount in _mount_points()]
    return [(label, path) for label, path in roots if _is_dir(path)]


class _RootButton(Button):
    """A jump button in the strip; ``target`` is the directory it re-roots the tree to."""

    def __init__(self, label: str, target: Path) -> None:
        super().__init__(label, classes="picker-root")
        self.target = target


class _PickerTree(DirectoryTree):
    def reset_node(self, node, label, data=None):
        """Label filesystem roots with the path: a Windows drive root and POSIX ``/`` both have
        an empty ``name``, which the stock tree would render as a blank root node."""
        if data is not None and not str(label):
            label = str(data.path)
        return super().reset_node(node, label, data)


class _DirectoriesOnlyTree(_PickerTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if _is_dir(path)]


class PathPickerModal(ModalScreen[Optional[Path]]):
    """Pick a file or a directory. Dismisses with the chosen path, or None if cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PathPickerModal { align: center middle; }
    PathPickerModal #dialog {
        width: 72; max-width: 100%; height: 28; max-height: 100%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    PathPickerModal #title { text-style: bold; margin-bottom: 1; }
    PathPickerModal #picker-roots { height: 1; margin-bottom: 1; }
    PathPickerModal #picker-roots Button {
        height: 1; min-width: 0; width: auto; border: none; margin-right: 1;
        padding: 0 1; background: $panel; color: $text;
    }
    PathPickerModal #picker-roots Button:hover { background: $primary; }
    PathPickerModal #picker-roots Button.-disabled { color: $text-disabled; }
    PathPickerModal DirectoryTree { height: 1fr; margin-top: 1; border: round $primary; }
    PathPickerModal #buttons { align: right middle; height: auto; margin-top: 1; }
    PathPickerModal #buttons Button { margin-left: 1; }
    """

    def __init__(self, start: str | Path, *, directories_only: bool = False,
                 title: str = "Select", prefer_relative: bool = False) -> None:
        super().__init__()
        self._start = Path(start).expanduser()
        self._directories_only = directories_only
        self._title = title
        self._prefer_relative = prefer_relative

    def compose(self) -> ComposeResult:
        root = self._start if _is_dir(self._start) else _nearest_dir(self._start)
        tree = _DirectoriesOnlyTree if self._directories_only else _PickerTree
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            with Horizontal(id="picker-roots"):
                yield Button("..", id="picker-parent")
                for label, target in picker_roots():
                    yield _RootButton(label, target)
            yield Input(str(self._start), id="picker-path")
            yield tree(root, id="picker-tree")
            with Horizontal(id="buttons"):
                yield Button("Cancel", "default", id="picker-cancel")
                yield Button("Select", "primary", id="picker-select")

    @on(Tree.NodeHighlighted)
    def _track_highlight(self, event: Tree.NodeHighlighted) -> None:
        """Moving the cursor onto an entry writes that path into the box."""
        entry = event.node.data
        if entry is not None and event.node is not event.node.tree.root:
            self.query_one("#picker-path", Input).value = str(entry.path)

    @on(Input.Submitted, "#picker-path")
    def _submit_path(self, event: Input.Submitted) -> None:
        self._accept(Path(event.value).expanduser())

    def on_mount(self) -> None:
        self.call_after_refresh(self._sync_root)

    @on(Button.Pressed, ".picker-root")
    def _jump_to_root(self, event: Button.Pressed) -> None:
        self._reroot(event.button.target)

    @on(Button.Pressed, "#picker-parent")
    def _go_up(self, event: Button.Pressed) -> None:
        self._reroot(Path(self.query_one("#picker-tree", DirectoryTree).path).parent)

    def _sync_root(self) -> None:
        """Point the box and the root node at the tree's root, and disable ".." once at the top."""
        if not self.is_active:
            return
        root = Path(self.query_one("#picker-tree", DirectoryTree).path)
        self.query_one("#picker-path", Input).value = str(root)
        self.query_one("#picker-parent", Button).disabled = root.parent == root

    @on(Button.Pressed, "#picker-select")
    def _select(self, event) -> None:
        self._accept(Path(self.query_one("#picker-path", Input).value).expanduser())

    def _accept(self, path: Path) -> None:
        """Dismiss with ``path``, or re-root the tree when it's a directory we can't return."""
        if self._directories_only:
            if _is_dir(path):
                self._dismiss_path(path)
            else:
                self.notify(f"Not a directory: {path}", severity="error")
            return
        if _is_dir(path):
            self._reroot(path)
        elif path.is_file():
            self._dismiss_path(path)
        else:
            self.notify(f"No such file: {path}", severity="error")

    def _dismiss_path(self, path: Path) -> None:
        self.dismiss(relative_to_cwd(path) if self._prefer_relative else path)

    def _reroot(self, directory: Path) -> None:
        tree = self.query_one("#picker-tree", DirectoryTree)
        if Path(tree.path) != directory:
            tree.path = directory
        self._sync_root()

    @on(Button.Pressed, "#picker-cancel")
    def _cancel(self, event) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class PathInput(Horizontal):
    """A path ``Input`` plus a folder button that opens ``PathPickerModal`` onto it."""

    DEFAULT_CSS = """
    PathInput { height: auto; }
    PathInput Input { width: 1fr; }
    PathInput Button { min-width: 6; width: 6; margin: 0; }
    """

    def __init__(self, value: str = "", *, directories_only: bool = False,
                 title: str = "Select", placeholder: str = "", prefer_relative: bool = False,
                 id: str | None = None) -> None:
        super().__init__()
        self._value = value
        self._directories_only = directories_only
        self._title = title
        self._placeholder = placeholder
        self._prefer_relative = prefer_relative
        self._input_id = id

    def compose(self) -> ComposeResult:
        yield Input(self._value, placeholder=self._placeholder, id=self._input_id)
        yield Button(BROWSE_GLYPH, "default", id="browse")

    @property
    def value(self) -> str:
        return self.query_one(Input).value

    @on(Button.Pressed, "#browse")
    def _browse(self, event: Button.Pressed) -> None:
        event.stop()
        start = self.value or str(Path.cwd())
        self.app.push_screen(
            PathPickerModal(start, directories_only=self._directories_only, title=self._title,
                            prefer_relative=self._prefer_relative),
            self._picked,
        )

    def _picked(self, path: Optional[Path]) -> None:
        if path is not None:
            self.query_one(Input).value = str(path)
