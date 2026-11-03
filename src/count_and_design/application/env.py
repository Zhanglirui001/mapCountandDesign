"""Small typed environment and .env loading helpers."""

from __future__ import annotations

import os
from pathlib import Path
import sys


class ConfigError(RuntimeError):
    """Raised when required application configuration is missing or invalid."""


def load_env_file(env_path: str | Path | None = None) -> Path | None:
    """Load .env values into ``os.environ``.

    For packaged builds, bundled configuration is applied first and optional
    external files beside the executable may override those values. Variables
    already supplied by the process environment always take precedence.
    """

    if env_path is not None:
        paths = [Path(env_path).expanduser()]
    else:
        paths = resolve_env_paths()

    inherited_keys = {key for key, value in os.environ.items() if value}
    loaded: Path | None = None
    for index, path in enumerate(paths):
        if not path.exists():
            continue
        _apply_env_file(
            path,
            override=index > 0 and loaded is not None,
            protected_keys=inherited_keys,
        )
        loaded = path
    return loaded


def resolve_env_path(env_path: str | Path | None = None) -> Path | None:
    """Return the highest-priority existing .env path, if any."""

    paths = (
        [Path(env_path).expanduser()]
        if env_path is not None
        else resolve_env_paths()
    )
    existing = [path for path in paths if path.exists()]
    return existing[-1] if existing else (paths[0] if paths else None)


def resolve_env_paths(env_path: str | Path | None = None) -> list[Path]:
    """Return .env paths in load order for the active runtime."""

    if env_path is not None:
        return [Path(env_path).expanduser()]

    configured = os.environ.get("ENV_FILE")
    if configured:
        return [Path(configured).expanduser()]

    paths: list[Path] = []
    if getattr(sys, "frozen", False):
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            bundle_root = Path(bundle_dir)
            paths.extend(
                bundle_root / name
                for name in ("embedded.env", ".env")
            )
        paths.append(Path(sys.executable).resolve().parent / ".env")
    paths.append(Path.cwd() / ".env")
    if not getattr(sys, "frozen", False):
        module_path = Path(__file__).resolve()
        if len(module_path.parents) > 3:
            paths.append(module_path.parents[3] / ".env")
    return paths


def _apply_env_file(
    path: Path,
    *,
    override: bool,
    protected_keys: set[str],
) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in protected_keys:
            continue
        if override or not os.environ.get(key):
            os.environ[key] = _clean_env_value(value.strip())


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def env_str(name: str, default: str) -> str:
    return os.environ.get(name) or default


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid float value for {name}: {value}") from exc


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer value for {name}: {value}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be greater than zero.")
    return parsed


def env_int_or_none(name: str, default: int | None) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    if value.lower() in {"none", "null", "0"}:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer value for {name}: {value}") from exc


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"Invalid boolean value for {name}: {value}")


def _clean_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
