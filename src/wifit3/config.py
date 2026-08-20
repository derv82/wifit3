"""Small user config loader/saver.

Primary location is the OS config dir from platformdirs. A repo/current-directory
``config.toml`` or ``config.json`` is accepted as a portable fallback, which is useful
for source checkouts, dev runs, and platforms where the config dir cannot be created.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from platformdirs import user_config_dir
except Exception:  # pragma: no cover - only used when the optional import is unavailable
    user_config_dir = None


@dataclass
class AppConfig:
    theme: str = "textual-dark"


_LOCAL_NAMES = ("config.toml", "config.json")


def _platform_config_dir() -> Path | None:
    if user_config_dir is None:
        return None
    try:
        return Path(user_config_dir("wifit3", "wifit3"))
    except Exception:
        return None


def default_config_path() -> Path:
    root = _platform_config_dir()
    return (root / "config.toml") if root is not None else Path("config.toml")


def candidate_paths() -> list[Path]:
    paths: list[Path] = []
    root = _platform_config_dir()
    if root is not None:
        paths.extend(root / name for name in _LOCAL_NAMES)
    paths.extend(Path(name) for name in _LOCAL_NAMES)
    return paths


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _load_toml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = _parse_scalar(value)
    return data


def _load_file(path: Path) -> dict[str, Any]:
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    return _load_toml(path)


def _from_dict(data: dict[str, Any]) -> AppConfig:
    cfg = AppConfig()
    theme = data.get("theme")
    if isinstance(theme, str) and theme.strip():
        cfg.theme = theme.strip()
    return cfg


def load_config() -> AppConfig:
    for path in candidate_paths():
        if not path.exists():
            continue
        try:
            return _from_dict(_load_file(path))
        except Exception:
            continue
    return AppConfig()


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    path = path or default_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        path = Path("config.toml")
    if path.suffix == ".json":
        path.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(f'theme = "{config.theme}"\n', encoding="utf-8")
    return path
