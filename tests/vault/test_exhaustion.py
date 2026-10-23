import os
import time

from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.meta import meta_path, read_meta, update_meta
from wifit3.vault.exhaustion import (Exhaustion, capture_exhaustion, capture_hashline_ids,
                                     hashline_id, record_exhausted, wordlist_identity)

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
    record_exhausted("Home_Net", _BSSID, "9:100", [hashline_id(_LINE_A), hashline_id(_LINE_B)])
    record_exhausted("Home_Net", _BSSID, "8:200", [hashline_id(_LINE_A), hashline_id("WPA*gone")])
    record_exhausted("Home_Net", _BSSID, "ssid-variants:v1", [])

    found = capture_exhaustion(capture)
    assert found["9:100"] == Exhaustion(2, 2, future)
    assert (found["8:200"].exhausted_hashlines, found["8:200"].hashlines) == (1, 2)
    assert found["8:200"].latest >= before and "ssid-variants:v1" not in found
    meta = read_meta(meta_path("Home_Net", _BSSID))
    assert (meta["ssid"], meta["nickname"]) == ("Home Net", "den")
    assert meta["exhausted"]["9:100"][hashline_id(_LINE_A)] >= before
