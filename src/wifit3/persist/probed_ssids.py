"""Append-only wordlist of every SSID probed for, across sessions."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional, Set

from wifit3.persist.config import Config

FILE_NAME = "probed_ssids.txt"


class ProbedSsidFile:
    """Appends each newly probed SSID to ``{captures_dir}/probed_ssids.txt`` while the pref is on."""

    def __init__(self, on_error: Callable[[Path, OSError], None]) -> None:
        self._on_error = on_error
        self._loaded_path: Optional[Path] = None
        self._failed_path: Optional[Path] = None
        self._saved: Set[str] = set()
        self._last_count = 0

    def sync(self, probed_ssids: Set[str]) -> None:
        """Follow the pref and captures_dir; append every probed SSID not yet in the file."""
        if not Config.save_probed_ssids:
            self._loaded_path = None
            self._failed_path = None
            self._saved = set()
            self._last_count = 0
            return
        path = Path(Config.captures_dir) / FILE_NAME
        if path == self._failed_path:
            return
        if path != self._loaded_path:
            self._failed_path = None
            try:
                with path.open(encoding="utf-8", errors="ignore") as f:
                    self._saved = {line.rstrip("\n") for line in f}
            except FileNotFoundError:
                self._saved = set()
            except OSError as e:
                self._fail(path, e)
                return
            self._loaded_path = path
            self._last_count = 0
        if len(probed_ssids) == self._last_count:
            return
        self._last_count = len(probed_ssids)
        self._write(sorted(probed_ssids - self._saved))

    def _write(self, ssids: Iterable[str]) -> None:
        unsaved = [s for s in ssids if "\n" not in s and "\r" not in s]
        if not unsaved:
            return
        try:
            self._loaded_path.parent.mkdir(parents=True, exist_ok=True)
            with self._loaded_path.open("a", encoding="utf-8", newline="\n") as f:
                f.writelines(s + "\n" for s in unsaved)
        except OSError as e:
            self._fail(self._loaded_path, e)
            return
        self._saved.update(unsaved)

    def _fail(self, path: Path, error: OSError) -> None:
        self._loaded_path = None
        self._failed_path = path
        self._saved = set()
        self._last_count = 0
        self._on_error(path, error)
