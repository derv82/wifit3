"""VaultView: one screen over everything captured/cracked under captures/, instead of
squinting at long BSSID-encoded filenames -- handshakes, PMKIDs, WEP keys, WPS PSKs, newest
first. Per-entry: remove the underlying file, or copy its payload (the WEP/WPS credential
itself, or the hashcat hashline for HS/PMKID -- there's no single "value" for those). Bulk:
export everything as one zip, or open the captures/ folder in the OS file manager.

Read-only otherwise: this screen never touches a radio, so it has no interface/array dependency
and works even with no card plugged in.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from wifit3.models import PersistedCapture
from wifit3.persist.capture_history import load_capture_index

logger = logging.getLogger(__name__)

_TYPE_LABELS = {"HS": "Handshake", "PMKID": "PMKID", "WEP": "WEP key", "WPS": "WPS PSK"}


def _copy_payload(capture: PersistedCapture) -> str:
    """The WEP/WPS credential itself, or (HS/PMKID, which have no single "value") the file's
    own hashcat hashline content."""
    if capture.value is not None:
        return capture.value
    try:
        return Path(capture.path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def open_in_file_manager(path: Path) -> None:
    if sys.platform.startswith("win"):
        import os
        os.startfile(path)                                        # noqa: S606 (Windows only)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def export_zip(captures_dir: Path) -> Optional[Path]:
    """Zip every file under ``captures_dir`` into a sibling archive (never inside
    ``captures_dir`` itself, so a re-export never zips a previous export into itself).
    None if there's nothing to export."""
    files = [p for p in captures_dir.iterdir() if p.is_file()] if captures_dir.is_dir() else []
    if not files:
        return None
    out = captures_dir.parent / f"wifit3_captures_{int(time.time())}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return out


class VaultView(Screen):
    """The loot manager: every captures/ artifact in one table."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("r", "remove_selected", "Remove", show=True),
        Binding("c", "copy_selected", "Copy", show=True),
        Binding("z", "export_zip", "Export Zip", show=True),
        Binding("o", "show_directory", "Show Directory", show=True),
    ]

    CSS = """
    VaultView DataTable { width: 100%; height: 1fr; border: round ansi_cyan;
                          border-title-color: ansi_cyan; border-title-style: bold; }
    """

    def __init__(self, captures_dir: Path = Path("captures")) -> None:
        super().__init__()
        self.captures_dir = Path(captures_dir)
        self._by_path: Dict[str, PersistedCapture] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="vault-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#vault-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("SSID", "BSSID", "Type", "Value", "Saved")
        self.reload()

    def on_screen_resume(self) -> None:
        """Recheck captures/ from disk every time this screen becomes visible again: a
        campaign may have saved something new since the last visit."""
        self.reload()

    # ----- data --------------------------------------------------------------

    def reload(self) -> None:
        """Reread captures/ from disk and repopulate the table, newest first."""
        table = self.query_one("#vault-table", DataTable)
        table.clear()
        self._by_path.clear()
        index = load_capture_index(self.captures_dir)
        rows: List[tuple] = [(bssid, cap) for bssid, caps in index.items() for cap in caps]
        rows.sort(key=lambda r: r[1].timestamp, reverse=True)
        for bssid, cap in rows:
            self._by_path[cap.path] = cap
            when = datetime.fromtimestamp(cap.timestamp).strftime("%Y-%m-%d %H:%M")
            table.add_row(cap.ssid or "?", bssid, _TYPE_LABELS.get(cap.type, cap.type),
                         cap.value or "", when, key=cap.path)
        self._update_title()

    def _update_title(self) -> None:
        self.query_one("#vault-table", DataTable).border_title = f"VAULT ({len(self._by_path)})"

    def _selected_capture(self) -> Optional[PersistedCapture]:
        table = self.query_one("#vault-table", DataTable)
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self._by_path.get(key)

    # ----- actions -------------------------------------------------------------

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_remove_selected(self) -> None:
        cap = self._selected_capture()
        if cap is None:
            return
        try:
            Path(cap.path).unlink()
        except OSError as exc:
            self.notify(f"Could not remove {Path(cap.path).name}: {exc}", severity="error")
            return
        self.notify(f"Removed {Path(cap.path).name}")
        self.reload()

    def action_copy_selected(self) -> None:
        cap = self._selected_capture()
        if cap is None:
            return
        payload = _copy_payload(cap)
        if not payload:
            self.notify("Nothing to copy for this entry", severity="warning")
            return
        self.app.copy_to_clipboard(payload)
        self.notify("Copied to clipboard")

    def action_export_zip(self) -> None:
        try:
            out = export_zip(self.captures_dir)
        except OSError as exc:
            self.notify(f"Export failed: {exc}", severity="error")
            return
        if out is None:
            self.notify("Nothing to export", severity="warning")
            return
        self.notify(f"Exported to {out}")

    def action_show_directory(self) -> None:
        try:
            open_in_file_manager(self.captures_dir)
        except OSError as exc:
            self.notify(f"Could not open {self.captures_dir}: {exc}", severity="error")
