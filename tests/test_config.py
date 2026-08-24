import copy

import pytest

import wifit3.persist.config as cfg
from wifit3.persist.config import Config, ConfigError

_DEFAULTS = {n: getattr(Config, n)
             for n in ("theme", "scanner_sort", "scanner_sort_reverse", "silenced_bssids")}


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


def test_is_silenced_is_case_insensitive():
    Config.silenced_bssids = ["aa:bb:cc:dd:ee:ff"]
    assert Config.is_silenced("AA:BB:CC:DD:EE:FF") is True
    assert Config.is_silenced("aa:bb:cc:dd:ee:ff") is True
    assert Config.is_silenced("11:22:33:44:55:66") is False
