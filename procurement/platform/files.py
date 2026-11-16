"""Durable local-file helpers shared by caches and compatibility artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable


def write_json_atomic(
    path: Path,
    value: Any,
    *,
    indent: int | None = 2,
    default: Callable[[Any], Any] | None = None,
) -> None:
    """Replace a JSON file only after its complete payload reaches disk."""
    payload = json.dumps(value, indent=indent, ensure_ascii=True, default=default)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
