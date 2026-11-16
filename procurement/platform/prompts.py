"""Environment and file-backed prompt overrides."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _prompt_overrides_file() -> str:
    return os.getenv("PROMPT_OVERRIDES_FILE", "").strip()


def prompt_config_error() -> str | None:
    if not _prompt_overrides_file():
        return None
    try:
        load_prompt_overrides()
    except Exception as exc:
        return str(exc)
    return None


def load_prompt_overrides() -> dict[str, str]:
    prompt_overrides_file = _prompt_overrides_file()
    if not prompt_overrides_file:
        return {}
    path = Path(prompt_overrides_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("PROMPT_OVERRIDES_FILE must contain a JSON object")
    return {str(key): str(value) for key, value in data.items() if str(value).strip()}


def prompt_text(key: str, default: str) -> str:
    env_value = os.getenv(f"PROMPT_{key.upper()}")
    if env_value:
        return env_value
    return load_prompt_overrides().get(key, default)
