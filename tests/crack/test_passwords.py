"""WPA passphrase tests: SSID variants, wordlist streaming."""
from pathlib import Path

from wifit3.crack import passwords
from wifit3.crack.default_psks import default_psks
from wifit3.crack.passwords import expand_wordlists, read_passwords, ssid_variants
from wifit3.dot11.wpa import is_valid_psk


def test_ssid_variants_start_with_the_cases_and_primary_suffixes():
    assert ssid_variants("HomeNet")[:12] == [
        "HomeNet123", "HomeNet1234", "HomeNet12345678", "HomeNet!",
        "homenet123", "homenet1234", "homenet12345678", "homenet!",
        "HOMENET123", "HOMENET1234", "HOMENET12345678", "HOMENET!",
    ]
    assert ssid_variants("lowercase")[:10] == [
        "lowercase", "LOWERCASE", "lowercase123", "lowercase1234", "lowercase12345678",
        "lowercase!", "LOWERCASE123", "LOWERCASE1234", "LOWERCASE12345678", "LOWERCASE!",
    ]


def test_ssid_variants_add_leetspeak_separators_and_common_suffixes():
    variants = ssid_variants("HomeNet")
    assert {"H0meNet1", "Hom3Net1", "H0m3N3t1", "HomeNet_123", "HomeNet-1234", "HomeNet@2026",
            "HomeNetpassword"} <= set(variants)
    assert len(variants) == len(set(variants))
    assert all(is_valid_psk(psk) for psk in variants)


def test_ssid_variants_pad_numbers_only_as_far_as_eight_bytes_needs():
    ducky = set(ssid_variants("ducky"))
    assert {"ducky012", "ducky001", "ducky1234"} <= ducky
    assert not {"ducky12", "ducky0012"} & ducky
    longname = set(ssid_variants("longname"))
    assert {"longname12", "longname9999"} <= longname and "longname012" not in longname
    assert len(ssid_variants("ab")) < 100


def test_all_ssid_variants_try_every_default_psk_before_any_ssid_variant():
    variants = passwords.all_ssid_variants([("HomeNet", "02:ab:cd:00:00:01"),
                                            ("CafeWifi", "02:ab:cd:00:00:02")])
    defaults = default_psks("HomeNet", "02:ab:cd:00:00:01") + default_psks("CafeWifi", "02:ab:cd:00:00:02")
    assert variants[:len(defaults)] == defaults
    assert variants[len(defaults):] == list(dict.fromkeys(ssid_variants("HomeNet") + ssid_variants("CafeWifi")))


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


# ----- wordlist expansion ---------------------------------------------------

def test_expand_wordlists_lists_the_files_inside_a_directory(tmp_path):
    directory = tmp_path / "dictionary folder"
    directory.mkdir()
    (directory / "second.txt").write_text("two\n")
    (directory / "first.txt").write_text("one\n")
    (directory / "nested").mkdir()

    assert expand_wordlists([str(directory)]) == [
        str((directory / "first.txt").resolve()), str((directory / "second.txt").resolve())]


def test_expand_wordlists_keeps_the_requested_order_of_files(tmp_path):
    second, first = tmp_path / "z.txt", tmp_path / "a.txt"
    second.write_text("two\n")
    first.write_text("one\n")

    assert expand_wordlists([str(second), str(first)]) == [str(second.resolve()),
                                                           str(first.resolve())]


def test_expand_wordlists_drops_what_does_not_exist(tmp_path):
    assert expand_wordlists([str(tmp_path / "missing.txt")]) == []
