import copy

import pytest

import wifit3.persist.config as cfg
from wifit3.models import ToolName
from wifit3.persist.config import (Config, ConfigError, CpuCrackSettings, HashcatSettings,
                                   HashtopolisSettings)

_DEFAULTS = {n: copy.deepcopy(getattr(Config, n))
             for n in (
                 "theme", "scanner_sort", "scanner_sort_reverse", "scanner_sort_delay",
                 "silenced_bssids", "hide_silenced", "log_level", "captures_dir", "save_pcap",
                 "decloaked_ssids", "wps_auto_pbc", "auto_crack", "cpu_crack", "hashcat",
                 "hashtopolis")}


@pytest.fixture(autouse=True)
def _restore_defaults():
    for n, v in _DEFAULTS.items():
        setattr(Config, n, copy.deepcopy(v))
    yield
    for n, v in _DEFAULTS.items():
        setattr(Config, n, copy.deepcopy(v))


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg, "_PATH", path)
    return path


def test_load_missing_file_keeps_defaults(config_path):
    before = {n: getattr(Config, n) for n in _DEFAULTS}
    Config.load()
    assert all(getattr(Config, n) == before[n] for n in before)


def test_load_reads_values_and_ignores_unknown_keys(config_path):
    config_path.write_text(
        'theme = "gruvbox"\nscanner_sort = "channel"\nscanner_sort_reverse = false\nfuture = 1\n')
    Config.load()
    assert Config.theme == "gruvbox"
    assert Config.scanner_sort == "channel"
    assert Config.scanner_sort_reverse is False


def test_load_absent_key_keeps_default(config_path):
    config_path.write_text('theme = "nord"\n')
    Config.load()
    assert Config.theme == "nord"
    assert Config.scanner_sort == "signal"


def test_load_corrupt_file_raises(config_path):
    config_path.write_text("this is = = not toml")
    with pytest.raises(ConfigError):
        Config.load()


def test_save_then_load_round_trips(config_path):
    Config.theme, Config.scanner_sort, Config.scanner_sort_reverse = "nord", "channel", False
    Config.save()
    Config.theme, Config.scanner_sort, Config.scanner_sort_reverse = "x", "y", True
    Config.load()
    assert (Config.theme, Config.scanner_sort, Config.scanner_sort_reverse) == ("nord", "channel", False)


def test_save_load_preserves_windows_path_literally(config_path):
    Config.theme = r"C:\Users\Someone\theme"
    Config.save()
    assert r"'C:\Users\Someone\theme'" in config_path.read_text("utf-8")
    Config.theme = "x"
    Config.load()
    assert Config.theme == r"C:\Users\Someone\theme"


def test_save_load_round_trips_captures_dir_with_apostrophe(config_path):
    Config.captures_dir = r"C:\Users\O'Brien\captures"
    Config.save()
    Config.captures_dir = "x"
    Config.load()
    assert Config.captures_dir == r"C:\Users\O'Brien\captures"


def test_save_failure_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(cfg, "_PATH", blocker / "config.toml")
    with pytest.raises(ConfigError):
        Config.save()


def test_fmt_scalars():
    assert cfg._fmt(True) == "true"
    assert cfg._fmt(False) == "false"
    assert cfg._fmt(3) == "3"
    assert cfg._fmt("signal") == "'signal'"


def test_fmt_list():
    assert cfg._fmt([]) == "[]"
    assert cfg._fmt(["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]) == \
        "['aa:bb:cc:dd:ee:ff', '11:22:33:44:55:66']"


def test_silenced_bssids_save_load_roundtrip(config_path):
    Config.silenced_bssids = ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]
    Config.save()
    assert "silenced_bssids = ['aa:bb:cc:dd:ee:ff', '11:22:33:44:55:66']" \
        in config_path.read_text("utf-8")
    Config.silenced_bssids = []
    Config.load()
    assert Config.silenced_bssids == ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]


def test_silenced_bssids_load_normalizes_case(config_path):
    config_path.write_text("silenced_bssids = ['AA:BB:CC:DD:EE:FF']\n")
    Config.load()
    assert Config.silenced_bssids == ["aa:bb:cc:dd:ee:ff"]


def test_silenced_bssids_bad_type_keeps_default(config_path):
    Config.silenced_bssids = []
    config_path.write_text('silenced_bssids = "not-a-list"\n')
    Config.load()
    assert Config.silenced_bssids == []


def test_hide_silenced_save_load_roundtrip(config_path):
    assert Config.hide_silenced is False
    Config.hide_silenced = True
    Config.save()
    Config.hide_silenced = False
    Config.load()
    assert Config.hide_silenced is True


def test_wps_auto_pbc_save_load_roundtrip(config_path):
    assert Config.wps_auto_pbc is True
    Config.wps_auto_pbc = False
    Config.save()
    Config.wps_auto_pbc = True
    Config.load()
    assert Config.wps_auto_pbc is False


def test_wps_auto_pbc_absent_key_keeps_default(config_path):
    config_path.write_text('theme = "nord"\n')
    Config.load()
    assert Config.wps_auto_pbc is True


def test_auto_crack_save_load_roundtrip(config_path):
    assert Config.auto_crack is ToolName.CPU_CRACK
    Config.auto_crack = ToolName.HASHCAT
    Config.save()
    assert "auto_crack = 'hashcat'" in config_path.read_text()
    Config.auto_crack = None
    Config.load()
    assert Config.auto_crack is ToolName.HASHCAT


def test_auto_crack_off_roundtrip(config_path):
    Config.auto_crack = None
    Config.save()
    Config.auto_crack = ToolName.CPU_CRACK
    Config.load()
    assert Config.auto_crack is None


def test_auto_crack_absent_key_keeps_default(config_path):
    config_path.write_text('theme = "nord"\n')
    Config.load()
    assert Config.auto_crack is ToolName.CPU_CRACK


@pytest.mark.parametrize(("line", "expected"), [
    ("auto_crack = true", ToolName.CPU_CRACK),
    ("auto_crack = false", None),
    ("auto_crack = ''", None),
    ("auto_crack = 'nonexistent'", None),
    ("auto_crack_tool = 'hashtopolis'", ToolName.HASHTOPOLIS),
])
def test_auto_crack_load_maps_legacy_and_unknown_values(config_path, line, expected):
    config_path.write_text(line + "\n")
    Config.load()
    assert Config.auto_crack is expected


def test_hide_silenced_absent_key_keeps_default(config_path):
    config_path.write_text('theme = "nord"\n')
    Config.load()
    assert Config.hide_silenced is False


def test_is_silenced_is_case_insensitive():
    Config.silenced_bssids = ["aa:bb:cc:dd:ee:ff"]
    assert Config.is_silenced("AA:BB:CC:DD:EE:FF") is True
    assert Config.is_silenced("aa:bb:cc:dd:ee:ff") is True
    assert Config.is_silenced("11:22:33:44:55:66") is False


def test_scanner_sort_delay_save_load_roundtrip(config_path):
    Config.scanner_sort_delay = 5.0
    Config.save()
    assert "scanner_sort_delay = 5.0" in config_path.read_text("utf-8")
    Config.scanner_sort_delay = 1.0
    Config.load()
    assert Config.scanner_sort_delay == 5.0


def test_tool_settings_defaults(config_path):
    assert Config.cpu_crack == CpuCrackSettings(wordlists=[], default_passwords=True,
                                                ap_ssid_passwords=True, probed_ssid_passwords=True)
    assert Config.hashcat == HashcatSettings()
    assert Config.hashtopolis == HashtopolisSettings()


def test_unset_tool_settings_are_omitted_from_the_file(config_path):
    """TOML has no null, so 'not configured' has to be an absent key -- never the string
    'None', which would then load back as a real (broken) path."""
    Config.save()
    text = config_path.read_text()
    assert "[hashtopolis]" not in text and "path" not in text
    assert "None" not in text


def test_hashcat_settings_round_trip_in_their_table(config_path):
    saved = HashcatSettings(path=r"D:\tools\hashcat\hashcat.exe", args="-a 0 -w 3",
                            wordlists=[r"D:\wordlists\Top29Million.txt", r"D:\wordlists\rockyou.txt"],
                            default_passwords=False, probed_ssid_passwords=False)
    Config.hashcat = copy.deepcopy(saved)
    Config.save()
    assert "\n[hashcat]\npath = " in config_path.read_text()
    Config.hashcat = HashcatSettings()
    Config.load()
    assert Config.hashcat == saved


def test_cpu_crack_password_sources_round_trip(config_path):
    Config.cpu_crack = CpuCrackSettings(wordlists=["/dicts/small.txt"], default_passwords=False,
                                        ap_ssid_passwords=True, probed_ssid_passwords=False)
    Config.save()
    Config.cpu_crack = CpuCrackSettings()
    Config.load()
    assert Config.cpu_crack == CpuCrackSettings(wordlists=["/dicts/small.txt"],
                                                default_passwords=False, ap_ssid_passwords=True,
                                                probed_ssid_passwords=False)


def test_cpu_crack_and_hashcat_wordlists_are_separate(config_path):
    Config.cpu_crack = CpuCrackSettings(wordlists=["/dicts/small.txt"])
    Config.hashcat = HashcatSettings(wordlists=["/dicts/rockyou.txt"])
    Config.save()
    Config.cpu_crack, Config.hashcat = CpuCrackSettings(), HashcatSettings()
    Config.load()
    assert Config.cpu_crack.wordlists == ["/dicts/small.txt"]
    assert Config.hashcat.wordlists == ["/dicts/rockyou.txt"]


def test_hashtopolis_settings_save_load_roundtrip(config_path):
    saved = HashtopolisSettings(url="http://server:8080", access_key="key", wordlist_id=3,
                                wordlist_name="rockyou.txt", access_group_id=1,
                                access_group_name="Default Group")
    Config.hashtopolis = copy.deepcopy(saved)
    Config.save()
    Config.hashtopolis = HashtopolisSettings()
    Config.load()
    assert Config.hashtopolis == saved


def test_a_value_of_the_wrong_type_keeps_the_default(config_path):
    config_path.write_text("[hashtopolis]\nurl = 5\nwordlist_id = 'three'\naccess_group_id = true\n"
                           "[hashcat]\nwordlists = 'rockyou.txt'\npath = ''\n")
    Config.load()
    assert Config.hashtopolis == HashtopolisSettings()
    assert Config.hashcat == HashcatSettings()


def test_legacy_flat_keys_migrate_into_the_tables(config_path):
    config_path.write_text(
        "hashcat_path = '/usr/bin/hashcat'\nhashcat_args = '-w 3'\n"
        "wordlist_paths = ['/dicts/rockyou.txt']\n"
        "hashtopolis_url = 'http://server:8080'\nhashtopolis_access_key = 'key'\n"
        "hashtopolis_wordlist_id = 3\nhashtopolis_wordlist_name = 'rockyou.txt'\n"
        "hashtopolis_access_group_id = 1\n")
    Config.load()
    assert Config.hashcat == HashcatSettings(path="/usr/bin/hashcat", args="-w 3",
                                             wordlists=["/dicts/rockyou.txt"])
    assert Config.cpu_crack.wordlists == []
    assert Config.hashtopolis == HashtopolisSettings(
        url="http://server:8080", access_key="key", wordlist_id=3, wordlist_name="rockyou.txt",
        access_group_id=1)
    Config.save()
    text = config_path.read_text()
    assert "hashcat_path" not in text and "wordlist_paths" not in text
    assert "hashtopolis_url" not in text


def test_a_single_wordlist_path_from_an_older_config_still_loads(config_path):
    config_path.write_text(r"wordlist_path = 'D:\wordlists\rockyou.txt'" + "\n")
    Config.load()
    assert Config.hashcat.wordlists == [r"D:\wordlists\rockyou.txt"]


def test_a_table_wins_over_the_legacy_keys(config_path):
    config_path.write_text("hashcat_path = '/old/hashcat'\n[hashcat]\npath = '/new/hashcat'\n")
    Config.load()
    assert Config.hashcat.path == "/new/hashcat"


def test_load_without_tool_settings_keeps_whatever_is_set(config_path):
    config_path.write_text('theme = "gruvbox"\n')
    Config.hashcat.path = "/usr/bin/hashcat"
    Config.load()
    assert Config.hashcat.path == "/usr/bin/hashcat"


def test_decloaked_ssids_save_load_roundtrip(config_path):
    assert Config.remember_decloak("AA:BB:CC:DD:EE:FF", "Net-Guest")
    assert not Config.remember_decloak("aa:bb:cc:dd:ee:ff", "Net-Guest")   # already stored
    Config.save()
    assert "[decloak]\n'aa:bb:cc:dd:ee:ff' = 'Net-Guest'" in config_path.read_text("utf-8")
    Config.decloaked_ssids = {}
    Config.load()
    assert Config.decloaked_ssid("aa:bb:cc:dd:ee:ff") == "Net-Guest"


def test_an_over_long_or_empty_ssid_is_not_remembered():
    assert not Config.remember_decloak("aa:bb:cc:dd:ee:ff", "x" * 33)
    assert not Config.remember_decloak("aa:bb:cc:dd:ee:ff", "")
    assert Config.decloaked_ssids == {}


def test_decloaked_ssids_load_drops_junk_entries(config_path):
    config_path.write_text("[decloak]\n'AA:BB:CC:DD:EE:FF' = 'Net'\n'11:22:33:44:55:66' = 7\n")
    Config.load()
    assert Config.decloaked_ssids == {"aa:bb:cc:dd:ee:ff": "Net"}
