from __future__ import annotations

from pathlib import Path

from wifit3.crack.hc22000_format import hashline_id
from wifit3.persist.config import Config
from wifit3.persist.hc22000 import Hc22000Import
from wifit3.persist.meta import meta_path, read_meta

HANDSHAKE = f"WPA*02*{'01' * 16}*aabbccddeeff*112233445566*5465737431*{'11' * 32}**02"
PMKID = f"WPA*01*{'33' * 16}*aabbccddeeff*112233445566*5465737431***"
FILE_DATE = 1_700_000_000


def _imported(text: str) -> Hc22000Import:
    result = Hc22000Import()
    result.add_file("old.hc22000", text, FILE_DATE)
    result.write_files()
    return result


def test_import_writes_hashlines_to_the_aps_file_and_dates_them_in_its_meta():
    result = _imported(f"{HANDSHAKE}\n{PMKID}\n")
    assert (result.handshakes, result.pmkids, result.duplicates, result.bssids) == (1, 1, 0, {"aa:bb:cc:dd:ee:ff"})
    assert (Path(Config.captures_dir) / "Test1_aa-bb-cc-dd-ee-ff.hc22000").read_text().splitlines() == [HANDSHAKE, PMKID]
    meta = read_meta(meta_path("Test1", "aa:bb:cc:dd:ee:ff"))
    assert meta["ssid_hex"] == "5465737431"
    assert meta["hashlines"][hashline_id(HANDSHAKE)]["captured"] == [FILE_DATE]
    assert meta["hashlines"][hashline_id(PMKID)]["captured"] == [FILE_DATE]


def test_reimport_counts_duplicates_and_writes_nothing():
    _imported(f"{HANDSHAKE}\n{PMKID}\n")
    result = _imported(f"{HANDSHAKE}\n{PMKID}\n")
    assert (result.handshakes, result.pmkids, result.duplicates, result.bssids) == (0, 0, 2, set())
    assert len((Path(Config.captures_dir) / "Test1_aa-bb-cc-dd-ee-ff.hc22000").read_text().splitlines()) == 2


def test_add_timestamp_fields_are_dropped():
    _imported(f"{PMKID}\t22.11.2026 19:57:57\n")
    assert (Path(Config.captures_dir) / "Test1_aa-bb-cc-dd-ee-ff.hc22000").read_text().splitlines() == [PMKID]


def test_bad_lines_and_lines_without_essid_are_problems_and_blank_lines_are_skipped():
    no_essid = f"WPA*01*{'33' * 16}*aabbccddeeff*112233445566****"
    result = _imported(f"{PMKID}\n\nnot a hashline\n{no_essid}\n")
    assert result.pmkids == 1
    assert result.problems == [("old.hc22000:3", "not a hashline"), ("old.hc22000:4", "no SSID")]


def test_a_hashline_in_one_of_the_aps_legacy_split_files_is_a_duplicate():
    Path(Config.captures_dir).mkdir(parents=True, exist_ok=True)
    (Path(Config.captures_dir) / "Test1_aa-bb-cc-dd-ee-ff_1700000000_pmkid.hc22000").write_text(PMKID + "\n")
    result = _imported(f"{PMKID}\n")
    assert (result.pmkids, result.duplicates) == (0, 1)
