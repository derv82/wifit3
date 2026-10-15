import copy

import pytest

import wifit3.persist.config as cfg
from wifit3.persist.config import Config, ConfigError

_DEFAULTS = {n: getattr(Config, n)
             for n in (
                 "theme", "scanner_sort", "scanner_sort_reverse", "scanner_sort_delay",
                 "silenced_bssids", "hide_silenced", "log_level", "captures_dir", "save_pcap",
                 "hashcat_path", "wordlist_path", "hashcat_args", "decloaked_ssids",
                 "wps_auto_pbc")}


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


def test_tool_paths_default_to_unset(config_path):
    assert Config.hashcat_path is None and Config.wordlist_path is None


def test_unset_tool_paths_are_omitted_from_the_file(config_path):
    """TOML has no null, so 'not configured' has to be an absent key -- never the string
    'None', which would then load back as a real (broken) path."""
    Config.save()
    text = config_path.read_text()
    assert "hashcat_path" not in text and "wordlist_path" not in text
    assert "hashcat_args" not in text


def test_tool_paths_round_trip_when_set(config_path):
    Config.hashcat_path = r"D:\tools\hashcat\hashcat.exe"
    Config.wordlist_path = r"D:\wordlists\Top29Million.txt"
    Config.hashcat_args = "-a 0 -w 3"
    Config.save()
    Config.hashcat_path = Config.wordlist_path = Config.hashcat_args = None
    Config.load()
    assert Config.hashcat_path == r"D:\tools\hashcat\hashcat.exe"
    assert Config.wordlist_path == r"D:\wordlists\Top29Million.txt"
    assert Config.hashcat_args == "-a 0 -w 3"


def test_empty_tool_path_loads_as_unset(config_path):
    config_path.write_text("hashcat_path = ''\n")
    Config.load()
    assert Config.hashcat_path is None


def test_load_without_tool_paths_keeps_whatever_is_set(config_path):
    config_path.write_text('theme = "gruvbox"\n')
    Config.hashcat_path = "/usr/bin/hashcat"
    Config.load()
    assert Config.hashcat_path == "/usr/bin/hashcat"


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
