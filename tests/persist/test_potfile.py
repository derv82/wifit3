from wifit3.persist.potfile import (PotfileEntry, missing_entries, parse_potfile, potfile_line,
                                    potfile_path, read_potfile, remove_potfile_key, write_potfile)

_LINE = "aabbccddeeff:112233445566:TestAP-DWPA:88888888"
_ENTRY = PotfileEntry("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66", "TestAP-DWPA", "88888888")


def test_a_line_parses_into_an_entry_with_colonised_macs():
    assert parse_potfile(_LINE + "\n") == [_ENTRY]


def test_a_line_round_trips_through_the_dwpa_format():
    assert potfile_line(_ENTRY) == _LINE


def test_duplicates_and_unusable_lines_are_dropped():
    text = "\n".join([_LINE, _LINE, "nonsense", "aabbccddeeff:112233445566::88888888",
                      "aabbccddeeff:112233445566:Short:short", "zzbbccddeeff:112233445566:X:88888888"])

    assert parse_potfile(text) == [_ENTRY]


def test_write_read_and_remove_keys_one_at_a_time(tmp_path):
    second = PotfileEntry("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66", "TestAP-DWPA", "hunter22")
    path = tmp_path / "TestAP-DWPA_aa-bb-cc-dd-ee-ff.potfile"

    write_potfile(path, [_ENTRY, second])
    assert read_potfile(path) == [_ENTRY, second]

    remove_potfile_key(path, "88888888")
    assert read_potfile(path) == [second]

    remove_potfile_key(path, "hunter22")
    assert not path.exists()


def test_missing_entries_keeps_only_keys_the_potfile_lacks():
    existing = [_ENTRY]
    second = PotfileEntry("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66", "TestAP-DWPA", "hunter22")

    assert missing_entries(existing, [_ENTRY, second, second]) == [second]


def test_the_potfile_path_reuses_the_aps_file_whatever_its_ssid(tmp_path):
    existing = tmp_path / "OldName_aa-bb-cc-dd-ee-ff.potfile"
    existing.write_text("")

    assert potfile_path(tmp_path, "NewName", "aa:bb:cc:dd:ee:ff") == existing
    assert potfile_path(tmp_path, "NewName", "02:00:00:00:00:01") == \
        tmp_path / "NewName_02-00-00-00-00-01.potfile"
