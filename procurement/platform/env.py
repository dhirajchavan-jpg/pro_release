"""Minimal dotenv loader shared by every process entry point."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final


TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES: Final = frozenset({"0", "false", "no", "off"})


class EnvironmentValueError(RuntimeError):
    """Raised when a configured environment value cannot be used safely."""


def load_local_env(path: Path | None = None) -> None:
    env_path = path or (Path(__file__).resolve().parents[2] / ".env")
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip("\"'"))


def env_text(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = env_text(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise EnvironmentValueError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise EnvironmentValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise EnvironmentValueError(f"{name} must be at most {maximum}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_text(name, "1" if default else "0").casefold()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise EnvironmentValueError(
        f"{name} must be one of: 1, 0, true, false, yes, no, on, off"
    )
