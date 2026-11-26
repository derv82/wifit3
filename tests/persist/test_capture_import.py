from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from wifit3.persist.capture_import import CaptureImport
from wifit3.persist.config import Config

PMKID = f"WPA*01*{'33' * 16}*aabbccddeeff*112233445566*5465737431***"
OTHER_PMKID = f"WPA*01*{'44' * 16}*aabbccddeeff*112233445566*5465737431***"


@pytest.fixture
def selected_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path / "captures"))
    (tmp_path / "old").mkdir()
    return tmp_path / "old"


def _imported(paths: list[Path]) -> CaptureImport:
    result = CaptureImport()
    result.add_paths(paths)
    return result


def test_a_directory_is_walked_recursively_and_other_file_types_are_skipped(selected_dir: Path):
    (selected_dir / "sub").mkdir()
    (selected_dir / "sub" / "a.HC22000").write_text(PMKID + "\n")
    (selected_dir / "b.pcap").write_bytes(b"")
    (selected_dir / "c.jpg").write_bytes(b"")
    result = _imported([selected_dir])
    assert (result.files, result.pmkids) == (2, 1)
    assert result.problems == [("old/b.pcap", "not supported yet")]


def test_a_zip_is_read_with_its_subdirectories_and_a_zip_inside_it_is_skipped(selected_dir: Path):
    with zipfile.ZipFile(selected_dir / "export.zip", "w") as archive:
        archive.writestr("a.hc22000", PMKID + "\n")
        archive.writestr("sub/b.hc22000", OTHER_PMKID + "\n")
        archive.writestr("inner.zip", b"")
        archive.writestr("readme.md", b"")
    result = _imported([selected_dir / "export.zip"])
    assert (result.files, result.pmkids) == (2, 2)
    assert result.problems == [("export.zip/inner.zip", "zip inside zip, skipped")]


def test_a_corrupt_zip_is_a_problem(selected_dir: Path):
    (selected_dir / "broken.zip").write_bytes(b"not a zip")
    result = _imported([selected_dir / "broken.zip"])
    assert result.problems == [("broken.zip", "corrupt zip")]


def test_files_in_the_captures_directory_are_skipped(selected_dir: Path):
    captures_dir = Path(Config.captures_dir)
    captures_dir.mkdir()
    (captures_dir / "Test1_aa-bb-cc-dd-ee-ff.hc22000").write_text(PMKID + "\n")
    result = _imported([selected_dir.parent])
    assert (result.files, result.pmkids, result.problems) == (0, 0, [])


def test_the_summary_counts_what_was_imported_and_what_the_vault_already_had(selected_dir: Path):
    (selected_dir / "a.hc22000").write_text(PMKID + "\n")
    (selected_dir / "b.hc22000").write_text(PMKID + "\n" + OTHER_PMKID + "\n")
    result = _imported([selected_dir])
    assert result.summary == "Imported 0 handshakes & 2 PMKIDs (1 APs) from 2 files, 1 already in Vault"
