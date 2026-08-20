"""Small user config loader/saver.

Primary location is the OS config dir from platformdirs. A repo/current-directory
``config.toml`` or ``config.json`` is accepted as a portable fallback, which is useful
for source checkouts, dev runs, and platforms where the config dir cannot be created.

Adding a setting is deliberately boring: add a typed field with a default to
``AppConfig`` (supported scalar types: ``str``, ``bool``, ``int``, ``float``), read it
from ``app.config`` / copy it into app state, and call ``app.save_preferences()`` after
the UI changes it. Unknown keys are ignored, missing/bad values keep the dataclass
default, and saving writes every ``AppConfig`` field back to TOML/JSON.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, get_type_hints

try:
    from platformdirs import user_config_dir
except Exception:  # pragma: no cover - only used when the optional import is unavailable
    user_config_dir = None


@dataclass
class AppConfig:
    """User preferences.

    To add a setting: declare a typed field with a default here, then read/write it
    through ``app.config``. ``load_config`` ignores unknown keys and coerces simple
    scalar types from TOML/JSON, so older/newer config files remain compatible.
    """
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
    lower = value.lower()
    if lower in ("true", "false"):
        return lower == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
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


def _coerce(value: Any, expected: type, default: Any) -> Any:
    if expected is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
        return default
    if expected is int and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if expected is float and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if expected is str:
        return value.strip() if isinstance(value, str) and value.strip() else default
    return value if isinstance(value, expected) else default


def _from_dict(data: dict[str, Any]) -> AppConfig:
    hints = get_type_hints(AppConfig)
    defaults = AppConfig()
    values: dict[str, Any] = {}
    for f in fields(AppConfig):
        default = getattr(defaults, f.name)
        if f.name in data:
            values[f.name] = _coerce(data[f.name], hints[f.name], default)
        else:
            values[f.name] = default
    return AppConfig(**values)


def load_config() -> AppConfig:
    for path in candidate_paths():
        if not path.exists():
            continue
        try:
            return _from_dict(_load_file(path))
        except Exception:
            continue
    return AppConfig()


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return json.dumps(str(value))


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    path = path or default_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        path = Path("config.toml")
    data = asdict(config)
    if path.suffix == ".json":
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text("".join(f"{k} = {_toml_value(v)}\n" for k, v in data.items()),
                        encoding="utf-8")
    return path
