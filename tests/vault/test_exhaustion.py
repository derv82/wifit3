import json
import os

from wifit3.persist.metadata import metadata_path
from wifit3.vault.exhaustion import (
    capture_record_ids,
    exhausted_wordlists,
    record_exhaustion,
    record_id,
    wordlist_identity,
)


_RECORD_A = "WPA*01*ABCDEF*aabbccddeeff*001122334455*74657374***"
_RECORD_B = "WPA*01*123456*aabbccddeeff*66778899aabb*74657374***"


def test_wordlist_identity_uses_size_and_nanosecond_modification_time(tmp_path):
    wordlist = tmp_path / "words.txt"
    wordlist.write_bytes(b"password\r\n\xff")
    modified_ns = 1_700_000_000_123_456_789
    os.utime(wordlist, ns=(modified_ns, modified_ns))

    assert wordlist_identity(wordlist) == f"11:{modified_ns}"


def test_record_id_hashes_the_canonical_hc22000_record():
    assert record_id(f"  {_RECORD_A}  ") == record_id(_RECORD_A.lower().replace("wpa", "WPA", 1))
    assert record_id(_RECORD_A) != record_id(_RECORD_B)
    assert record_id("not a record") is None


def test_capture_record_ids_deduplicate_parseable_records(tmp_path):
    capture = tmp_path / "capture.hc22000"
    capture.write_text(f"{_RECORD_A}\ninvalid\n{_RECORD_A.lower()}\n{_RECORD_B}\n")

    assert capture_record_ids(capture) == {record_id(_RECORD_A), record_id(_RECORD_B)}


def test_wordlist_is_exhausted_only_when_recorded_for_every_record(tmp_path):
    first, second = record_id(_RECORD_A), record_id(_RECORD_B)
    assert first is not None and second is not None
    checksum = "a" * 64

    record_exhaustion("aa:bb:cc:dd:ee:ff", {first}, checksum)
    assert exhausted_wordlists("aa:bb:cc:dd:ee:ff", {first, second}) == set()

    record_exhaustion("aa:bb:cc:dd:ee:ff", {second}, checksum)
    assert exhausted_wordlists("aa:bb:cc:dd:ee:ff", {first, second}) == {checksum}
    metadata = json.loads(metadata_path("aa:bb:cc:dd:ee:ff").read_text())
    assert metadata["exhausted_wordlists"] == {first: [checksum], second: [checksum]}
