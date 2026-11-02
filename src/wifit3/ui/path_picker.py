"""Modal file/directory picker, and the Input+button pair that opens it."""
from __future__ import annotations

import ctypes
import getpass
import glob
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
from rich.text import Text

BROWSE_GLYPH = "\U0001F4C1"  # folder emoji/icon
CHECK_GLYPH = "✅"


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
    # Bind spacebar to select files alongside directory expand/collapse.
    BINDINGS = [Binding("space", "screen.toggle_check", "Add/remove", show=False)]
    COMPONENT_CLASSES = {"picker-tree--checked"}
    DEFAULT_CSS = """
    _PickerTree > .picker-tree--checked { color: $text; background: $success-muted; }
    """

    def __init__(self, path: Path, *, checked: list[Path], id: Optional[str] = None) -> None:
        super().__init__(path, id=id)
        self.checked = checked

    def reset_node(self, node, label, data=None):
        """Label filesystem roots with the path: a Windows drive root and POSIX ``/`` both have
        an empty ``name``, which the stock tree would render as a blank root node."""
        if data is not None and not str(label):
            label = str(data.path)
        return super().reset_node(node, label, data)

    def refresh_path(self, path: Path) -> None:
        """Repaint the row for ``path``: the tree renders each row once and caches the result."""
        pending = list(self.root.children)
        while pending:
            node = pending.pop()
            if node.data is not None and node.data.path == path:
                node.refresh()
                return
            pending.extend(node.children)

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        entry = node.data
        if entry is not None and entry.path in self.checked:
            label = Text(CHECK_GLYPH + " ") + label[len(self.ICON_FILE):]
            label.stylize_before(
                self.get_component_rich_style("picker-tree--checked", partial=True))
        label.stylize(style, 0, len(self.ICON_FILE))    # highlight the icon with its row
        return label


class _DirectoriesOnlyTree(_PickerTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if _is_dir(path)]


class PathPickerModal(ModalScreen[Optional[list[Path]]]):
    """Pick files or a directory. Dismisses with the chosen paths, or None if cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PathPickerModal { align: center middle; }
    PathPickerModal #dialog {
        width: 72; max-width: 100%; height: 90%; max-height: 100%;
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
    PathPickerModal #picker-hint { height: 1; color: $text-muted; }
    PathPickerModal #buttons { align: right middle; height: auto; margin-top: 1; }
    PathPickerModal #buttons Button { margin-left: 1; }
    """

    def __init__(self, start: str | Path, *, directories_only: bool = False,
                 title: str = "Select", prefer_relative: bool = False,
                 multiple: bool = False, checked: Iterable[Path] = ()) -> None:
        super().__init__()
        self._start = Path(start).expanduser()
        self._directories_only = directories_only
        self._title = title
        self._prefer_relative = prefer_relative
        self._multiple = multiple
        self._checked: list[Path] = [Path(path) for path in checked]

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
            yield tree(root, checked=self._checked, id="picker-tree")
            if self._multiple:
                yield Label("Select multiple files with spacebar or click", id="picker-hint")
            with Horizontal(id="buttons"):
                yield Button("Cancel", "default", id="picker-cancel")
                yield Button(self._select_label(), "primary", id="picker-select")

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
        self._sync_root()
        if self._multiple:
            self.query_one("#picker-tree", _PickerTree).focus()

    @on(Button.Pressed, ".picker-root")
    def _jump_to_root(self, event: Button.Pressed) -> None:
        self._reroot(event.button.target)

    @on(Button.Pressed, "#picker-parent")
    def _go_up(self, event: Button.Pressed) -> None:
        self._reroot(Path(self.query_one("#picker-tree", DirectoryTree).path).parent)

    def _sync_root(self) -> None:
        """Point the box and the root node at the tree's root, and disable ".." once at the top."""
        root = Path(self.query_one("#picker-tree", DirectoryTree).path)
        self.query_one("#picker-path", Input).value = str(root)
        self.query_one("#picker-parent", Button).disabled = root.parent == root

    @on(Button.Pressed, "#picker-select")
    def _select(self, event) -> None:
        if self._checked:
            self._dismiss_paths(list(self._checked))
            return
        self._accept(Path(self.query_one("#picker-path", Input).value).expanduser())

    @on(DirectoryTree.FileSelected)
    def _check_node(self, event: DirectoryTree.FileSelected) -> None:
        """Clicking a file, or pressing enter on it, selects it as well."""
        if self._multiple:
            self._toggle_check(event.path)

    def action_toggle_check(self) -> None:
        node = self.query_one("#picker-tree", _PickerTree).cursor_node
        if node is None:
            return
        entry = node.data
        if self._multiple and entry is not None and not _is_dir(entry.path):
            self._toggle_check(entry.path)
        else:
            node.toggle()

    def _toggle_check(self, path: Path) -> None:
        if path in self._checked:
            self._checked.remove(path)
        else:
            self._checked.append(path)
        self.query_one("#picker-tree", _PickerTree).refresh_path(path)
        self.query_one("#picker-select", Button).label = self._select_label()

    def _select_label(self) -> str:
        return f"Select ({len(self._checked)})" if self._checked else "Select"

    def _accept(self, path: Path) -> None:
        """Dismiss with ``path``, or re-root the tree when it's a directory we can't return."""
        if self._directories_only:
            if _is_dir(path):
                self._dismiss_paths([path])
            else:
                self.notify(f"Not a directory: {path}", severity="error")
            return
        if _is_dir(path):
            self._reroot(path)
        elif not path.is_file() and self._multiple:
            self._check_glob_matches(path)
        elif not path.is_file():
            self.notify(f"No such file: {path}", severity="error")
        elif self._multiple:
            self._toggle_check(path)        # a typed-in file joins the selection
        else:
            self._dismiss_paths([path])

    def _check_glob_matches(self, pattern: Path) -> None:
        matches = [Path(match) for match in sorted(glob.glob(str(pattern))) if Path(match).is_file()]
        if not matches:
            self.notify(f"No files match: {pattern}", severity="error")
            return
        added = [match for match in matches if match not in self._checked]
        for match in added:
            self._toggle_check(match)
        self.notify(f"Added {len(added)} files")

    def _dismiss_paths(self, paths: list[Path]) -> None:
        self.dismiss([relative_to_cwd(path) for path in paths] if self._prefer_relative else paths)

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
        yield Button(BROWSE_GLYPH, "primary", id="browse")

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

    def _picked(self, paths: Optional[list[Path]]) -> None:
        if paths:
            self.query_one(Input).value = str(paths[0])
