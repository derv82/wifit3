from pathlib import Path

import wifit3.config as cfg


def test_load_config_from_local_toml(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfg, "user_config_dir", None)
    Path("config.toml").write_text('theme = "ansi-dark"\n')

    loaded = cfg.load_config()

    assert loaded.theme == "ansi-dark"


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
