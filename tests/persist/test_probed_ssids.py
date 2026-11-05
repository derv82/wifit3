"""Tests for the probed-SSID wordlist file."""
from __future__ import annotations

from pathlib import Path

import pytest

from wifit3.persist.config import Config
from wifit3.persist.probed_ssids import FILE_NAME, ProbedSsidFile


@pytest.fixture
def errors() -> list[tuple[Path, OSError]]:
    return []


@pytest.fixture
def probed_ssid_file(errors, monkeypatch) -> ProbedSsidFile:
    monkeypatch.setattr(Config, "save_probed_ssids", True)
    return ProbedSsidFile(on_error=lambda path, error: errors.append((path, error)))


def _lines(path: Path) -> list[str]:
    return path.read_bytes().decode("utf-8").split("\n")[:-1]


def test_pref_off_writes_nothing(probed_ssid_file, tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "save_probed_ssids", False)
    probed_ssid_file.sync({"HomeNet"})
    assert not (tmp_path / FILE_NAME).exists()


def test_catch_up_is_sorted_and_skips_ssids_already_in_the_file(probed_ssid_file, tmp_path):
    (tmp_path / FILE_NAME).write_bytes(b"Bravo\n")
    probed_ssid_file.sync({"Charlie", "Bravo", "Alpha"})
    assert _lines(tmp_path / FILE_NAME) == ["Bravo", "Alpha", "Charlie"]


def test_new_probes_are_appended_once(probed_ssid_file, tmp_path):
    probed = {"Alpha"}
    probed_ssid_file.sync(probed)
    probed_ssid_file.sync(probed)
    probed.add("Bravo")
    probed_ssid_file.sync(probed)
    probed_ssid_file.sync(probed)
    assert _lines(tmp_path / FILE_NAME) == ["Alpha", "Bravo"]


def test_ssids_with_line_breaks_are_skipped(probed_ssid_file, tmp_path):
    probed_ssid_file.sync({"two\nlines", "carriage\rreturn", "Alpha"})
    assert _lines(tmp_path / FILE_NAME) == ["Alpha"]


def test_turning_the_pref_back_on_reloads_without_duplicates(probed_ssid_file, tmp_path, monkeypatch):
    probed = {"Alpha"}
    probed_ssid_file.sync(probed)
    monkeypatch.setattr(Config, "save_probed_ssids", False)
    probed_ssid_file.sync(probed)
    probed.add("Bravo")
    monkeypatch.setattr(Config, "save_probed_ssids", True)
    probed_ssid_file.sync(probed)
    assert _lines(tmp_path / FILE_NAME) == ["Alpha", "Bravo"]


def test_changing_captures_dir_catches_up_the_new_file(probed_ssid_file, tmp_path, monkeypatch):
    probed = {"Alpha", "Bravo"}
    probed_ssid_file.sync(probed)
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path / "other"))
    probed_ssid_file.sync(probed)
    assert _lines(tmp_path / "other" / FILE_NAME) == ["Alpha", "Bravo"]


def test_write_failure_reports_once_until_the_path_changes(probed_ssid_file, errors, tmp_path, monkeypatch):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_bytes(b"")
    monkeypatch.setattr(Config, "captures_dir", str(not_a_dir))
    probed = {"Alpha"}
    for ssid in ("Bravo", "Charlie", "Delta"):
        probed.add(ssid)
        probed_ssid_file.sync(probed)
    assert [path for path, _ in errors] == [not_a_dir / FILE_NAME]

    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    probed_ssid_file.sync(probed)
    assert _lines(tmp_path / FILE_NAME) == ["Alpha", "Bravo", "Charlie", "Delta"]
    assert len(errors) == 1
