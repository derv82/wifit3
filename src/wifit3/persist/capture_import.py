"""Import of capture files, directories and zips into the captures directory."""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from wifit3.persist.config import Config
from wifit3.persist.hc22000 import Hc22000Import

PCAP_EXTENSIONS = (".cap", ".pcap", ".pcapng")
IMPORT_EXTENSIONS = (".hc22000", *PCAP_EXTENSIONS, ".txt", ".meta")


@dataclass
class CaptureImport(Hc22000Import):
    """One import: the selected files and directories (recursive), zips read in memory; other file types
    and anything in Config.captures_dir are skipped."""
    files: int = 0

    @property
    def summary(self) -> str:
        """``Imported 41 handshakes & 12 PMKIDs (38 APs) from 120 files, 9 already in Vault``."""
        text = (f"Imported {self.handshakes} handshakes & {self.pmkids} PMKIDs ({len(self.bssids)} APs) "
                f"from {self.files} files")
        if self.duplicates:
            text += f", {self.duplicates} already in Vault"
        return text

    def add_paths(self, paths: list[Path]) -> None:
        captures_dir = Path(Config.captures_dir).resolve()
        for path in paths:
            if path.is_dir():
                file_paths = sorted(p for p in path.rglob("*") if p.is_file())
            else:
                file_paths = [path]
            for file_path in file_paths:
                if file_path.resolve().is_relative_to(captures_dir):
                    continue
                name = file_path.relative_to(path.parent).as_posix()
                if file_path.suffix.lower() == ".zip":
                    self._add_zip(name, file_path)
                    continue
                if file_path.suffix.lower() not in IMPORT_EXTENSIONS:
                    continue
                try:
                    data = file_path.read_bytes()
                    date = int(file_path.stat().st_mtime)
                except OSError:
                    self.problems.append((name, "unreadable"))
                    continue
                self._add_file(name, data, date)
        self.write_meta()

    def _add_zip(self, name: str, path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    entry_name = f"{name}/{info.filename}"
                    if info.filename.lower().endswith(".zip"):
                        self.problems.append((entry_name, "zip inside zip, skipped"))
                        continue
                    if PurePosixPath(info.filename).suffix.lower() not in IMPORT_EXTENSIONS:
                        continue
                    self._add_file(entry_name, archive.read(info), int(datetime(*info.date_time).timestamp()))
        except zipfile.BadZipFile:
            self.problems.append((name, "corrupt zip"))
        except OSError:
            self.problems.append((name, "unreadable"))

    def _add_file(self, name: str, data: bytes, date: int) -> None:
        self.files += 1
        extension = PurePosixPath(name).suffix.lower()
        if extension == ".hc22000":
            self.add_file(name, data.decode("utf-8", errors="replace"), date)
        else:
            self.problems.append((name, "not supported yet"))
