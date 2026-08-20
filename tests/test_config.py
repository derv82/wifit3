from dataclasses import dataclass, field
from pathlib import Path

import wifit3.persist.config as cfg


@dataclass
class _TypedConfig:
    name: str = "default"
    enabled: bool = False
    count: int = 1
    ratio: float = 1.0
    items: list[int] = field(default_factory=lambda: [1])
    labels: dict[str, list[str]] = field(default_factory=lambda: {"default": ["label"]})


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


def test_save_config_writes_supported_toml_types(tmp_path):
    path = tmp_path / "config.toml"

    cfg.save_config(_TypedConfig(
        name="custom", enabled=True, count=42, ratio=1.5,
        items=[1, 2], labels={"known": ["a", "b"]}), path)

    assert cfg._load_file(path) == {
        "name": "custom",
        "enabled": True,
        "count": 42,
        "ratio": 1.5,
        "items": [1, 2],
        "labels": {"known": ["a", "b"]},
    }


def test_config_type_coercion_uses_dataclass_defaults(monkeypatch):
    monkeypatch.setattr(cfg, "AppConfig", _TypedConfig)

    loaded = cfg._from_dict({
        "name": "  custom  ",
        "enabled": "yes",
        "count": "42",
        "ratio": "1.5",
        "items": ["2", 3],
        "labels": {"known": ["a", "b"]},
    })

    assert loaded == _TypedConfig(
        name="custom", enabled=True, count=42, ratio=1.5,
        items=[2, 3], labels={"known": ["a", "b"]})

    loaded = cfg._from_dict({
        "name": "",
        "enabled": "maybe",
        "count": object(),
        "ratio": object(),
        "items": "not-a-list",
        "labels": "not-a-dict",
    })

    assert loaded == _TypedConfig()


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
