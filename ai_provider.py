"""Backward-compatible provider facade.

Application code keeps importing this module while each provider capability
lives in a focused module under :mod:`procurement.providers`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable
from procurement.platform.env import load_local_env

from procurement.platform.env import load_local_env

load_local_env()

from procurement.providers.cache import (  # noqa: E402
    _EMBEDDING_CACHE_LOCK,
    _EMBEDDING_MEMORY_CACHE,
    cached_openai_client,
)
from procurement.providers.errors import (  # noqa: E402
    AIConfigurationError,
    AIContextLimitError,
    AIProviderFailure,
    AIProviderHTTPError,
    AIProviderOutputError,
    AIProviderTransportError,
    AIProviderTruncatedError,
    _retry_after_seconds,
    _status_code,
    classify_provider_exception,
    is_provider_spend_cap_error,
)
from procurement.providers.embeddings import embed_texts as _embed_texts  # noqa: E402
from procurement.providers.json_protocol import (  # noqa: E402
    json_chat as _structured_json_chat,
    parse_json_object,
)
from procurement.providers.reranking import (  # noqa: E402
    _rerank_excerpt,
    rerank_texts as _rerank_texts,
)
from procurement.providers.retry import (  # noqa: E402
    _PROVIDER_GATES,
    _PROVIDER_GATES_LOCK,
    _ProviderGate,
    _provider_attempts,
    _provider_retry_delay,
    call_with_provider_retry,
    provider_slot,
)
from procurement.providers.settings import (  # noqa: E402
    AISettings,
    _capability_for_task,
    _is_verifier_task,
    _provider_group,
    _provider_int_setting,
    _provider_timeout,
    api_key_for,
    base_url_for,
    model_for,
    settings,
    validate,
)
from procurement.providers.transport import (  # noqa: E402
    _PDF_RENDER_LOCK,
    ensure_context_budget,
    messages_from_gemini_body,
    openai_client as _transport_openai_client,
    openai_compatible_content as _transport_openai_compatible_content,
    pdf_as_data_url,
    warmup_chat_if_needed,
    warmup_llm as _transport_warmup_llm,
)
from procurement.providers.verification import verify_evidence as _verify_evidence  # noqa: E402


logger = logging.getLogger("procurement.ai_provider")

# These names remain available for existing tests and operational scripts.
_cached_openai_client = cached_openai_client
_pdf_as_data_url = pdf_as_data_url
_messages_from_gemini_body = messages_from_gemini_body
_provider_slot = provider_slot
_warmup_chat_if_needed = warmup_chat_if_needed


def _openai_client(
    settings_value: AISettings,
    timeout: int,
    base_url: str | None = None,
    *,
    task: str = "primary",
) -> Any:
    """Preserve the historical client factory monkeypatch seam."""
    return _transport_openai_client(
        settings_value,
        timeout,
        base_url,
        task=task,
        client_cache=_cached_openai_client,
    )


def _call_with_provider_retry(
    call: Any,
    *,
    task: str,
    attempts: int,
    retry_delay: Any = None,
) -> Any:
    """Preserve the historical retry seam and its patchable root ``time``."""
    return call_with_provider_retry(
        call,
        task=task,
        attempts=attempts,
        retry_delay=retry_delay,
        sleep=time.sleep,
    )


def openai_compatible_content(
    settings_value: AISettings,
    requested_model: str,
    body: dict[str, Any],
    *,
    task: str,
    timeout: int,
    attempts: int,
    retry_delay: Callable[[int], float],
) -> tuple[str, dict[str, Any]]:
    return _transport_openai_compatible_content(
        settings_value,
        requested_model,
        body,
        task=task,
        timeout=timeout,
        attempts=attempts,
        retry_delay=retry_delay,
        client_factory=_openai_client,
    )


def warmup_llm(model: str | None = None, timeout: int = 60) -> None:
    return _transport_warmup_llm(
        model,
        timeout,
        client_factory=_openai_client,
    )


def embed_texts(values: list[str], model: str | None = None, timeout: int = 45) -> list[list[float]]:
    return _embed_texts(values, model, timeout, client_factory=_openai_client)


def _json_chat(task: str, prompt: str, *, timeout: int = 60) -> dict[str, Any]:
    return _structured_json_chat(
        task,
        prompt,
        timeout=timeout,
        client_factory=_openai_client,
    )


def rerank_texts(
    query: str,
    values: list[str],
    *,
    top_n: int | None = None,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    return _rerank_texts(
        query,
        values,
        top_n=top_n,
        timeout=timeout,
        client_factory=_openai_client,
        json_chat_call=_json_chat,
    )


def verify_evidence(claim: str, evidence: list[str], *, timeout: int = 60) -> dict[str, Any]:
    return _verify_evidence(
        claim,
        evidence,
        timeout=timeout,
        json_chat_call=_json_chat,
    )


__all__ = [
    "AIConfigurationError",
    "AIContextLimitError",
    "AIProviderFailure",
    "AIProviderHTTPError",
    "AIProviderOutputError",
    "AIProviderTransportError",
    "AIProviderTruncatedError",
    "AISettings",
    "api_key_for",
    "base_url_for",
    "classify_provider_exception",
    "embed_texts",
    "ensure_context_budget",
    "is_provider_spend_cap_error",
    "model_for",
    "openai_compatible_content",
    "parse_json_object",
    "rerank_texts",
    "settings",
    "validate",
    "verify_evidence",
    "warmup_llm",
]
