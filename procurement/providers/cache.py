"""Small bounded caches shared by the provider capability modules."""

from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import threading
from typing import Any


_EMBEDDING_CACHE_LOCK = threading.Lock()
_EMBEDDING_MEMORY_CACHE: OrderedDict[str, list[float]] = OrderedDict()


@lru_cache(maxsize=32)
def cached_openai_client(
    api_key: str,
    base_url: str,
    timeout: int,
    deployment_id: str,
) -> Any:
    """Reuse one SDK connection pool for an identical provider configuration."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        from .errors import AIConfigurationError

        raise AIConfigurationError("Install the openai package for AI_PROVIDER=openai_compatible") from exc
    client_options: dict[str, Any] = {}
    if deployment_id:
        client_options["default_headers"] = {"id": deployment_id}
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
        **client_options,
    )


__all__ = [
    "_EMBEDDING_CACHE_LOCK",
    "_EMBEDDING_MEMORY_CACHE",
    "cached_openai_client",
]
