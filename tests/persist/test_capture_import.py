from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from wifit3.persist.capture_import import CaptureImport
from wifit3.persist.config import Config
from wifit3.persist.meta import meta_path, read_meta
from wifit3.persist.pcap import write_pcap
from tests.persist.test_pcap import _BSSID, _SSID, _beacon, _m1_m2

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
    (selected_dir / "b.txt").write_bytes(b"")
    (selected_dir / "c.jpg").write_bytes(b"")
    result = _imported([selected_dir])
    assert (result.files, result.pmkids) == (2, 1)
    assert result.problems == [("old/b.txt", "not supported yet")]


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


def _handshake_pcaps() -> list[str]:
    return sorted(path.name for path in Path(Config.captures_dir).glob("*_handshake.pcap"))


def test_a_pcaps_4way_becomes_a_hashline_and_a_pcap_dated_by_its_frames(selected_dir: Path):
    write_pcap(selected_dir / "a.pcap", [(_beacon(), 999.0), *_m1_m2(1, 1000.0)])
    result = _imported([selected_dir / "a.pcap"])
    assert (result.handshakes, result.bssids, result.problems) == (1, {_BSSID}, [])
    assert _handshake_pcaps() == ["HomeNet_02-00-00-00-00-01_1000_handshake.pcap"]
    dates = read_meta(meta_path(_SSID, _BSSID))["hashlines"]
    assert [record["captured"] for record in dates.values()] == [[1000]]


def test_a_pcap_import_writes_no_pcap_when_save_pcap_is_off(selected_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Config, "save_pcap", False)
    write_pcap(selected_dir / "a.pcap", [(_beacon(), 999.0), *_m1_m2(1, 1000.0)])
    result = _imported([selected_dir / "a.pcap"])
    assert result.handshakes == 1
    assert _handshake_pcaps() == []


def test_cumulative_pcaps_write_each_4way_once(selected_dir: Path):
    first, second = _m1_m2(1, 1000.0), _m1_m2(2, 1060.0)
    write_pcap(selected_dir / "old.pcap", [(_beacon(), 999.0), *first])
    write_pcap(selected_dir / "new.pcap", [(_beacon(), 999.0), *first, *second])
    result = _imported([selected_dir])
    assert (result.handshakes, result.duplicates) == (2, 1)
    assert _handshake_pcaps() == ["HomeNet_02-00-00-00-00-01_1000_handshake.pcap",
                                  "HomeNet_02-00-00-00-00-01_1060_handshake.pcap"]


def test_reimporting_a_pcap_writes_no_second_pcap(selected_dir: Path):
    write_pcap(selected_dir / "a.pcap", [(_beacon(), 999.0), *_m1_m2(1, 1000.0)])
    _imported([selected_dir / "a.pcap"])
    result = _imported([selected_dir / "a.pcap"])
    assert (result.handshakes, result.duplicates) == (0, 1)
    assert len(_handshake_pcaps()) == 1


def test_an_ap_with_no_ssid_and_a_pcap_with_nothing_crackable_are_problems(selected_dir: Path):
    hidden_beacon = _beacon().replace(b"\x00\x07HomeNet", b"\x00\x00")
    write_pcap(selected_dir / "hidden.pcap", [(hidden_beacon, 999.0), *_m1_m2(1, 1000.0)])
    write_pcap(selected_dir / "beacon.pcap", [(_beacon(), 999.0)])
    result = _imported([selected_dir])
    assert result.problems == [("old/beacon.pcap", "no handshakes or PMKIDs found"),
                               ("old/hidden.pcap", f"AP {_BSSID} has no SSID, skipped")]
