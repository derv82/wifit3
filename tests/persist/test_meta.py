"""Tests for the per SSID+BSSID .meta file."""
from __future__ import annotations

import json
from pathlib import Path

from wifit3.persist.common import AGGREGATED_HC22000_RE, LEGACY_CAPTURE_RE
from wifit3.persist.meta import (bssid_meta_paths, meta_path, nickname_aps, read_meta, read_nickname,
                                 set_nickname, update_meta)


def test_nickname_survives_ssid_changes_and_clearing_preserves_metadata(tmp_path):
    bssid = "AA:BB:CC:DD:EE:FF"
    old = update_meta("Old", bssid, nickname="old", extra={"keep": True})
    other = update_meta("Other", bssid, nickname="stale", custom=3)
    assert read_nickname(bssid, "New") == "old"
    assert read_nickname(bssid, "Other") == "stale"
    set_nickname("New", bssid, "home")
    assert read_nickname(bssid, "Old") == "home"
    assert read_meta(old)["extra"] == {"keep": True}
    assert read_meta(other)["custom"] == 3
    assert "nickname" not in read_meta(old) and "nickname" not in read_meta(other)
    assert nickname_aps() == {"aa:bb:cc:dd:ee:ff": "New"}
    set_nickname("Unknown", bssid, None)
    assert read_nickname(bssid) == ""
    assert nickname_aps() == {}
    assert not meta_path("Unknown", bssid).exists()


def test_nickname_only_preserves_original_ssid_and_legacy_metadata(tmp_path):
    bssid = "aa:bb:cc:dd:ee:ff"
    legacy = meta_path("Café Net", bssid)
    legacy.write_text(json.dumps({"nickname": "old", "custom": {"keep": True}}), encoding="utf-8")
    assert nickname_aps() == {bssid: None}
    set_nickname("Café Net", bssid, "home")
    assert nickname_aps() == {bssid: "Café Net"}
    assert read_meta(legacy)["custom"] == {"keep": True}
    set_nickname("Café Net", bssid, None)
    assert read_meta(legacy)["ssid"] == "Café Net"
    assert read_meta(legacy)["custom"] == {"keep": True}


def test_meta_path_sorts_next_to_the_captures(tmp_path):
    assert meta_path("Café Net", "AA:BB:CC:DD:EE:FF") == tmp_path / "Caf__Net_aa-bb-cc-dd-ee-ff.meta"
    assert meta_path(None, "aa:bb:cc:dd:ee:ff").name == "hidden_aa-bb-cc-dd-ee-ff.meta"


def test_read_meta_is_empty_for_missing_invalid_or_non_object(tmp_path):
    path = tmp_path / "x.meta"
    assert read_meta(path) == {}
    path.write_text("{not json", encoding="utf-8")
    assert read_meta(path) == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert read_meta(path) == {}


def test_update_meta_merges_keeps_unknown_keys_and_none_removes(tmp_path):
    path = update_meta("Café", "AA-BB-CC-DD-EE-FF", nickname="Home")
    data = read_meta(path)
    data["exhausted"] = ["rockyou.txt"]
    path.write_text(json.dumps(data), encoding="utf-8")

    update_meta("Café", "aa:bb:cc:dd:ee:ff")
    assert read_meta(path) == {"bssid": "aa:bb:cc:dd:ee:ff", "ssid": "Café",
                               "nickname": "Home", "exhausted": ["rockyou.txt"]}
    assert "Café" in path.read_text(encoding="utf-8")
    update_meta("Café", "aa:bb:cc:dd:ee:ff", nickname=None)
    assert "nickname" not in read_meta(path)
    assert not path.with_name(path.name + ".tmp").exists()


def test_update_meta_omits_a_hidden_ssid(tmp_path):
    assert read_meta(update_meta(None, "aa:bb:cc:dd:ee:ff")) == {"bssid": "aa:bb:cc:dd:ee:ff"}


def test_capture_regexes_do_not_match_meta_files():
    name = "Net_aa-bb-cc-dd-ee-ff.meta"
    assert LEGACY_CAPTURE_RE.match(name) is None
    assert AGGREGATED_HC22000_RE.match(name) is None


def test_bssid_meta_paths_lists_while_another_thread_writes_a_meta(monkeypatch):
    update_meta("NetA", "aa:bb:cc:dd:ee:ff", nickname="a")
    update_meta("NetB", "aa:bb:cc:dd:ee:ff", nickname="b")
    assert len(bssid_meta_paths("aa:bb:cc:dd:ee:ff")) == 2
    exists = Path.exists
    writes: list[str] = []

    def exists_while_a_meta_is_written(path: Path) -> bool:
        if not writes:
            writes.append("NetC")
            update_meta("NetC", "aa:bb:cc:dd:ee:ff", nickname="c")
        return exists(path)
    monkeypatch.setattr(Path, "exists", exists_while_a_meta_is_written)

    assert len(bssid_meta_paths("aa:bb:cc:dd:ee:ff")) == 2
