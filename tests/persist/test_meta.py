"""Tests for the per SSID+BSSID .meta file."""
from __future__ import annotations

import json

from wifit3.persist.common import AGGREGATED_HC22000_RE, LEGACY_CAPTURE_RE
from wifit3.persist.meta import meta_path, read_meta, update_meta


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
