"""Environment-backed provider settings and capability routing."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .errors import AIConfigurationError


@dataclass(frozen=True)
class AISettings:
    provider: str
    api_key: str | None
    base_url: str
    model_fast: str
    model_deep: str
    model_ocr: str
    model_embedding: str
    model_reranker: str
    model_verifier: str
    primary_base_url: str
    ocr_base_url: str
    embedding_base_url: str
    reranker_base_url: str
    verifier_base_url: str
    region: str
    require_india_region: bool
    response_format: str
    model_premium: str = ""


def settings() -> AISettings:
    """Read settings for every call so deployment ENV changes are explicit."""
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    if provider in {"openai", "openai-compatible", "openai_compatible", "yotta", "yota"}:
        provider = "openai_compatible"
    if provider != "gemini" and provider != "openai_compatible":
        raise AIConfigurationError("AI_PROVIDER must be gemini or openai_compatible")

    legacy_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    legacy_fast = os.getenv("GEMINI_MODEL_FAST", os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"))
    fast = (os.getenv("AI_MODEL_FAST") or (legacy_fast if provider == "gemini" else "")).strip()
    deep = (
        os.getenv("AI_MODEL_DEEP")
        or (os.getenv("GEMINI_MODEL_DEEP", fast) if provider == "gemini" else "")
    ).strip()
    ocr = (os.getenv("AI_MODEL_OCR") or fast).strip()
    embedding = (
        os.getenv("AI_MODEL_EMBEDDING")
        or (
            os.getenv("GEMINI_MODEL_EMBEDDING", "gemini-embedding-2")
            if provider == "gemini"
            else ""
        )
    ).strip()
    api_key = (
        (legacy_key if provider == "gemini" else "")
        or os.getenv("AI_API_KEY")
        or ""
    ).strip() or None
    base_url = os.getenv(
        "AI_BASE_URL",
        os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta") if provider == "gemini" else "",
    ).strip()
    # Each capability can be routed to a separate OpenAI-compatible deployment.
    # Empty values deliberately inherit AI_BASE_URL; there is no provider fallback.
    capability_default = (
        os.getenv("GEMINI_OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/").strip()
        if provider == "gemini"
        else base_url
    )
    capability_urls = {
        name: os.getenv(f"AI_{name}_BASE_URL", "").strip() or capability_default
        for name in ("PRIMARY", "OCR", "EMBEDDING", "RERANKER", "VERIFIER")
    }
    return AISettings(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model_fast=fast,
        model_deep=deep,
        model_ocr=ocr,
        model_embedding=embedding,
        model_reranker=(
            os.getenv("AI_MODEL_RERANKER") or (fast if provider == "gemini" else "")
        ).strip(),
        model_verifier=(
            os.getenv("AI_MODEL_VERIFIER") or (deep if provider == "gemini" else "")
        ).strip(),
        primary_base_url=capability_urls["PRIMARY"],
        ocr_base_url=capability_urls["OCR"],
        embedding_base_url=capability_urls["EMBEDDING"],
        reranker_base_url=capability_urls["RERANKER"],
        verifier_base_url=capability_urls["VERIFIER"],
        region=os.getenv("AI_INFERENCE_REGION", "").strip().lower(),
        require_india_region=os.getenv("AI_REQUIRE_INDIA_REGION", "0") == "1",
        response_format=os.getenv("AI_OPENAI_RESPONSE_FORMAT", "json_object").strip().lower(),
        model_premium=os.getenv("AI_MODEL_PREMIUM", "").strip(),
    )


def validate(
    settings_value: AISettings,
    *,
    need_embedding: bool = False,
    need_ocr: bool = False,
    need_reranker: bool = False,
    need_verifier: bool = False,
) -> None:
    if not settings_value.api_key:
        raise AIConfigurationError("AI_API_KEY is required for the selected AI_PROVIDER")
    if not settings_value.model_fast or not settings_value.model_deep:
        raise AIConfigurationError("AI_MODEL_FAST and AI_MODEL_DEEP are required for the selected AI_PROVIDER")
    if settings_value.provider == "openai_compatible" and not settings_value.primary_base_url:
        raise AIConfigurationError("AI_PRIMARY_BASE_URL or AI_BASE_URL is required for AI_PROVIDER=openai_compatible")
    if settings_value.require_india_region and settings_value.region != "india":
        raise AIConfigurationError("AI_INFERENCE_REGION=india is required when AI_REQUIRE_INDIA_REGION=1")
    if need_ocr and not settings_value.model_ocr:
        raise AIConfigurationError("AI_MODEL_OCR is required when provider OCR is enabled")
    if need_ocr and settings_value.provider == "openai_compatible" and not settings_value.ocr_base_url:
        raise AIConfigurationError("AI_OCR_BASE_URL or AI_BASE_URL is required when provider OCR is enabled")
    if need_embedding and not settings_value.model_embedding:
        raise AIConfigurationError("AI_MODEL_EMBEDDING is required before embeddings can be used")
    if need_embedding and not settings_value.embedding_base_url:
        raise AIConfigurationError("AI_EMBEDDING_BASE_URL or AI_BASE_URL is required before embeddings can be used")
    if need_reranker and not settings_value.model_reranker:
        raise AIConfigurationError("AI_MODEL_RERANKER is required before reranking can be used")
    if need_reranker and not settings_value.reranker_base_url:
        raise AIConfigurationError("AI_RERANKER_BASE_URL or AI_BASE_URL is required before reranking can be used")
    if need_reranker and os.getenv("AI_RERANKER_API_STYLE", "chat").strip().lower() not in {"chat", "rerank"}:
        raise AIConfigurationError("AI_RERANKER_API_STYLE must be chat or rerank")
    if need_verifier and not settings_value.model_verifier:
        raise AIConfigurationError("AI_MODEL_VERIFIER is required before verification can be used")
    if need_verifier and settings_value.provider == "openai_compatible" and not settings_value.verifier_base_url:
        raise AIConfigurationError("AI_VERIFIER_BASE_URL or AI_BASE_URL is required before verification can be used")
    if settings_value.response_format not in {"json_object", "json_schema", "none"}:
        raise AIConfigurationError(
            "AI_OPENAI_RESPONSE_FORMAT must be json_object, json_schema, or none"
        )


def _is_verifier_task(task: str) -> bool:
    return "verify" in task or "verifier" in task or "judge" in task


def model_for(settings_value: AISettings, requested_model: str, task: str) -> str:
    if "rerank" in task:
        validate(settings_value, need_reranker=True)
        return settings_value.model_reranker
    if "embedding" in task:
        validate(settings_value, need_embedding=True)
        return settings_value.model_embedding
    if _is_verifier_task(task):
        validate(settings_value, need_verifier=True)
        return settings_value.model_verifier
    if "ocr" in task or "signature" in task or "structural" in task:
        return settings_value.model_ocr
    if requested_model and requested_model == settings_value.model_premium:
        return settings_value.model_premium
    if requested_model and requested_model in {
        os.getenv("AI_MODEL_UNDERSTANDING", "").strip(),
        os.getenv("AI_MODEL_SOURCE_EXTRACTION", "").strip(),
    }:
        return requested_model
    if requested_model and requested_model == settings_value.model_deep:
        return settings_value.model_deep
    return settings_value.model_fast


def base_url_for(settings_value: AISettings, task: str) -> str:
    if "rerank" in task:
        return settings_value.reranker_base_url
    if "embedding" in task:
        return settings_value.embedding_base_url
    if _is_verifier_task(task):
        return settings_value.verifier_base_url
    if "ocr" in task or "signature" in task or "structural" in task:
        return settings_value.ocr_base_url
    return settings_value.primary_base_url


def api_key_for(settings_value: AISettings, task: str) -> str:
    capability = (
        "RERANKER"
        if "rerank" in task
        else "VERIFIER"
        if _is_verifier_task(task)
        else "OCR"
        if "ocr" in task or "signature" in task or "structural" in task
        else "EMBEDDING"
        if "embedding" in task
        else "PRIMARY"
    )
    key = os.getenv(f"AI_{capability}_API_KEY", "").strip()
    key_file = os.getenv(f"AI_{capability}_API_KEY_FILE", "").strip()
    if not key and key_file:
        try:
            key = Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AIConfigurationError(
                f"Unable to read AI_{capability}_API_KEY_FILE: {exc}"
            ) from exc
    # Gemini selection must use GEMINI_API_KEY unless a capability-specific key
    # is configured; a stale OpenAI-compatible AI_API_KEY must not leak across
    # provider switches.
    provider_key = settings_value.api_key if getattr(settings_value, "provider", "") == "gemini" else ""
    key = key or provider_key or os.getenv("AI_API_KEY", "").strip() or settings_value.api_key or ""
    if not key:
        raise AIConfigurationError(f"AI_{capability}_API_KEY or AI_API_KEY is required")
    return key


def _capability_for_task(task: str) -> str:
    if "rerank" in task:
        return "RERANKER"
    if "embedding" in task:
        return "EMBEDDING"
    if _is_verifier_task(task):
        return "VERIFIER"
    if "ocr" in task or "signature" in task or "structural" in task:
        return "OCR"
    return "PRIMARY"


def _provider_group(task: str) -> str:
    capability = _capability_for_task(task)
    if capability in {"PRIMARY", "VERIFIER"} and "vision" in task:
        return "VISION"
    return "LLM" if capability in {"PRIMARY", "VERIFIER"} else capability


def _provider_int_setting(
    task: str,
    suffix: str,
    default: int,
    *,
    maximum: int = 3600,
) -> int:
    capability = _capability_for_task(task)
    group = _provider_group(task)
    names = [f"AI_{capability}_{suffix}"]
    if group != capability:
        names.append(f"AI_{group}_{suffix}")
    for name in names:
        raw = os.getenv(name, "").strip()
        if raw:
            try:
                return max(1, min(maximum, int(raw)))
            except ValueError as exc:
                raise AIConfigurationError(f"{name} must be an integer") from exc
    return max(1, min(maximum, int(default)))


def _provider_timeout(task: str, fallback: int) -> int:
    return _provider_int_setting(task, "REQUEST_TIMEOUT_SECONDS", fallback)


__all__ = [
    "AISettings",
    "api_key_for",
    "base_url_for",
    "model_for",
    "settings",
    "validate",
    "_capability_for_task",
    "_is_verifier_task",
    "_provider_group",
    "_provider_int_setting",
    "_provider_timeout",
]
