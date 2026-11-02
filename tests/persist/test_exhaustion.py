import os
import time

from wifit3.crack.hc22000_format import hashline_id
from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.meta import meta_path, read_bssid_metas, read_meta, update_meta
from wifit3.persist.config import Config
from wifit3.persist.exhaustion import (ExhaustedWordlist, Exhaustion, capture_hashline_ids, read_exhaustion,
                                       read_exhausted_wordlists, record_exhausted, wordlist_identity)

_BSSID = "02:00:00:00:00:01"
_LINE_A = f"WPA*01*{'aa' * 16}*020000000001*020000000002*486f6d65***"
_LINE_B = f"WPA*01*{'bb' * 16}*020000000001*020000000003*486f6d65***"


def _capture(tmp_path, lines: list[str]) -> PersistedCapture:
    path = tmp_path / "Home_Net_02-00-00-00-00-01.hc22000"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return PersistedCapture(type=CaptureType.PMKID, timestamp=0, path=str(path), bssid=_BSSID,
                            ssid="Home_Net")


def test_wordlist_identity_is_size_and_mtime_in_nanoseconds(tmp_path):
    wordlist = tmp_path / "words.txt"
    wordlist.write_bytes(b"password\n")
    modified_ns = 1_700_000_000_123_456_700      # NTFS keeps mtime in 100 ns steps
    os.utime(wordlist, ns=(modified_ns, modified_ns))
    assert wordlist_identity(wordlist) == f"9:{modified_ns}"


def test_hashline_id_ignores_case_and_surrounding_space(tmp_path):
    assert hashline_id(f"  {_LINE_A.upper()} \n") == hashline_id(_LINE_A)
    assert len(hashline_id(_LINE_A)) == 16 and hashline_id(_LINE_A) != hashline_id(_LINE_B)
    capture = _capture(tmp_path, [_LINE_A, "not a hashline", _LINE_A.lower(), _LINE_B])
    assert capture_hashline_ids(capture.path) == [hashline_id(_LINE_A), hashline_id(_LINE_B)]


def test_exhaustion_counts_the_captures_current_hashlines_and_keeps_the_latest(tmp_path):
    capture = _capture(tmp_path, [_LINE_A, _LINE_B])
    future = int(time.time()) + 1_000_000
    update_meta("Home Net", _BSSID, nickname="den",
                exhausted={"9:100": {hashline_id(_LINE_A): 1_000, hashline_id(_LINE_B): future}})
    before = int(time.time())
    record_exhausted("Home_Net", _BSSID, "9:100", "a.txt", [hashline_id(_LINE_A), hashline_id(_LINE_B)])
    record_exhausted("Home_Net", _BSSID, "8:200", "b.txt", [hashline_id(_LINE_A), hashline_id("WPA*gone")])
    record_exhausted("Home_Net", _BSSID, "ssid-variants:v1", "SSID variants", [])

    found = read_exhaustion(read_bssid_metas(_BSSID), capture_hashline_ids(capture.path))
    assert found["9:100"] == Exhaustion("9:100", ("a.txt",), 2, 2, future)
    assert (found["8:200"].exhausted_hashlines, found["8:200"].hashlines) == (1, 2)
    assert found["8:200"].latest >= before and "ssid-variants:v1" not in found
    meta = read_meta(meta_path("Home_Net", _BSSID))
    assert (meta["ssid"], meta["nickname"]) == ("Home Net", "den")
    assert meta["exhausted"]["9:100"][hashline_id(_LINE_A)] >= before


def test_read_exhaustion_merges_every_ssid_and_names_old_records(tmp_path):
    configured = tmp_path / "rockyou.txt"
    configured.write_bytes(b"password\n")
    Config.hashcat.wordlists = [str(tmp_path)]      # a directory; conftest resets the settings
    a, b = hashline_id(_LINE_A), hashline_id(_LINE_B)
    record_exhausted("Old", _BSSID, "9:100", "a.txt", [a])
    record_exhausted("New", _BSSID, "9:100", "a-copy.txt", [b])
    update_meta("New", _BSSID, exhausted={"9:100": {b: 1}, wordlist_identity(configured): {b: 1}})

    found = read_exhaustion(read_bssid_metas(_BSSID), [a, b])
    assert (found["9:100"].wordlist_names, found["9:100"].exhausted_hashlines) == (("a-copy.txt", "a.txt"), 2)
    assert found[wordlist_identity(configured)].wordlist_names == ("rockyou.txt",)
    assert read_exhaustion(read_bssid_metas(_BSSID), [hashline_id("WPA*other")]) == {}


def test_a_record_with_no_name_and_no_configured_wordlist_has_no_names(tmp_path):
    a = hashline_id(_LINE_A)
    update_meta("Home", _BSSID, exhausted={"7:700": {a: 1}})
    assert read_exhaustion(read_bssid_metas(_BSSID), [a])["7:700"].wordlist_names == ()


def test_read_exhausted_wordlists_lists_each_hashlines_wordlists(tmp_path):
    a, b = hashline_id(_LINE_A), hashline_id(_LINE_B)
    record_exhausted("Old", _BSSID, "9:100", "a.txt", [a, b])
    record_exhausted("New", _BSSID, "8:800", "b.txt", [b])

    found = read_exhausted_wordlists(read_bssid_metas(_BSSID), [a, b, hashline_id("WPA*other")])
    assert found == {a: frozenset({ExhaustedWordlist("9:100", ("a.txt",))}),
                     b: frozenset({ExhaustedWordlist("9:100", ("a.txt",)), ExhaustedWordlist("8:800", ("b.txt",))})}
