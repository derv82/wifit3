from wifit3.crack import default_psks as default_psks_module
from wifit3.crack.default_psks import bssid_as_psk, default_psks


def test_bssid_as_psk_is_the_bssid_then_its_last_10_and_8_hex_digits_in_both_cases():
    assert bssid_as_psk("HomeNet", "02:ab:cd:ef:00:01") == [
        "02ABCDEF0001", "02abcdef0001", "ABCDEF0001", "abcdef0001", "CDEF0001", "cdef0001"]


def test_default_psks_dedupes_a_bssid_with_no_letters():
    assert default_psks("HomeNet", "02:00:00:00:00:01") == ["020000000001", "0000000001", "00000001"]


def test_default_psks_drops_what_is_not_a_wpa_passphrase(monkeypatch):
    monkeypatch.setattr(default_psks_module, "ALGORITHMS", [lambda ssid, bssid: ["short", "longenough"]])
    assert default_psks_module.default_psks("HomeNet", "02:00:00:00:00:01") == ["longenough"]
