from pathlib import Path

import wifit3.persist.config as cfg


def test_load_config_from_local_toml(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfg, "user_config_dir", None)
    Path("config.toml").write_text(
        'theme = "ansi-dark"\nfuture_list = ["a", "b"]\nfuture_map = { key = "value" }\n')

    loaded = cfg.load_config()

    assert loaded.theme == "ansi-dark"
    raw = cfg._load_file(Path("config.toml"))
    assert raw["future_list"] == ["a", "b"]
    assert raw["future_map"] == {"key": "value"}


def test_load_config_from_local_json(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfg, "user_config_dir", None)
    Path("config.json").write_text('{"theme": "textual-light"}\n')

    loaded = cfg.load_config()

    assert loaded.theme == "textual-light"


def test_platform_config_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    platform_root = tmp_path / "platform"
    platform_root.mkdir()
    monkeypatch.setattr(cfg, "user_config_dir", lambda *_: str(platform_root))
    Path("config.toml").write_text('theme = "local"\n')
    (platform_root / "config.toml").write_text('theme = "platform"\n')

    loaded = cfg.load_config()

    assert loaded.theme == "platform"


def test_save_config_writes_toml(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfg, "user_config_dir", None)

    path = cfg.save_config(cfg.AppConfig(theme="ansi-dark"))

    assert path == Path("config.toml")
    assert path.read_text() == 'theme = "ansi-dark"\n'


def test_config_scalar_coercion_helpers():
    assert cfg._coerce("yes", bool, False) is True
    assert cfg._coerce("0", bool, True) is False
    assert cfg._coerce("42", int, 0) == 42
    assert cfg._coerce("1.5", float, 0.0) == 1.5
    assert cfg._coerce("  ansi-dark  ", str, "textual-dark") == "ansi-dark"
    assert cfg._coerce(["1", 2], list[int], []) == [1, 2]
    assert cfg._coerce({"one": "1", "two": 2}, dict[str, int], {}) == {"one": 1, "two": 2}
    assert cfg._toml_value(True) == "true"
    assert cfg._toml_value("ansi-dark") == '"ansi-dark"'
    assert cfg._toml_value(["a", 2, False]) == '["a", 2, false]'
    assert cfg._toml_value({"key": ["value"]}) == '{ key = ["value"] }'
