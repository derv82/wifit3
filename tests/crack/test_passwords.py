"""WPA passphrase tests: length bounds, SSID variants, wordlist streaming."""
from pathlib import Path

from wifit3.crack import passwords
from wifit3.crack.passwords import is_valid_psk, read_passwords, ssid_variants


def test_is_valid_psk_length_bounds():
    assert not is_valid_psk("a" * 7)
    assert is_valid_psk("a" * 8)
    assert is_valid_psk("a" * 63)
    assert not is_valid_psk("a" * 64)


def test_is_valid_psk_counts_utf8_bytes():
    assert not is_valid_psk("é" * 32)
    assert is_valid_psk("é" * 31)


def test_ssid_variants_order_dedupe_and_filter():
    assert ssid_variants("HomeNet") == [
        "HomeNet123", "HomeNet1234", "HomeNet12345678", "HomeNet!",
        "homenet123", "homenet1234", "homenet12345678", "homenet!",
        "HOMENET123", "HOMENET1234", "HOMENET12345678", "HOMENET!",
    ]
    assert ssid_variants("lowercase") == [
        "lowercase", "LOWERCASE", "lowercase123", "lowercase1234", "lowercase12345678",
        "lowercase!", "LOWERCASE123", "LOWERCASE1234", "LOWERCASE12345678", "LOWERCASE!",
    ]


def test_read_passwords_strips_newlines_and_skips_invalid(tmp_path: Path):
    wordlist = tmp_path / "words.txt"
    wordlist.write_bytes(b"password1\r\nshort\nhashcat!\n\xff\xfevalidpass\n" + b"x" * 64 + b"\n")
    assert list(read_passwords(wordlist)) == ["password1", "hashcat!", "validpass"]


def test_ssid_passwords_tries_every_ssid_before_any_variant():
    assert passwords.ssid_passwords(["HomeNet1", "CafeWifi"])[:2] == ["HomeNet1", "CafeWifi"]
    assert "homenet1123" in passwords.ssid_passwords(["HomeNet1"])


def test_ssid_passwords_stops_at_the_cap():
    ssids = [f"network{i:05d}" for i in range(2000)]
    result = passwords.ssid_passwords(ssids)
    assert len(result) == passwords._MAX_SSID_PASSWORDS
    assert result == ssids[:passwords._MAX_SSID_PASSWORDS]
